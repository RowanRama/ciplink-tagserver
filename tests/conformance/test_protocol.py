"""Protocol conformance over a real WebSocket against the Test Bench config (design section 15a)."""

import json
import time
import uuid

import pytest
from websockets.exceptions import ConnectionClosed


def is_values(m):
    return m["op"] == "values"


def audit_lines(server, n):
    """The audit lines once there are at least n (written by a background thread)."""
    end = time.monotonic() + 5
    while True:
        lines = server.audit.read_text(encoding="utf-8").splitlines() if server.audit.exists() else []
        if len(lines) >= n or time.monotonic() > end:
            return [json.loads(line) for line in lines]
        time.sleep(0.02)


def audit_tail(server, *momentary):
    """The last audit lines once their momentary labels are these (a previous test's line may come late)."""
    end = time.monotonic() + 5
    while True:
        tail = audit_lines(server, 0)[-len(momentary):]
        if [line.get("momentary") for line in tail] == list(momentary) or time.monotonic() > end:
            return tail
        time.sleep(0.02)


def stop_bench(client):
    """Bench.Running false (a short press of StopPB), so the speed is 0."""
    assert client.request("momentary", tag="Bench.StopPB", action="press")["ok"]
    assert client.request("momentary", tag="Bench.StopPB", action="release")["ok"]


# hello, welcome and auth

def test_welcome(server):
    c = server.client()
    try:
        w = c.welcome
        assert w["v"] == 1 and w["write"] is True and w["plcs"] == {"PLC1": "connected"}
        assert w["features"] == ["info", "momentary", "readback", "errors"]
    finally:
        c.close()
    if server.viewer:
        c = server.client(server.viewer)
        assert c.welcome["write"] is False
        c.close()


@pytest.mark.parametrize("first", [
    {"op": "hello", "v": 1, "token": "not-a-token"},
    {"op": "sub", "id": 1, "tags": ["Bench.Count"]},
    {"op": "hello", "v": 2, "token": None},
])
def test_auth_failure_closes_4401(server, first):
    c = server.raw()
    if first.get("token", "") is None:
        first["token"] = server.token
    c.send(first)
    with pytest.raises(ConnectionClosed) as err:
        c.recv(5)
    assert err.value.rcvd.code == 4401


# sub snapshots, info and values

def test_info_before_values(client):
    client.sub(1, ["Bench.SpeedSP", "Bench.Count", "Trend", "Tank[1].Temp", "Bench.NoSuchTag"])
    client.wait(lambda: {"Bench.SpeedSP", "Bench.Count", "Bench.NoSuchTag"} <= set(client.values))
    first_values = next(i for i, m in enumerate(client.log) if is_values(m))
    first_info = next(i for i, m in enumerate(client.log) if m["op"] == "info")
    assert first_info < first_values
    sp = client.info["Bench.SpeedSP"]
    assert sp == {"type": "REAL", "writable": True, "rate_ms": 250, "group": None, "min": 0, "max": 1800,
                  "units": "rpm", "description": "Spindle speed setpoint"}
    assert client.info["Bench.Count"] == {"type": "DINT", "writable": False, "rate_ms": 250, "group": None}
    assert client.info["Trend"]["dims"] == [16]
    assert client.info["Tank[1].Temp"]["group"] == "slow" and client.info["Tank[1].Temp"]["rate_ms"] == 2000
    value, quality, t, error = client.values["Bench.Count"]
    assert isinstance(value, int) and quality == "good" and isinstance(t, (int, float)) and error is None
    value, quality, _, error = client.values["Bench.NoSuchTag"]
    assert value is None and quality == "missing" and error["kind"] == "not_found"


def test_sub_snapshot(server, client):
    """A tag already polled for another client is not published again (SpeedSP never changes by itself): a new
    subscriber gets the cached entry right after its ack."""
    client.sub(1, ["Bench.SpeedSP"])
    client.wait(lambda: client.value("Bench.SpeedSP") is not None)
    other = server.client()
    try:
        other.sub(1, ["Bench.SpeedSP"])
        assert other.until(is_values, timeout=2)["d"]["Bench.SpeedSP"] == client.values["Bench.SpeedSP"]
    finally:
        other.close()


def test_viewer_info_not_writable(server):
    if not server.viewer:
        pytest.skip("needs --viewer-token")
    c = server.client(server.viewer)
    try:
        c.sub(1, ["Bench.SpeedSP"])
        c.wait(lambda: "Bench.SpeedSP" in c.info)
        assert c.info["Bench.SpeedSP"]["writable"] is False
    finally:
        c.close()


def test_whole_struct_values(client):
    client.sub(1, ["Bench.Recipe", "Trend[2]", "Trend[3]"])
    client.wait(lambda: all(client.value(n) is not None for n in ("Bench.Recipe", "Trend[2]", "Trend[3]")))
    recipe = client.value("Bench.Recipe")
    assert set(recipe) == {"Name", "Setpoints"} and len(recipe["Setpoints"]) == 4
    assert isinstance(recipe["Name"], str) and isinstance(client.value("Trend[3]"), float)


