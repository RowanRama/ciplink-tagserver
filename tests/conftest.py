"""A fake driver injected through the factory, so no test opens a socket."""

import time

import pytest
from ciplink import CommError, Tag
from fastapi.testclient import TestClient

from ciplink_tagserver.config import load_config
from ciplink_tagserver.server import create_app
from ciplink_tagserver.worker import PlcWorker


class FakePLC:
    """State shared by every driver instance a worker creates for one PLC."""

    def __init__(self):
        self.values = {}  # tag -> value; tags not here read as "Tag doesn't exist"
        self.errors = {}  # tag -> error string
        self.fail = False  # open() and read() raise CommError
        self.version = 1
        self.reads = []  # tuples of tag names per read call
        self.writes = []
        self.write_error = None
        self.tags = {}
        self.opens = 0


class FakeDriver:
    def __init__(self, plc: FakePLC):
        self.plc = plc

    @property
    def _tag_table_version(self):
        return self.plc.version

    @property
    def tags(self):
        return self.plc.tags

    def open(self):
        self.plc.opens += 1
        if self.plc.fail:
            raise CommError("fake: connection refused")
        return True

    def close(self):
        pass

    def read(self, *tags):
        if self.plc.fail:
            raise CommError("fake: connection lost")
        self.plc.reads.append(tags)
        results = []
        for tag in tags:
            if tag in self.plc.errors:
                results.append(Tag(tag, None, None, self.plc.errors[tag]))
            elif tag in self.plc.values:
                results.append(Tag(tag, self.plc.values[tag], "DINT", None))
            else:
                results.append(Tag(tag, None, None, f"Tag doesn't exist - {tag}"))
        return results[0] if len(results) == 1 else results

    def write(self, tag, value):
        if self.plc.fail:
            raise CommError("fake: connection lost")
        if self.plc.write_error:
            return Tag(tag, value, None, self.plc.write_error)
        self.plc.writes.append((tag, value))
        self.plc.values[tag] = value
        return Tag(tag, value, "DINT", None)


CONFIG = """
[server]
cycle_ms = 20
audit_log = "audit.log"

{plcs}

[token.viewer]
secret = "view"

[token.hmi1]
secret = "s3cret"
write = ["{first}/Speed*", "{first}/Program:Main.Door*"]
"""


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(PlcWorker, "backoff", (0.01, 0.02, 0.05, 0.1))


@pytest.fixture
def make(tmp_path):
    """make(*plc_names, setup=fn(fakes)) -> (TestClient, {name: FakePLC}, config); the client is closed at teardown."""
    clients = []

    def _make(*names, mock=False, extra="", setup=None):
        names = names or ("PLC1",)
        plcs = "\n".join(f'[plc.{n}]\npath = "192.0.2.{i}"\n{extra}' for i, n in enumerate(names, 1))
        path = tmp_path / "tagserver.toml"
        path.write_text(CONFIG.format(plcs=plcs, first=names[0]))
        config = load_config(path)
        fakes = {n: FakePLC() for n in names}
        if setup:
            setup(fakes)  # before the workers start
        factory = None if mock else (lambda cfg: FakeDriver(fakes[cfg.name]))
        client = TestClient(create_app(config, factory=factory, mock=mock))
        client.__enter__()
        clients.append(client)
        return client, fakes, config

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def until(ws, pred):
    """Receives messages until pred(msg) is true; returns that message."""
    while True:
        msg = ws.receive_json()
        if pred(msg):
            return msg


def wait_for(cond, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not met")


def hello(ws, token="s3cret"):
    ws.send_json({"op": "hello", "v": 1, "token": token})
    return until(ws, lambda m: m["op"] == "welcome")
