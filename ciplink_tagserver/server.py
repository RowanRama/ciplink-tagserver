"""The hub (subscriptions, fan-out, writes, momentary) and the FastAPI app (WebSocket /ws plus HTTP)."""

import asyncio
import hmac
import json
import logging
import os
import queue
import re
import threading
from collections import Counter, deque
from concurrent.futures import Future
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .rules import BIT, Rules, match
from .worker import PlcWorker

log = logging.getLogger(__name__)

PROTOCOL = 1
FEATURES = ["info", "momentary", "readback", "errors"]
RELEASES = ("release", "timeout", "disconnect")
# kinds a retry never fixes; a release failing any other way (refused, comm, table_changed, ...) is owed
FINAL = {"bad_request", "not_allowed", "not_writable", "out_of_range", "not_found", "access", "bad_value"}
_INDEX = re.compile(r"\[([^\]]*)\]")


def _canonical(match):
    """[02] and [ 2] address element 2 in ciplink and the mock (int()), so rules must see [2]."""
    try:
        return "[" + ",".join(str(int(i)) for i in match[1].split(",")) + "]"
    except ValueError:
        return match[0]


def driver_factory(cfg):
    from ciplink import LogixDriver

    if cfg.tag_cache_file:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.tag_cache_file)), exist_ok=True)
        return LogixDriver(cfg.path, tag_cache_file=cfg.tag_cache_file)
    return LogixDriver(cfg.path)


class Session:
    def __init__(self, token):
        self.token = token
        # sub id -> {"tier": int, "tags": {name as the client sent it: qualified resolved name}, "follow": page}
        self.subs = {}
        self.control = deque()  # ack, info, status, meta, error: sent in order, before the values
        self.pending = {}  # client name -> latest entry, sent as one values message
        self.wake = asyncio.Event()
        self.info_sent = set()  # client names
        self.pressed = {}  # qname -> (watchdog TimerHandle, name as sent)
        self.closed = False  # dropped: a press still in flight is released at once

    def send(self, msg):
        self.control.append(msg)
        self.wake.set()

    def push(self, name, entry):
        self.pending[name] = entry
        self.wake.set()

    async def run_sender(self, send_json):
        """Sends the control messages in order, then one values message with everything pending."""
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.control:
                await send_json(self.control.popleft())
            if self.pending:
                d, self.pending = self.pending, {}
                await send_json({"op": "values", "d": d})


def load_pages(path) -> dict:
    """page path -> set of qualified tag names, from the pages file; empty if there is none."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as file:
            return {page: set(tags) for page, tags in json.load(file).items()}
    except (OSError, ValueError, AttributeError) as err:
        log.warning("cannot read %s: %r", path, err)
        return {}


def save_pages(path, pages):
    """Writes the pages file atomically: a temporary file, then a rename over the old one."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump({page: sorted(tags) for page, tags in sorted(pages.items())}, file, indent=1)
    os.replace(tmp, path)


