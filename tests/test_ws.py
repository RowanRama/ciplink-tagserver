import json
import os
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import hello, until, wait_for

# the 0.2 write tests' tags made writable (writes need a writable rule and a token pattern)
WRITABLE = """
[[plc.PLC1.rule]]
match = "SpeedSP"
min = 0
max = 1800
units = "rpm"

[[plc.PLC1.rule]]
match = ["Speed*", "Program:Main.Door*"]
writable = true

[[plc.PLC1.rule]]
match = "Word"
bits = { EStop = 0, Door = 1 }
"""


def is_values(m):
    return m["op"] == "values"


def is_ack(m):
    return m["op"] == "ack"


def audit_lines(config, n):
    wait_for(lambda: os.path.exists(config.audit_log) and len(open(config.audit_log).read().splitlines()) >= n)
    return [json.loads(line) for line in open(config.audit_log, encoding="utf-8")]


def test_welcome(make):
    client, fakes, _ = make()
    with client.websocket_connect("/ws") as ws:
        welcome = hello(ws)
        assert welcome["v"] == 1 and welcome["write"] is True and set(welcome["plcs"]) == {"PLC1"}
        assert welcome["features"] == ["info", "momentary", "readback", "errors"]
    with client.websocket_connect("/ws") as ws:
        assert hello(ws, "view")["write"] is False


@pytest.mark.parametrize("first", [
    {"op": "hello", "v": 1, "token": "wrong"},
    {"op": "sub", "id": 1, "tags": ["Speed"]},
    {"op": "hello", "v": 2, "token": "s3cret"},
])
def test_auth_failure_closes_4401(make, first):
    client, _, _ = make()
    with client.websocket_connect("/ws") as ws:
        ws.send_json(first)
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
        assert err.value.code == 4401


def test_subscribe_snapshot_and_changes_only(make):
    client, fakes, _ = make()
    fake = fakes["PLC1"]
    fake.values.update({"Speed": 10, "Count": 1})
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Speed", "PLC1/Count", "Nope"], "tier": 1})
        assert until(ws, lambda m: m["op"] == "ack") == {"op": "ack", "id": 1, "ok": True, "error": None}
        first = until(ws, is_values)["d"]
        assert first["Speed"][:2] == [10, "good"]
        assert first["PLC1/Count"][:2] == [1, "good"]  # keyed by the name as subscribed
        assert first["Nope"][:2] == [None, "missing"]
        assert first["Nope"][3]["kind"] == "not_found" and first["Speed"][3] is None
        assert isinstance(first["Speed"][2], int)

        fake.values["Speed"] = 11
        assert until(ws, is_values)["d"].keys() == {"Speed"}

    with client.websocket_connect("/ws") as ws:  # a new session gets the cached values at once
        hello(ws)
        ws.send_json({"op": "sub", "id": 7, "tags": ["Speed"]})
        until(ws, lambda m: m["op"] == "ack")
        assert until(ws, is_values)["d"]["Speed"][:2] == [11, "good"]


def test_two_sessions_share_a_tag_and_unsub_stops_polling(make):
    client, fakes, _ = make()
    fake = fakes["PLC1"]
    fake.values["Speed"] = 5
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        hello(a)
        hello(b)
        a.send_json({"op": "sub", "id": 1, "tags": ["Speed"]})
        until(a, is_values)
        b.send_json({"op": "sub", "id": 1, "tags": ["Speed"]})
        assert until(b, lambda m: m["op"] == "ack")["ok"]
        assert until(b, is_values)["d"]["Speed"][:2] == [5, "good"]  # snapshot

        fake.values["Speed"] = 6
        assert until(a, is_values)["d"]["Speed"][0] == 6
        assert until(b, is_values)["d"]["Speed"][0] == 6

        a.send_json({"op": "unsub", "id": 1})
        assert until(a, lambda m: m["op"] == "ack")["ok"]
        time.sleep(0.1)
        fake.reads.clear()
        wait_for(lambda: len(fake.reads) >= 2)
        assert all("Speed" in r for r in fake.reads)  # b still subscribes

        b.send_json({"op": "unsub", "id": 1})
        until(b, lambda m: m["op"] == "ack")
        time.sleep(0.1)
        fake.reads.clear()
        time.sleep(0.1)
        assert fake.reads == []  # refcount 0: no longer polled
        assert client.get("/stats").json()["plcs"]["PLC1"]["subscriptions"] == 0

        b.send_json({"op": "unsub", "id": 99})
        assert until(b, lambda m: m["op"] == "ack") == {"op": "ack", "id": 99, "ok": False, "error": "unknown id"}


