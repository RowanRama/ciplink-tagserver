"""The hub (subscriptions, fan-out, writes) and the FastAPI app (WebSocket /ws plus HTTP)."""

import asyncio
import hmac
import json
import logging
import os
from collections import Counter
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from fnmatch import fnmatchcase

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from .worker import PlcWorker

log = logging.getLogger(__name__)

PROTOCOL = 1


def driver_factory(cfg):
    from ciplink import LogixDriver

    if cfg.tag_cache_file:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.tag_cache_file)), exist_ok=True)
        return LogixDriver(cfg.path, tag_cache_file=cfg.tag_cache_file)
    return LogixDriver(cfg.path)


def mock_factory(cfg):
    from .mock import MockDriver

    return MockDriver(cfg)


class Session:
    def __init__(self, token):
        self.token = token
        self.subs = {}  # sub id -> {"tier": int, "tags": {qualified name: name as the client sent it}}
        self.queue = asyncio.Queue()  # messages to send

    def name_of(self, qname):
        for sub in self.subs.values():
            if qname in sub["tags"]:
                return sub["tags"][qname]
        return None


class Hub:
    def __init__(self, config, factory):
        self.config = config
        self.sessions = set()
        self.refs = Counter()  # qualified name -> subscriptions across sessions
        self.loop = None
        self.workers = {
            name: PlcWorker(cfg, factory, config.cycle_ms, self._on_values, self._on_status, self._on_meta)
            for name, cfg in config.plcs.items()
        }

    def start(self, loop):
        self.loop = loop
        for worker in self.workers.values():
            worker.start()

    def stop(self):
        for worker in self.workers.values():
            worker.stop()

    def states(self):
        return {name: worker.state for name, worker in self.workers.items()}

    def authenticate(self, secret):
        if not isinstance(secret, str):
            return None
        for token in self.config.tokens.values():
            if hmac.compare_digest(token.secret.encode(), secret.encode()):
                return token
        return None

    # worker threads -> event loop

    def _call(self, func, *args):
        try:
            self.loop.call_soon_threadsafe(func, *args)
        except RuntimeError:  # loop closed during shutdown
            pass

    def _on_values(self, plc, changes):
        self._call(self._fan_values, plc, changes)

    def _on_status(self, plc, state):
        self._call(self._broadcast, "status")

    def _on_meta(self, plc):
        self._call(self._broadcast, "meta", plc)

    def _fan_values(self, plc, changes):
        for session in self.sessions:
            d = {}
            for tag, entry in changes.items():
                name = session.name_of(f"{plc}/{tag}")
                if name is not None:
                    d[name] = entry
            if d:
                session.queue.put_nowait({"op": "values", "d": d})

    def _broadcast(self, op, plc=None):
        message = {"op": "status", "plcs": self.states()} if op == "status" else {"op": "meta", "plc": plc}
        for session in self.sessions:
            session.queue.put_nowait(message)

    # requests, on the event loop

    def subscribe(self, session, sub_id, names, tier):
        """Returns the cached values of the subscribed tags, keyed by the names as given."""
        if not isinstance(names, list):
            raise TypeError("tags must be a list")
        tags = {}
        for name in names:
            plc, tag = self.config.qualify(name)
            tags[f"{plc}/{tag}"] = name
        if sub_id in session.subs:
            self.unsubscribe(session, sub_id)
        session.subs[sub_id] = {"tier": tier, "tags": tags}
        snapshot = {}
        for qname, name in tags.items():
            plc, tag = qname.split("/", 1)
            self.refs[qname] += 1
            if self.refs[qname] == 1:
                self.workers[plc].add_tags([tag])
            entry = self.workers[plc].snapshot([tag]).get(tag)
            if entry is not None:
                snapshot[name] = entry
        return snapshot

    def unsubscribe(self, session, sub_id):
        sub = session.subs.pop(sub_id, None)
        if sub is None:
            return False
        for qname in sub["tags"]:
            self.refs[qname] -= 1
            if self.refs[qname] <= 0:
                del self.refs[qname]
                plc, tag = qname.split("/", 1)
                self.workers[plc].remove_tags([tag])
        return True

    def drop(self, session):
        for sub_id in list(session.subs):
            self.unsubscribe(session, sub_id)
        self.sessions.discard(session)

    async def write(self, session, name, value):
        qname = name
        try:
            plc, tag = self.config.qualify(name)
            qname = f"{plc}/{tag}"
            if not any(fnmatchcase(qname.lower(), p.lower()) for p in session.token.write):
                ok, error = False, "not allowed"
            else:
                ok, error = await asyncio.wrap_future(self.workers[plc].write(tag, value))
        except (ValueError, TypeError) as err:
            ok, error = False, str(err)
        self.audit(session.token.name, qname, value, ok, error)
        return ok, error

    def audit(self, token, tag, value, ok, error):
        line = {"time": datetime.now(timezone.utc).isoformat(), "token": token, "tag": tag,
                "value": value, "ok": ok, "error": error}
        with open(self.config.audit_log, "a", encoding="utf-8") as file:
            file.write(json.dumps(line, default=str) + "\n")