# aliases and bits

def test_bit_names_and_bits(client):
    names = ["Bench.StatusWord", "Bench.StatusWord.EStop", "Bench.StatusWord.0", "Bench.StatusWord.DoorOpen"]
    client.sub(1, names)
    client.wait(lambda: set(names) <= set(client.values) and set(names) <= set(client.info))
    assert client.info["Bench.StatusWord"]["bits"] == {"EStop": 0, "DoorOpen": 1, "Fault": 2}
    for name in names[1:]:
        assert client.info[name]["type"] == "BOOL"
    # values are keyed as sent; the bits agree with the word once a cycle has delivered all of them
    client.wait(lambda: client.value(names[1]) == client.value(names[2]) == bool(client.value(names[0]) & 1)
                and client.value(names[3]) == bool(client.value(names[0]) & 2))


# deadband

def test_deadband(client):
    """The mock's speed ripples 0.2 around the setpoint; with deadband 0.5 a published change is always >= 0.5."""
    client.sub(1, ["Bench.Speed", "Bench.Running"])
    assert client.request("write", tag="Bench.SpeedSP", value=900.0)["ok"]
    assert client.request("momentary", tag="Bench.StartPB", action="press")["ok"]
    assert client.request("momentary", tag="Bench.StartPB", action="release")["ok"]
    client.wait(lambda: abs((client.value("Bench.Speed") or 0) - 900) < 1)
    seen = [m["d"]["Bench.Speed"] for m in client.drain(2) if is_values(m) and "Bench.Speed" in m["d"]]
    previous = client.value("Bench.Speed") if not seen else None
    for value, quality, *_ in seen:
        if previous is not None and quality == "good":
            assert abs(value - previous) >= 0.5
        previous = value
    assert len(seen) <= 2  # the ripple alone never publishes
    stop_bench(client)


# writes: every check, read-back and audit

@pytest.mark.parametrize("tag, value, kind", [
    ("NoPlc/Bench.SpeedSP", 1.0, "bad_request"),
    ("Tank[0].Level", 1.0, "not_allowed"),
    ("Bench.Count", 1, "not_writable"),
    ("Bench.StatusWord.EStop", True, "not_writable"),
    ("Bench.SpeedSP", 5000, "out_of_range"),
    ("Bench.SpeedSP", -1, "out_of_range"),
    ("Bench.SpeedSP", "fast", "bad_value"),
])
def test_write_rejected(client, tag, value, kind):
    ack = client.request("write", tag=tag, value=value)
    assert ack["ok"] is False and ack["kind"] == kind and isinstance(ack["error"], str) and ack["value"] is None


def test_write_not_allowed_for_viewer(server):
    if not server.viewer:
        pytest.skip("needs --viewer-token")
    c = server.client(server.viewer)
    try:
        ack = c.request("write", tag="Bench.SpeedSP", value=10.0)
        assert ack["ok"] is False and ack["kind"] == "not_allowed" and ack["error"] == "not allowed"
    finally:
        c.close()


def test_write_reads_back(server, client):
    before = len(audit_lines(server, 0)) if server.audit else 0
    client.sub(1, ["Bench.SpeedSP"])
    ack = client.request("write", tag="Bench.SpeedSP", value=1234.5)
    assert ack == {"op": "ack", "id": ack["id"], "ok": True, "error": None, "kind": None, "value": 1234.5}
    client.wait(lambda: client.value("Bench.SpeedSP") == 1234.5)
    stats = server.get("/stats")
    assert stats["writes"] >= 1 and stats["write_rejects"] >= 0
    if server.audit:
        line = audit_lines(server, before + 1)[-1]
        assert line["tag"] == "PLC1/Bench.SpeedSP" and line["value"] == 1234.5 and line["ok"] is True
        assert line["kind"] is None and line["token"] == "hmi" and "momentary" not in line


# momentary

def test_momentary_press_hold_release(server, client):
    client.sub(1, ["Bench.StartPB", "Bench.Running"])
    stop_bench(client)
    ack = client.request("momentary", tag="Bench.StartPB", action="press")
    assert ack["ok"] is True and ack["kind"] is None and ack["value"] is True
    end = time.monotonic() + 1.5  # longer than momentary_timeout_ms: the holds keep it pressed
    while time.monotonic() < end:
        client.send({"op": "momentary", "tag": "Bench.StartPB", "action": "hold"})  # no id, no ack
        client.drain(0.3)
        assert client.value("Bench.StartPB") is True
    assert client.value("Bench.Running") is True
    ack = client.request("momentary", tag="Bench.StartPB", action="release")
    assert ack["ok"] is True and ack["value"] is False
    client.wait(lambda: client.value("Bench.StartPB") is False)
    assert not any(m["op"] == "ack" and m.get("id") is None for m in client.log)
    stop_bench(client)