def test_closing_a_session_drops_its_subscriptions(make):
    client, fakes, _ = make()
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["A", "B"]})
        until(ws, is_values)
        assert client.get("/stats").json()["sessions"] == 1
    wait_for(lambda: client.get("/stats").json()["sessions"] == 0)
    assert client.app.state.hub.workers["PLC1"].tags == set()


def test_bad_requests(make):
    client, _, _ = make("A", "B")
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text("not json")
        assert until(ws, lambda m: m["op"] == "error")["id"] is None
        ws.send_json({"op": "frobnicate", "id": 3})
        assert until(ws, lambda m: m["op"] == "error") == {"op": "error", "id": 3, "error": "unknown op 'frobnicate'"}
        ws.send_json({"op": "sub", "id": 4, "tags": ["Speed"]})  # two PLCs: must be qualified
        msg = until(ws, lambda m: m["op"] == "error")
        assert msg["id"] == 4 and "qualified" in msg["error"]
        ws.send_json({"op": "sub", "id": 5})
        assert until(ws, lambda m: m["op"] == "error")["error"] == "missing field 'tags'"


def write(ws, msg_id, tag, value):
    ws.send_json({"op": "write", "id": msg_id, "tag": tag, "value": value})
    return until(ws, lambda m: is_ack(m) and m["id"] == msg_id)


def connected(client):
    wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")


def test_write_checks_readback_and_audit(make):
    client, fakes, config = make(extra=WRITABLE)
    fake = fakes["PLC1"]
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        connected(client)
        assert write(ws, 1, "SpeedSP", 42) == {"op": "ack", "id": 1, "ok": True, "error": None, "kind": None,
                                               "value": 42}  # the read-back
        assert fake.writes == [("SpeedSP", 42)]
        assert write(ws, 2, "PLC1/program:main.DoorOpen", True)["ok"]  # patterns match case-insensitively
        assert write(ws, 3, "Other", 1) == {"op": "ack", "id": 3, "ok": False, "error": "not allowed",
                                            "kind": "not_allowed", "value": None}
        assert write(ws, 4, "SpeedSP", 1801)["kind"] == "out_of_range"
        assert write(ws, 5, "B/SpeedSP", 1)["kind"] == "bad_request"
    assert len(fake.writes) == 2

    with client.websocket_connect("/ws") as ws:
        hello(ws, "view")
        assert write(ws, 1, "SpeedSP", 1)["error"] == "not allowed"

    lines = audit_lines(config, 6)
    assert [(l["token"], l["tag"], l["old"], l["value"], l["ok"], l["error"], l["kind"]) for l in lines] == [
        ("hmi1", "PLC1/SpeedSP", None, 42, True, None, None),
        ("hmi1", "PLC1/program:main.DoorOpen", None, True, True, None, None),
        ("hmi1", "PLC1/Other", None, 1, False, "not allowed", "not_allowed"),
        ("hmi1", "PLC1/SpeedSP", None, 1801, False, "out of range", "out_of_range"),
        ("hmi1", "B/SpeedSP", None, 1, False, "unknown PLC 'B'", "bad_request"),
        ("viewer", "PLC1/SpeedSP", None, 1, False, "not allowed", "not_allowed"),
    ]
    assert all("time" in l and "momentary" not in l for l in lines)
    stats = client.get("/stats").json()
    assert (stats["writes"], stats["write_rejects"], stats["momentary_timeouts"]) == (2, 4, 0)


