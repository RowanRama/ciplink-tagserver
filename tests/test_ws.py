import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import hello, until, wait_for


def is_values(m):
    return m["op"] == "values"


def test_welcome(make):
    client, fakes, _ = make()
    with client.websocket_connect("/ws") as ws:
        welcome = hello(ws)
        assert welcome["v"] == 1 and welcome["write"] is True and set(welcome["plcs"]) == {"PLC1"}
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


def test_write_allowed_denied_and_audit(make, tmp_path):
    client, fakes, config = make()
    fake = fakes["PLC1"]
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")
        ws.send_json({"op": "write", "id": 1, "tag": "SpeedSP", "value": 42})
        assert until(ws, lambda m: m["op"] == "ack") == {"op": "ack", "id": 1, "ok": True, "error": None}
        assert fake.writes == [("SpeedSP", 42)]
        ws.send_json({"op": "write", "id": 2, "tag": "PLC1/program:main.DoorOpen", "value": True})
        assert until(ws, lambda m: m["op"] == "ack")["ok"]  # patterns match case-insensitively
        ws.send_json({"op": "write", "id": 3, "tag": "Other", "value": 1})
        assert until(ws, lambda m: m["op"] == "ack") == {"op": "ack", "id": 3, "ok": False, "error": "not allowed"}
    assert len(fake.writes) == 2

    with client.websocket_connect("/ws") as ws:
        hello(ws, "view")
        ws.send_json({"op": "write", "id": 1, "tag": "SpeedSP", "value": 1})
        assert until(ws, lambda m: m["op"] == "ack")["error"] == "not allowed"

    lines = [json.loads(line) for line in open(config.audit_log, encoding="utf-8")]
    assert [(l["token"], l["tag"], l["value"], l["ok"], l["error"]) for l in lines] == [
        ("hmi1", "PLC1/SpeedSP", 42, True, None),
        ("hmi1", "PLC1/program:main.DoorOpen", True, True, None),
        ("hmi1", "PLC1/Other", 1, False, "not allowed"),
        ("viewer", "PLC1/SpeedSP", 1, False, "not allowed"),
    ]
    assert all("time" in l for l in lines)


def test_write_ack_with_driver_error(make):
    client, fakes, config = make()
    fakes["PLC1"].write_error = "Privilege violation"
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")
        ws.send_json({"op": "write", "id": 9, "tag": "SpeedSP", "value": 1})
        assert until(ws, lambda m: m["op"] == "ack") == {
            "op": "ack", "id": 9, "ok": False, "error": "Privilege violation"}
    line = json.loads(open(config.audit_log, encoding="utf-8").read().splitlines()[-1])
    assert line["ok"] is False and line["error"] == "Privilege violation"


def test_write_while_disconnected(make):
    client, fakes, _ = make(setup=lambda f: setattr(f["PLC1"], "fail", True))
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "write", "id": 1, "tag": "SpeedSP", "value": 1})
        assert until(ws, lambda m: m["op"] == "ack")["error"] == "PLC not connected"
    assert fakes["PLC1"].writes == []


def test_disconnect_marks_stale_then_reconnects(make):
    client, fakes, _ = make()
    fake = fakes["PLC1"]
    fake.values["Speed"] = 3
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Speed"]})
        assert until(ws, is_values)["d"]["Speed"][:2] == [3, "good"]

        fake.fail = True
        assert until(ws, is_values)["d"]["Speed"][:2] == [3, "stale"]  # value kept
        assert until(ws, lambda m: m["op"] == "status")["plcs"] == {"PLC1": "disconnected"}

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
