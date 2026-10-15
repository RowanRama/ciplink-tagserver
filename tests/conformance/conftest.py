"""The conformance suite talks to a server over a real WebSocket: an in-process --mock server of the Test Bench
(examples/bench) by default, or any server serving the bench tags and rules with
``pytest tests/conformance --url http://host:8765 --token T [--viewer-token V]``."""

import json
import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from websockets.sync.client import connect

from ciplink_tagserver.config import load_config
from ciplink_tagserver.mock import make_factory
from ciplink_tagserver.server import create_app

BENCH = Path(__file__).resolve().parents[2] / "examples" / "bench"


def pytest_addoption(parser):
    parser.addoption("--url", default=None, help="conformance: HTTP base of the server under test")
    parser.addoption("--token", default=None, help="conformance: a token that may write PLC1/Bench.*")
    parser.addoption("--viewer-token", default=None, help="conformance: a read-only token")


class Server:
    """Where the server under test is; ``mock`` (the bench PLC's MockState) and ``audit`` only in-process."""

    def __init__(self, http, token, viewer, mock=None, audit=None):
        self.http, self.token, self.viewer, self.mock, self.audit = http, token, viewer, mock, audit
        self.ws = http.replace("http", "ws", 1) + "/ws"

    def get(self, path, token=None):
        request = urllib.request.Request(self.http + path)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def raw(self):
        """A Client that has not said hello yet."""
        return Client(self.ws)

    def client(self, token=None):
        """A Client that has said hello (with this server's write token unless given)."""
        client = Client(self.ws)
        client.welcome = client.hello(token or self.token)
        return client

    def local(self):
        if self.mock is None:
            pytest.skip("needs control of the driver: in-process server only")


class Client:
    """A blocking protocol client that keeps the latest values and info it has seen."""

    def __init__(self, url):
        self.ws = connect(url, open_timeout=5)
        self.values, self.info, self.log = {}, {}, []
        self._id = 100

    def send(self, msg):
        self.ws.send(json.dumps(msg))

    def recv(self, timeout):
        msg = json.loads(self.ws.recv(timeout=timeout))
        self.log.append(msg)
        if msg["op"] == "values":
            self.values.update(msg["d"])
        elif msg["op"] == "info":
            self.info.update(msg["d"])
        return msg

    def until(self, pred, timeout=5):
        """Receives until pred(msg); returns that message."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no matching message in {timeout} s")
            try:
                msg = self.recv(left)
            except TimeoutError:
                continue
            if pred(msg):
                return msg

    def wait(self, cond, timeout=5):
        """Receives until cond() holds (checked after each message and on a quiet socket)."""
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise AssertionError(f"condition not met in {timeout} s")
            try:
                self.recv(0.05)
            except TimeoutError:
                pass

    def drain(self, seconds):
        """Receives for ``seconds``; returns the messages."""
        start, end = len(self.log), time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            try:
                self.recv(left)
            except TimeoutError:
                break
        return self.log[start:]

    def hello(self, token):
        self.send({"op": "hello", "v": 1, "token": token})
        return self.until(lambda m: m["op"] == "welcome")

    def sub(self, sub_id, tags=None, tier=1, page=None):
        msg = {"op": "sub", "id": sub_id, "tier": tier}
        if tags is not None:
            msg["tags"] = tags
        if page is not None:
            msg["page"] = page
        self.send(msg)
        return self.until(lambda m: m["op"] == "ack" and m["id"] == sub_id)

    def request(self, op, **fields):
        """Sends a write or momentary and returns its ack."""
        self._id += 1
        msg_id = self._id
        self.send({"op": op, "id": msg_id, **fields})
        return self.until(lambda m: m["op"] == "ack" and m["id"] == msg_id)

    def value(self, name):
        return self.values.get(name, [None])[0]

    def close(self):
        self.ws.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(request, tmp_path_factory):
    url = request.config.getoption("--url", default=None)
    if url:
        token = request.config.getoption("--token", default=None)
        if not token:
            pytest.exit("--url needs --token (a token that may write PLC1/Bench.*)")
        yield Server(url.rstrip("/"), token, request.config.getoption("--viewer-token", default=None))
        return
    tmp = tmp_path_factory.mktemp("bench")
    shutil.copy(BENCH / "mock_tags.json", tmp)
    shutil.copy(BENCH / "tagserver.toml", tmp)
    config = load_config(tmp / "tagserver.toml")
    config.port = _free_port()
    factory = make_factory()
    app = create_app(config, factory)
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    end = time.monotonic() + 10
    while not uv.started:
        assert time.monotonic() < end, "in-process server did not start"
        time.sleep(0.02)
    srv = Server(f"http://127.0.0.1:{config.port}", "bench-hmi", "bench-viewer", audit=Path(config.audit_log))
    end = time.monotonic() + 10
    while srv.get("/health")["plcs"]["PLC1"]["state"] != "connected":
        assert time.monotonic() < end, "mock PLC did not connect"
        time.sleep(0.05)
    srv.mock = factory.states["PLC1"]
    yield srv
    uv.should_exit = True
    thread.join(5)


@pytest.fixture
def client(server):
    c = server.client()
    yield c
    c.close()