def test_write_needs_a_writable_rule(make):
    client, fakes, _ = make(extra='[[plc.PLC1.rule]]\nmatch = "Speed*"\nunits = "rpm"\n')
    with client.websocket_connect("/ws") as ws:  # the token pattern alone no longer allows a write
        hello(ws)
        connected(client)
        assert write(ws, 1, "SpeedSP", 42)["kind"] == "not_writable"
    assert fakes["PLC1"].writes == []


def test_audit_old_value_from_the_cache(make):
    client, fakes, config = make(extra=WRITABLE)
    fakes["PLC1"].values["SpeedSP"] = 7
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["SpeedSP"]})
        until(ws, is_values)
        assert write(ws, 2, "SpeedSP", 8)["value"] == 8
    assert audit_lines(config, 1)[0]["old"] == 7


def test_write_ack_with_driver_error(make):
    client, fakes, config = make(extra=WRITABLE)
    fakes["PLC1"].write_error = "Privilege violation"
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        connected(client)
        ack = write(ws, 9, "SpeedSP", 1)
        assert (ack["ok"], ack["error"], ack["value"]) == (False, "Privilege violation", None)
        assert ack["kind"] is not None
    line = audit_lines(config, 1)[-1]
    assert line["ok"] is False and line["error"] == "Privilege violation"


def test_write_while_disconnected(make):
    client, fakes, _ = make(extra=WRITABLE, setup=lambda f: setattr(f["PLC1"], "fail", True))
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ack = write(ws, 1, "SpeedSP", 1)
        assert (ack["error"], ack["kind"]) == ("PLC not connected", "refused")
    assert fakes["PLC1"].writes == []