def test_momentary_watchdog(server, client):
    client.sub(1, ["Bench.StartPB"])
    before = server.get("/stats")["momentary_timeouts"]
    assert client.request("momentary", tag="Bench.StartPB", action="press")["ok"]
    client.wait(lambda: client.value("Bench.StartPB") is True)
    start = time.monotonic()
    client.wait(lambda: client.value("Bench.StartPB") is False, timeout=3)
    assert time.monotonic() - start < 2
    assert server.get("/stats")["momentary_timeouts"] == before + 1
    if server.audit:
        assert [l.get("momentary") for l in audit_tail(server, "press", "timeout")] == ["press", "timeout"]
    stop_bench(client)


def test_momentary_released_on_disconnect(server, client):
    client.sub(1, ["Bench.StopPB"])
    other = server.client()
    assert other.request("momentary", tag="Bench.StopPB", action="press")["ok"]
    client.wait(lambda: client.value("Bench.StopPB") is True)
    other.close()
    client.wait(lambda: client.value("Bench.StopPB") is False, timeout=2)
    if server.audit:
        assert audit_tail(server, "press", "disconnect")[-1]["momentary"] == "disconnect"


def test_momentary_needs_bool(client):
    ack = client.request("momentary", tag="Bench.SpeedSP", action="press")
    assert ack["ok"] is False and ack["kind"] == "bad_request"
    client.send({"op": "momentary", "id": 7, "tag": "Bench.StartPB", "action": "wiggle"})
    assert client.until(lambda m: m["op"] == "error")["id"] == 7


# page learning

def test_page_learning(server, client):
    page = f"/conformance-{uuid.uuid4().hex[:8]}"
    client.sub(1, ["Bench.Count", "Bench.Message"], tier=1, page=page)
    other = server.client()
    try:
        other.sub(1, page=page, tier=2)
        other.wait(lambda: {"Bench.Count", "Bench.Message"} <= set(other.values))
    finally:
        other.close()


# groups only at tier 1

def test_group_only_at_tier1(server, client):
    fast = lambda: server.get("/stats")["plcs"]["PLC1"]["groups"]["fast"]  # noqa: E731
    base = fast()["tags"]
    client.sub(1, ["Bench.Axis.Position", "Bench.Axis.Velocity"])
    end = time.monotonic() + 3
    while not (fast()["tags"] == base + 2 and (fast()["last_period_ms"] or 1000) < 150):
        assert time.monotonic() < end, fast()
        time.sleep(0.1)
    assert client.info["Bench.Axis.Position"]["group"] == "fast" and client.info["Bench.Axis.Position"]["rate_ms"] == 50
    client.sub(1, ["Bench.Axis.Position", "Bench.Axis.Velocity"], tier=2)
    end = time.monotonic() + 3
    while fast()["tags"] != base:
        assert time.monotonic() < end, fast()
        time.sleep(0.1)


def test_always_polled(server):
    """Bench.StatusWord is `always`: at tier 1 with no subscriber at all."""
    tags = server.get("/stats")["plcs"]["PLC1"]
    assert tags["tier1"] >= 1


# stale on disconnect, meta on reload (need control of the driver)

def test_stale_on_disconnect(server, client):
    server.local()
    client.sub(1, ["Bench.Count"])
    client.wait(lambda: client.values.get("Bench.Count", [0, ""])[1] == "good")
    kept = client.value("Bench.Count")
    server.mock.fail = True
    try:
        client.until(lambda m: m["op"] == "status" and m["plcs"]["PLC1"] == "disconnected")
        client.wait(lambda: client.values["Bench.Count"][1] == "stale")
        value, _, _, error = client.values["Bench.Count"]
        assert value is not None and error is None and abs(value - kept) < 5
        ack = client.request("write", tag="Bench.SpeedSP", value=1.0)
        assert ack["ok"] is False and ack["kind"] in ("refused", "comm")
    finally:
        server.mock.fail = False
    client.until(lambda m: m["op"] == "status" and m["plcs"]["PLC1"] == "connected", timeout=10)
    client.wait(lambda: client.values["Bench.Count"][1] == "good")


def test_meta_on_reload(server, client):
    server.local()
    client.sub(1, ["Bench.SpeedSP"])
    client.wait(lambda: "Bench.SpeedSP" in client.info)
    version = server.get("/tags", server.token)["version"]
    server.mock.version += 1
    client.until(lambda m: m["op"] == "meta" and m["plc"] == "PLC1")
    client.until(lambda m: m["op"] == "info" and "Bench.SpeedSP" in m["d"])  # info is sent again after a reload
    assert server.get("/tags", server.token)["version"] == version + 1