def in_range(value, lo, hi) -> bool:
    """min/max check of a number, or of every number in a list or dict; bools and non-numbers pass."""
    if isinstance(value, dict):
        return all(in_range(v, lo, hi) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(in_range(v, lo, hi) for v in value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return (lo is None or value >= lo) and (hi is None or value <= hi)


class Hub:
    save_delay = 2.0  # seconds: page learning is saved at most this often
    retry_delay = 0.5  # seconds before a release refused while connected is written again

    def __init__(self, config, factory):
        self.config = config
        self.rules = Rules(config)
        self.sessions = set()
        self.refs = {}  # qualified name -> Counter(tier -> subscriptions across sessions)
        self.subscribers = {}  # qualified name -> {session: set of client names}
        # page path -> qualified names its tier-1 subs used; names of a PLC no longer configured are dropped
        self.pages = {page: {q for q in qnames if "/" in q and q.split("/", 1)[0] in config.plcs}
                      for page, qnames in load_pages(config.pages_file).items()}
        self._save_handle = None
        self.loop = None
        self.writes = 0  # queued to a worker, momentary included
        self.write_rejects = 0  # by the hub's checks
        self.momentary_timeouts = 0
        self.owed = {}  # qname -> (session, name, momentary) of a release the PLC never got, sent on connect
        self._audit = queue.Queue()  # lines for the audit thread, None stops it
        self._audit_thread = None
        self.workers = {
            name: PlcWorker(cfg, factory, config.cycle_ms, self._on_values, self._on_status, self._on_meta,
                            tier2_period_s=config.tier2_period_s, grace_s=config.grace_s,
                            planner=config.planner, whole_max_bytes=config.whole_max_bytes,
                            rules=self.rules, on_info=self._on_info)
            for name, cfg in config.plcs.items()
        }
        for plc, worker in self.workers.items():  # an internal reference that is never released
            for tag in self.rules.always(plc):
                self.refs[f"{plc}/{tag}"] = Counter({1: 1})
                worker.set_tier(tag, 1)

    def start(self, loop):
        self.loop = loop
        self._audit_thread = threading.Thread(target=self._write_audit, name="audit", daemon=True)
        self._audit_thread.start()
        for worker in self.workers.values():
            worker.start()

    def stop(self):
        for session in list(self.sessions):  # queues the releases of pressed tags, which the workers still run
            self.drop(session)
        for worker in self.workers.values():
            worker.stop()
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_pages()
        if self._audit_thread is not None:
            self._audit.put(None)
            self._audit_thread.join(5)

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
        self._call(self._status, plc, state)

    def _on_meta(self, plc):
        self._call(self._meta, plc)

    def _on_info(self, plc, descriptions):
        self._call(self._send_info, plc, set(descriptions))

    def _fan_values(self, plc, changes):
        for tag, entry in changes.items():
            for session, names in self.subscribers.get(f"{plc}/{tag}", {}).items():
                for name in names:
                    session.push(name, entry)

    def _broadcast(self, message):
        for session in self.sessions:
            session.send(message)

    def _status(self, plc, state):
        self._broadcast({"op": "status", "plcs": self.states()})
        if state == "connected":  # types are known now
            self._send_info(plc)
            for qname in [q for q in list(self.owed) if q.split("/", 1)[0] == plc]:
                self._resend(qname)

    def _meta(self, plc):
        self._broadcast({"op": "meta", "plc": plc})
        self._send_info(plc)

    def _send_info(self, plc, tags=None):
        """Sends the info of the PLC's subscribed names again (only of ``tags`` if given)."""
        per_session = {}
        for qname, subs in self.subscribers.items():
            p, tag = qname.split("/", 1)
            if p != plc or (tags is not None and tag not in tags):
                continue
            for session, names in subs.items():
                d = per_session.setdefault(session, {})
                for name in names:
                    d[name] = self.info(session, plc, tag, name)
        for session, d in per_session.items():
            session.send({"op": "info", "d": d})

    # tag info

    def _allowed(self, token, *qnames):
        return any(match(p, q) for p in token.write for q in qnames)

    def info(self, session, plc, tag, name) -> dict:
        """The info record of a resolved tag for a session (section 8 of the design)."""
        rule = self.rules.effective(plc, tag)
        group = self.rules.group(plc, tag)
        sent = self._qualify(name)[1]
        common = {
            "writable": bool(rule.get("writable")) and self._allowed(session.token, f"{plc}/{sent}", f"{plc}/{tag}"),
            "rate_ms": group["rate_ms"] if group else self.config.cycle_ms,
            "group": group["name"] if group else None,
        }
        if BIT.match(tag):
            return {"type": "BOOL", **common}
        worker = self.workers[plc]
        type_info = worker.type_info(tag)
        record = {"type": type_info.get("type")}
        if "dims" in type_info:
            record["dims"] = type_info["dims"]
        record.update(common)
        for key in ("min", "max", "units", "deadband", "bits"):
            if rule.get(key) is not None:
                record[key] = rule[key]
        description = rule.get("description") or worker.description(tag)
        if description is not None:
            record["description"] = description
        return record

    # requests, on the event loop

    def _qualify(self, name) -> tuple:
        """(plc, tag as sent, resolved tag); ValueError or TypeError if it does not qualify."""
        if not isinstance(name, str):
            raise TypeError("tag names must be strings")
        plc, tag = self.config.qualify(name)
        tag = _INDEX.sub(_canonical, tag)
        return plc, tag, self.rules.resolve(plc, tag)

    def subscribe(self, session, sub_id, names, tier=1, page=None):
        """Queues the info of names new to the session and the cached values; returns those values.

        ``page`` (a route path): tier-1 tags teach the server that page's tags; with ``names`` None the
        subscription is that page's learned tags, kept up to date as the page teaches more."""
        if tier not in (1, 2, 3):
            raise ValueError("tier must be 1, 2 or 3")
        if page is not None and not isinstance(page, str):
            raise TypeError("page must be a string")
        follow = None
        if names is None:
            if page is None:
                raise KeyError("tags")
            follow = page
            tags = {self._client_name(qname): qname for qname in sorted(self.pages.get(page, ()))}
        else:
            if not isinstance(names, list):
                raise TypeError("tags must be a list")
            tags = {}
            for name in names:
                plc, _, tag = self._qualify(name)
                tags[name] = f"{plc}/{tag}"
        old = session.subs.pop(sub_id, None)  # replaced: released after the new one counts, so no tag drops out
        session.subs[sub_id] = {"tier": tier, "tags": tags, "follow": follow}
        for qname in set(tags.values()):
            plc, tag = qname.split("/", 1)
            counts = self.refs.setdefault(qname, Counter())
            before = min(counts) if counts else None
            counts[tier] += 1
            if before is None or tier < before:
                self.workers[plc].set_tier(tag, tier)
        if old is not None:
            self._release(old)
        self._reindex(session, set(tags.values()) | (set(old["tags"].values()) if old else set()))
        info, snapshot = {}, {}
        for name, qname in tags.items():
            plc, tag = qname.split("/", 1)
            if name not in session.info_sent:
                session.info_sent.add(name)
                info[name] = self.info(session, plc, tag, name)
            entry = self.workers[plc].snapshot([tag]).get(tag)
            if entry is not None:
                snapshot[name] = entry
        if info:
            session.send({"op": "info", "d": info})
        for name, entry in snapshot.items():
            session.push(name, entry)
        if page is not None and follow is None and tier == 1:
            self._learn(page, set(tags.values()))
        return snapshot

    def unsubscribe(self, session, sub_id):
        sub = session.subs.pop(sub_id, None)
        if sub is None:
            return False
        self._release(sub)
        self._reindex(session, set(sub["tags"].values()))
        return True

    def _release(self, sub):
        for qname in set(sub["tags"].values()):
            counts = self.refs[qname]
            before = min(counts)
            counts[sub["tier"]] -= 1
            if counts[sub["tier"]] <= 0:
                del counts[sub["tier"]]
            after = min(counts) if counts else None
            if not counts:
                del self.refs[qname]
            if after != before:
                plc, tag = qname.split("/", 1)
                self.workers[plc].set_tier(tag, after)

    def _reindex(self, session, qnames):
        """Rebuilds the session's entries of ``subscribers`` for these qualified names from its subs."""
        names = {}
        for sub in session.subs.values():
            for name, qname in sub["tags"].items():
                if qname in qnames:
                    names.setdefault(qname, set()).add(name)
        for qname in qnames:
            subs = self.subscribers.setdefault(qname, {})
            if qname in names:
                subs[session] = names[qname]
            else:
                subs.pop(session, None)
                if not subs:
                    del self.subscribers[qname]
        # a name subscribed again later gets fresh info (its type may be known by then)
        session.info_sent &= {name for sub in session.subs.values() for name in sub["tags"]}

    def _client_name(self, qname):
        """How a page subscription names a learned tag: unqualified when there is only one PLC."""
        return qname.split("/", 1)[1] if len(self.workers) == 1 else qname

    def _learn(self, page, qnames):
        known = self.pages.setdefault(page, set())
        if qnames <= known:
            return
        known.update(qnames)
        if self.config.pages_file and self.loop is not None and self._save_handle is None:
            self._save_handle = self.loop.call_later(self.save_delay, self._save_pages)
        for session in self.sessions:  # page subscriptions follow what their page learns
            for sub_id, sub in list(session.subs.items()):
                if sub["follow"] == page:
                    self.subscribe(session, sub_id, None, sub["tier"], page)

    def _save_pages(self):
        self._save_handle = None
        try:
            save_pages(self.config.pages_file, self.pages)
        except OSError as err:
            log.warning("cannot write %s: %r", self.config.pages_file, err)

    def drop(self, session):
        """A closed session: its subscriptions go and its pressed momentary tags are released (queued now)."""
        session.closed = True
        try:
            for sub_id in list(session.subs):
                self.unsubscribe(session, sub_id)
        finally:  # whatever went wrong above, the buttons are released and the session goes
            self.sessions.discard(session)
            for handle, name in list(session.pressed.values()):
                handle.cancel()
                self._submit(session, name, False, "disconnect")
            session.pressed.clear()

    # writes and momentary

    def _reject(self, error, kind):
        self.write_rejects += 1
        return False, error, kind, None

    def _start_write(self, session, name, value, momentary=False):
        """Checks 1-4 (and the BOOL type for momentary), then queues the write with no await before it, so
        writes keep their order. Returns (qualified name, old value, Future or a rejection result)."""
        try:
            plc, sent, tag = self._qualify(name)
        except (ValueError, TypeError) as err:
            return str(name), None, self._reject(str(err), "bad_request")
        qname, worker = f"{plc}/{tag}", self.workers[plc]
        if value is None:
            return qname, None, self._reject("missing value", "bad_request")
        if "{" in sent:  # X{1} would match other rules than X: no min/max or writable bypass by spelling
            return qname, None, self._reject("element counts are not allowed in writes", "bad_request")
        old = worker.snapshot([tag]).get(tag, [None])[0]
        type_info = worker.type_info(tag)
        if "dims" in type_info:  # written by name, an array takes one value at element 0: its rules would be skipped
            return qname, old, self._reject("write an element of the array", "bad_request")
        if momentary and type_info.get("type") not in (None, "BOOL"):
            return qname, old, self._reject("momentary needs a BOOL tag or a bit", "bad_request")
        if not self._allowed(session.token, f"{plc}/{sent}", qname):
            return qname, old, self._reject("not allowed", "not_allowed")
        rule = self.rules.effective(plc, tag)
        if not rule.get("writable"):
            return qname, old, self._reject("not writable", "not_writable")
        if not in_range(value, rule.get("min"), rule.get("max")):
            return qname, old, self._reject("out of range", "out_of_range")
        self.writes += 1
        return qname, old, worker.write(tag, value)

    def _submit(self, session, name, value, momentary=None):
        """_start_write, audited once the result is known (in the worker thread for a queued write), so a
        release queued at shutdown is audited too. Returns (qualified name, Future or a rejection result)."""
        qname, old, result = self._start_write(session, name, value, momentary is not None)

        def done(r):
            self.audit(session.token.name, qname, old, value, r, momentary)
            if momentary in RELEASES:  # a release the PLC never got is sent again
                if r[0] or r[2] in FINAL:
                    self.owed.pop(qname, None)
                    return
                self.owed[qname] = (session, name, momentary)
                worker = self.workers.get(qname.split("/", 1)[0])
                if worker is not None and worker.state == "connected" and self.loop is not None:
                    # refused while connected (ciplink's reload backoff): no connect comes to resend it
                    self._call(self.loop.call_later, self.retry_delay, self._resend, qname)

        if isinstance(result, Future):
            result.add_done_callback(lambda f: done(f.result()))
        else:
            done(result)
        return qname, result

    def _resend(self, qname):
        """Writes an owed release again; left owed while the PLC is not connected (sent on connect), and
        dropped if the tag was pressed again meanwhile."""
        if self.workers[qname.split("/", 1)[0]].state != "connected":
            return
        owed = self.owed.pop(qname, None)  # a worker thread may have popped it already
        if owed is None or any(qname in s.pressed for s in self.sessions):
            return
        session, name, momentary = owed
        self._submit(session, name, False, momentary)

    async def write(self, session, name, value, momentary=None):
        """(ok, error, kind, read-back value); ``momentary`` is press, release, timeout or disconnect."""
        qname, result = self._submit(session, name, value, momentary)
        if momentary == "press" and isinstance(result, Future):
            self.owed.pop(qname, None)
            if session.closed:  # dropped while this press was in flight
                self._submit(session, name, False, "disconnect")
            else:
                self._arm(session, qname, name)
        elif momentary == "release":
            self._disarm(session, qname)
        if isinstance(result, Future):
            result = tuple(await asyncio.wrap_future(result))
        return result

    async def momentary(self, session, name, action):
        """press writes true, release false (both like a write); hold refreshes the watchdog and returns None."""
        if action == "hold":
            self.hold(session, name)
            return None
        if action not in ("press", "release"):
            raise ValueError(f"unknown action {action!r}")
        return await self.write(session, name, action == "press", action)

    def hold(self, session, name):
        """Refreshes the watchdog of a pressed tag; ignored if the tag is not pressed."""
        try:
            plc, _, tag = self._qualify(name)
        except (ValueError, TypeError):
            return
        if f"{plc}/{tag}" in session.pressed:
            self._arm(session, f"{plc}/{tag}", session.pressed[f"{plc}/{tag}"][1])

    def _arm(self, session, qname, name):
        self._disarm(session, qname)
        handle = self.loop.call_later(self.config.momentary_timeout_ms / 1000, self._timeout, session, qname)
        session.pressed[qname] = (handle, name)

    def _disarm(self, session, qname):
        pressed = session.pressed.pop(qname, None)
        if pressed is not None:
            pressed[0].cancel()

    def _timeout(self, session, qname):
        _, name = session.pressed.pop(qname)
        self.momentary_timeouts += 1
        self._submit(session, name, False, "timeout")

    def audit(self, token, tag, old, value, result, momentary=None):
        if not self.config.audit_log:  # "" turns the audit log off
            return
        ok, error, kind, _ = result
        line = {"time": datetime.now(timezone.utc).isoformat(), "token": token, "tag": tag, "old": old,
                "value": value, "ok": ok, "error": error, "kind": kind}
        if momentary is not None:
            line["momentary"] = momentary
        self._audit.put(json.dumps(line, default=str))

    def _write_audit(self):
        """The audit thread: appends the queued lines, so a slow disk never blocks the event loop."""
        while (line := self._audit.get()) is not None:
            try:
                with open(self.config.audit_log, "a", encoding="utf-8") as file:
                    file.write(line + "\n")
            except Exception as err:  # keep draining whatever went wrong
                log.warning("cannot write %s: %r", self.config.audit_log, err)


def create_app(config, factory=None, mock=False) -> FastAPI:
    if factory is None and mock:
        from .mock import make_factory

        factory = make_factory()
    hub = Hub(config, factory or driver_factory)

    @asynccontextmanager
    async def lifespan(app):
        hub.start(asyncio.get_running_loop())
        yield
        hub.stop()

    app = FastAPI(title="ciplink-tagserver", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["Authorization"])
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
            "writes": hub.writes,
            "write_rejects": hub.write_rejects,
            "momentary_timeouts": hub.momentary_timeouts,
            "plcs": {
                name: {"reads_per_cycle": w.reads_per_cycle, "tags_per_cycle": w.tags_per_cycle,
                       "last_cycle_ms": w.last_cycle_ms, "cycle_ms": config.cycle_ms, "overruns": w.overruns,
                       "subscriptions": len(w.tiers), **w.tier_counts(), "group_fallbacks": w.group_fallbacks,
                       "groups": w.group_stats()}
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
        return {"plc": plc, "state": worker.state, "version": worker.version, "tags": worker.tag_list,
                "types": worker.types, "rules": config.plcs[plc].rules, "groups": config.plcs[plc].groups}

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
        await ws.send_json({"op": "welcome", "v": PROTOCOL, "plcs": hub.states(), "write": bool(token.write),
                            "features": FEATURES})

        async def do_write(msg_id, request):
            ok, error, kind, value = await request
            session.send({"op": "ack", "id": msg_id, "ok": ok, "error": error, "kind": kind, "value": value})

        def start_write(msg_id, request):
            task = asyncio.create_task(do_write(msg_id, request))
            write_tasks.add(task)
            task.add_done_callback(write_tasks.discard)

        send_task = asyncio.create_task(session.run_sender(ws.send_json))
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
                        tags, page = msg.get("tags"), msg.get("page")
                        if tags is None and page is None:
                            raise KeyError("tags")
                        hub.subscribe(session, msg["id"], tags, msg.get("tier", 1), page)
                        session.send({"op": "ack", "id": msg_id, "ok": True, "error": None})
                    elif op == "unsub":
                        ok = hub.unsubscribe(session, msg["id"])
                        session.send({"op": "ack", "id": msg_id, "ok": ok, "error": None if ok else "unknown id"})
                    elif op == "write":
                        # a missing tag or value is a bad_request ack, audited like any write
                        start_write(msg["id"], hub.write(session, msg.get("tag"), msg.get("value")))
                    elif op == "momentary":
                        action, tag = msg["action"], msg.get("tag")
                        if action == "hold":
                            hub.hold(session, tag)
                        elif action in ("press", "release"):
                            start_write(msg["id"], hub.momentary(session, tag, action))
                        else:
                            raise ValueError(f"unknown action {action!r}")
                    else:
                        raise ValueError(f"unknown op {op!r}")
                except KeyError as err:
                    session.send({"op": "error", "id": msg_id, "error": f"missing field {err}"})
                except (ValueError, TypeError, AttributeError) as err:
                    session.send({"op": "error", "id": msg_id, "error": str(err)})
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            hub.drop(session)

    return app