def test_info_before_the_snapshot_and_bit_names(make):
    tags = {"SpeedSP": {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"},
            "Word": {"tag_type": "atomic", "data_type": "DINT", "data_type_name": "DINT"}}
    client, fakes, _ = make(extra=WRITABLE, setup=lambda f: setattr(f["PLC1"], "tags", tags))
    fake = fakes["PLC1"]
    fake.values.update({"SpeedSP": 1.5, "Word": 0b10})
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        connected(client)
        ws.send_json({"op": "sub", "id": 1, "tags": ["SpeedSP", "Word.Door", "Word.1"]})
        info = until(ws, lambda m: m["op"] == "info")["d"]
        assert info["SpeedSP"] == {"type": "REAL", "writable": True, "rate_ms": 20, "group": None, "min": 0,
                                   "max": 1800, "units": "rpm"}
        assert info["Word.Door"] == info["Word.1"] == {"type": "BOOL", "writable": False, "rate_ms": 20,
                                                       "group": None}
        d = {}
        while len(d) < 3:
            d.update(until(ws, is_values)["d"])
        assert d["Word.Door"][:2] == d["Word.1"][:2] == [True, "good"]  # an alias and its bit, both as sent
        assert all("Word.Door" not in r for r in fake.reads)  # resolved by the server


def test_momentary_over_the_socket(make):
    client, fakes, config = make(extra=WRITABLE, server="grace_s = 0\nmomentary_timeout_ms = 300")
    fake = fakes["PLC1"]
    fake.values["SpeedStart"] = False
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        connected(client)
        ws.send_json({"op": "momentary", "id": 1, "tag": "SpeedStart", "action": "press"})
        assert until(ws, is_ack) == {"op": "ack", "id": 1, "ok": True, "error": None, "kind": None, "value": True}
        for _ in range(4):  # holds keep it pressed past the timeout
            ws.send_json({"op": "momentary", "tag": "SpeedStart", "action": "hold"})
            time.sleep(0.05)
        assert fake.writes == [("SpeedStart", True)]
        wait_for(lambda: fake.writes[-1] == ("SpeedStart", False))  # no hold: released by the watchdog
        ws.send_json({"op": "momentary", "id": 2, "tag": "SpeedStart", "action": "press"})
        assert until(ws, is_ack)["ok"]
        ws.send_json({"op": "momentary", "id": 3, "tag": "SpeedStart", "action": "release"})
        assert until(ws, is_ack)["value"] is False
        ws.send_json({"op": "momentary", "id": 4, "tag": "SpeedStart", "action": "push"})
        assert until(ws, lambda m: m["op"] == "error") == {"op": "error", "id": 4, "error": "unknown action 'push'"}
        ws.send_json({"op": "momentary", "id": 5, "tag": "SpeedStart", "action": "press"})
        assert until(ws, is_ack)["ok"]
    wait_for(lambda: len(fake.writes) == 6)  # the closed session's press is released
    assert fake.writes[-1] == ("SpeedStart", False)
    lines = audit_lines(config, 6)
    assert [l["momentary"] for l in lines] == ["press", "timeout", "press", "release", "press", "disconnect"]
    assert client.get("/stats").json()["momentary_timeouts"] == 1


def test_disconnect_marks_stale_then_reconnects(make):
    client, fakes, _ = make()
    fake = fakes["PLC1"]
    fake.values["Speed"] = 3
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Speed"]})
        assert until(ws, is_values)["d"]["Speed"][:2] == [3, "good"]

        fake.fail = True
        seen = {}  # status (a control message) may go out before the stale values
        while len(seen) < 2:
            msg = until(ws, lambda m: m["op"] in ("values", "status"))
            seen.setdefault(msg["op"], msg)
        assert seen["values"]["d"]["Speed"] == [3, "stale", seen["values"]["d"]["Speed"][2], None]  # value kept
        assert seen["status"]["plcs"] == {"PLC1": "disconnected"}

        fake.fail = False
        until(ws, lambda m: m["op"] == "status" and m["plcs"]["PLC1"] == "connected")
        assert until(ws, is_values)["d"]["Speed"][:2] == [3, "good"]
    assert client.get("/health").json()["plcs"]["PLC1"]["reconnects"] >= 1


def test_reload_sends_meta(make):
    client, fakes, _ = make()
    fake = fakes["PLC1"]
    fake.values["Speed"] = 1
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Speed"]})
        until(ws, is_values)
        fake.tags = {"Speed": {"tag_type": "atomic", "data_type": "DINT", "data_type_name": "DINT"}}
        fake.version = 2  # as if ciplink reloaded the tag table during a read
        assert until(ws, lambda m: m["op"] == "meta") == {"op": "meta", "plc": "PLC1"}
    assert client.get("/health").json()["plcs"]["PLC1"]["reloads"] == 1
    tags = client.get("/tags", headers={"Authorization": "Bearer view"}).json()["tags"]
    assert tags == [{"name": "Speed", "type": "DINT"}]  # tag list rebuilt after the reload


def test_several_plcs(make):
    client, fakes, _ = make("A", "B")
    fakes["A"].values["X"] = 1
    fakes["B"].values["X"] = 2
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["A/X", "B/X"]})
        d = {}
        while len(d) < 2:
            d.update(until(ws, is_values)["d"])
        assert d["A/X"][0] == 1 and d["B/X"][0] == 2


def test_a_write_with_a_missing_field_gets_a_bad_request_ack(make):
    client, fakes, config = make(extra=WRITABLE)
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        connected(client)
        ws.send_json({"op": "write", "id": 5, "tag": "SpeedSP"})
        assert until(ws, lambda m: is_ack(m) and m["id"] == 5) == {
            "op": "ack", "id": 5, "ok": False, "error": "missing value", "kind": "bad_request", "value": None}
        ws.send_json({"op": "momentary", "id": 6, "action": "press"})
        assert until(ws, lambda m: is_ack(m) and m["id"] == 6)["kind"] == "bad_request"
    assert [line["kind"] for line in audit_lines(config, 2)] == ["bad_request", "bad_request"]
    assert client.get("/stats").json()["write_rejects"] == 2