def create_app(config, factory=None, mock=False) -> FastAPI:
    hub = Hub(config, factory or (mock_factory if mock else driver_factory))

    @asynccontextmanager
    async def lifespan(app):
        hub.start(asyncio.get_running_loop())
        yield
        hub.stop()

    app = FastAPI(title="ciplink-tagserver", lifespan=lifespan)
    app.state.hub = hub

    @app.get("/health")
    def health():
        return {"plcs": {
            name: {"state": w.state, "last_cycle_ms": w.last_cycle_ms, "last_ok_ts": w.last_ok_ts,
                   "reconnects": w.reconnects, "reloads": w.reloads}
            for name, w in hub.workers.items()
        }}

    @app.get("/stats")
    def stats():
        return {
            "sessions": len(hub.sessions),
            "plcs": {
                name: {"reads_per_cycle": w.reads_per_cycle, "last_cycle_ms": w.last_cycle_ms,
                       "cycle_ms": config.cycle_ms, "overruns": w.overruns, "subscriptions": len(w.tags)}
                for name, w in hub.workers.items()
            },
        }

    @app.get("/tags")
    def tags(request: Request, plc: str | None = None):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or hub.authenticate(auth[7:]) is None:
            raise HTTPException(401, "invalid token")
        if plc is None and len(hub.workers) == 1:
            plc = next(iter(hub.workers))
        if plc not in hub.workers:
            raise HTTPException(404, f"unknown PLC {plc!r}")
        worker = hub.workers[plc]
        return {"plc": plc, "state": worker.state, "tags": worker.tag_list}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        try:
            hello = await ws.receive_json()
        except (WebSocketDisconnect, ValueError):
            hello = None
        token = None
        if isinstance(hello, dict) and hello.get("op") == "hello" and hello.get("v") == PROTOCOL:
            token = hub.authenticate(hello.get("token"))
        if token is None:
            with suppress(Exception):
                await ws.close(4401)
            return

        session = Session(token)
        hub.sessions.add(session)
        await ws.send_json({"op": "welcome", "v": PROTOCOL, "plcs": hub.states(), "write": bool(token.write)})

        async def sender():
            while True:
                await ws.send_json(await session.queue.get())

        async def do_write(msg_id, name, value):
            ok, error = await hub.write(session, name, value)
            session.queue.put_nowait({"op": "ack", "id": msg_id, "ok": ok, "error": error})

        send_task = asyncio.create_task(sender())
        write_tasks = set()
        try:
            while True:
                raw = await ws.receive_text()
                msg_id = None
                try:
                    msg = json.loads(raw)
                    msg_id = msg.get("id")
                    op = msg["op"]
                    if op == "sub":
                        snapshot = hub.subscribe(session, msg["id"], msg["tags"], msg.get("tier", 1))
                        session.queue.put_nowait({"op": "ack", "id": msg_id, "ok": True, "error": None})
                        if snapshot:
                            session.queue.put_nowait({"op": "values", "d": snapshot})
                    elif op == "unsub":
                        ok = hub.unsubscribe(session, msg["id"])
                        session.queue.put_nowait(
                            {"op": "ack", "id": msg_id, "ok": ok, "error": None if ok else "unknown id"})
                    elif op == "write":
                        task = asyncio.create_task(do_write(msg["id"], msg["tag"], msg["value"]))
                        write_tasks.add(task)
                        task.add_done_callback(write_tasks.discard)
                    else:
                        raise ValueError(f"unknown op {op!r}")
                except KeyError as err:
                    session.queue.put_nowait({"op": "error", "id": msg_id, "error": f"missing field {err}"})
                except (ValueError, TypeError, AttributeError) as err:
                    session.queue.put_nowait({"op": "error", "id": msg_id, "error": str(err)})
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            hub.drop(session)

    return app
