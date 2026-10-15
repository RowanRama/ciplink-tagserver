"""A fake driver injected through the factory, so no test opens a socket."""

import re
import time

import pytest
from ciplink import CommError, ErrorKind, Tag, TagError
from fastapi.testclient import TestClient

from ciplink_tagserver.config import load_config
from ciplink_tagserver.server import create_app
from ciplink_tagserver.worker import PlcWorker


def udt(name, size, members, aoi=None, string=None):
    """A ciplink-like struct data type. members: name -> atomic type name, a udt(...) dict, or (either, count)."""
    internal = {}
    for member, data_type in members.items():
        count = 0
        if isinstance(data_type, tuple):
            data_type, count = data_type
        if isinstance(data_type, dict):
            internal[member] = {"tag_type": "struct", "data_type": data_type,
                                "data_type_name": data_type["name"], "array": count}
        else:
            internal[member] = {"tag_type": "atomic", "data_type": data_type, "data_type_name": data_type,
                                "array": count}
    data_type = {"name": name, "template": {"structure_size": size}, "internal_tags": internal}
    if aoi is not None:
        data_type["aoi"] = aoi
    if string is not None:
        data_type["string"] = string
    return data_type


def tag_def(data_type, dim=0, dims=()):
    """A ciplink-like entry of plc.tags; dims: the array dimensions."""
    dimensions = (list(dims) + [0, 0, 0])[:3]
    if isinstance(data_type, dict):
        return {"tag_type": "struct", "data_type": data_type, "data_type_name": data_type["name"], "dim": dim,
                "dimensions": dimensions}
    return {"tag_type": "atomic", "data_type": data_type, "data_type_name": data_type, "dim": dim,
            "dimensions": dimensions}


def _key(d, name):
    """Logix names ignore case."""
    return name if name in d else next(k for k in d if k.lower() == name.lower())


class FakePLC:
    """State shared by every driver instance a worker creates for one PLC."""

    def __init__(self):
        # tag -> value; tags not here read as "Tag doesn't exist". A struct is a dict (an array a list) and
        # member paths (Line1.Motor.Speed, Arr[1].X, Count.5) and ranges (Trend[2]{3}) are read out of it.
        self.values = {}
        self.errors = {}  # tag -> error string
        self.fail = False  # open() and read() raise CommError
        self.version = 1
        self.reads = []  # tuples of tag names per read call (one per chunk of a planned cycle)
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

    def get_tag_info(self, name):
        """As ciplink: the definition of a tag or member path, array indexes ignored."""
        prefix, rest = "", name
        if name.lower().startswith("program:"):
            prefix, rest = name.split(".", 1)
            prefix += "."
        base, *attrs = re.sub(r"\[[^\]]*\]", "", rest).split(".")
        info = self.plc.tags[_key(self.plc.tags, prefix + base)]
        for attr in attrs:
            internal = info["data_type"]["internal_tags"]
            info = internal[_key(internal, attr)]
        return info

    def _value(self, name):
        if name in self.plc.values:
            return self.plc.values[name]
        prefix, rest = "", name
        if name.lower().startswith("program:"):
            prefix, rest = name.split(".", 1)
            prefix += "."
        parts = re.findall(r"[^.\[\]]+|\[\d+\]", rest)
        value = self.plc.values[_key(self.plc.values, prefix + parts[0])]
        for part in parts[1:]:
            if part.startswith("["):
                value = value[int(part[1:-1])]
            elif part.isdigit():
                value = bool(value >> int(part) & 1)
            else:
                value = value[_key(value, part)]
        return value

    def read(self, *tags):
        if self.plc.fail:
            raise CommError("fake: connection lost")
        self.plc.reads.append(tags)
        results = []
        for tag in tags:
            if tag in self.plc.errors:
                results.append(Tag(tag, None, None, self.plc.errors[tag]))
                continue
            try:
                ranged = re.fullmatch(r"(.+?)(?:\[(\d+)\])?\{(\d+)\}", tag)
                if ranged:
                    start, count = int(ranged.group(2) or 0), int(ranged.group(3))
                    value = self._value(ranged.group(1))[start:start + count]
                    if len(value) != count:
                        raise IndexError(tag)
                else:
                    value = self._value(tag)
                results.append(Tag(tag, value, "DINT", None))
            except (KeyError, IndexError, TypeError, StopIteration):
                results.append(Tag(tag, None, None, TagError(f"Tag doesn't exist - {tag}", ErrorKind.NOT_FOUND)))
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
{server}

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

    def _make(*names, mock=False, extra="", server="grace_s = 0", setup=None):
        names = names or ("PLC1",)
        plcs = "\n".join(f'[plc.{n}]\npath = "192.0.2.{i}"\n{extra}' for i, n in enumerate(names, 1))
        path = tmp_path / "tagserver.toml"
        path.write_text(CONFIG.format(plcs=plcs, first=names[0], server=server))
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


MOTOR = udt("Motor", 40, {"Speed": "REAL", "Current": "REAL", "Running": "BOOL", "Count": "DINT"})
STATION = udt("Station", 200, {"Motor": MOTOR, "Ready": "BOOL", "Data": ("DINT", 4), "Bits": ("DWORD", 1)})
BIG = udt("Big", 4000, {"A": "DINT", "B": "DINT"})
VALVE = udt("Valve_AOI", 60, {"EnableIn": "BOOL", "EnableOut": "BOOL", "Open": "BOOL", "Cmd": "BOOL"},
            aoi={"whole_read": False, "readable": ["EnableIn", "EnableOut", "Open"], "refused": ["Cmd"]})
PUMP = udt("Pump_AOI", 40, {"EnableIn": "BOOL", "EnableOut": "BOOL", "Run": "BOOL", "Speed": "REAL"},
           aoi={"whole_read": True, "readable": ["EnableIn", "EnableOut", "Run", "Speed"], "refused": []})
CELL = udt("Cell", 300, {"Valve": VALVE, "Mode": "DINT", "Step": "DINT"})  # holds an AOI that can't be read whole
STR = udt("STRING", 88, {"LEN": "DINT", "DATA": ("SINT", 82)}, string=82)


def motor(speed):
    return {"Speed": speed, "Current": speed / 10, "Running": True, "Count": 0b101101}


def station(n):
    return {"Motor": motor(n * 1.5), "Ready": n % 2 == 0, "Data": [n, n + 1, n + 2, n + 3], "Bits": [True] * 32}


def plant(fake):
    """Generic structs, AOIs and atomics on a FakePLC."""
    fake.tags = {
        "Line1": tag_def(STATION), "Line2": tag_def(STATION), "Arr": tag_def(STATION, dim=1, dims=[5]),
        "Program:Station1.Motor": tag_def(MOTOR), "Big1": tag_def(BIG), "Valve1": tag_def(VALVE),
        "Pump1": tag_def(PUMP), "Cell1": tag_def(CELL), "Name1": tag_def(STR),
        "Speed": tag_def("REAL"), "Count": tag_def("DINT"), "Flags": tag_def("DINT"),
    }
    fake.values.update({
        "Line1": station(1), "Line2": station(2), "Arr": [station(i) for i in range(5)],
        "Program:Station1.Motor": motor(7.5), "Big1": {"A": 1, "B": 2},
        "Valve1": {"EnableIn": True, "EnableOut": True, "Open": False},  # Cmd is local: not readable
        "Pump1": {"EnableIn": True, "EnableOut": True, "Run": True, "Speed": 12.5},
        "Cell1": {"Valve": {"EnableIn": True, "EnableOut": False, "Open": True}, "Mode": 2, "Step": 30},
        "Name1": "hello", "Speed": 3.5, "Count": 9, "Flags": 0b1010,
    })
