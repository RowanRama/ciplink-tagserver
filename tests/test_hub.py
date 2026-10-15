"""The hub against a fake worker that implements the worker side of the hub contract."""

import asyncio
import json
import os
from concurrent.futures import Future

import pytest

from ciplink_tagserver import server
from ciplink_tagserver.config import parse_config
from ciplink_tagserver.server import Hub, Session

from conftest import FakeDriver, FakePLC, wait_for


class FakeWorker:
    """The worker members the hub uses: set_tier, snapshot, write, type_info, description, version, state."""

    def __init__(self, cfg, factory, cycle_ms, on_values=None, on_status=None, on_meta=None, rules=None,
                 on_info=None, **kwargs):
        self.name, self.rules, self.on_info = cfg.name, rules, on_info
        self.state, self.version = "connected", 1
        self.tiers, self.cache, self.writes = {}, {}, []
        self.types, self.descriptions = {}, {}
        self.result = (True, None, None, None)

    def start(self):
        pass

    def stop(self):
        pass

    def set_tier(self, tag, tier):
        if tier is None:
            self.tiers.pop(tag, None)
        else:
            self.tiers[tag] = tier

    def snapshot(self, tags):
        return {tag: list(self.cache[tag]) for tag in tags if tag in self.cache}

    def write(self, tag, value):
        self.writes.append((tag, value))
        future = Future()
        future.set_result(self.result)
        return future

    def type_info(self, tag):
        return dict(self.types.get(tag, {"type": None}))

    def description(self, tag):
        return self.descriptions.get(tag)


CONFIG = {
    "server": {"cycle_ms": 100, "momentary_timeout_ms": 100, "pages_file": ""},
    "plc": {"P": {"path": "x", "rule": [
        {"match": "SpeedSP", "writable": True, "min": 0, "max": 1800, "units": "rpm", "description": "Setpoint"},
        {"match": "Speed", "deadband": 0.5, "units": "rpm"},
        {"match": "Word", "writable": True, "bits": {"EStop": 0, "Door": 1}, "always": True},
        {"match": "Recipe", "writable": True, "min": 0, "max": 10},
        {"match": ["Start*", "Mode"], "writable": True},
        {"match": "Locked", "max": 5},
    ], "group": [{"name": "fast", "rate_ms": 20, "match": "Speed"}]}},
    "token": {"op": {"secret": "s", "write": ["P/SpeedSP", "P/Word.EStop", "P/Start*", "P/Recipe", "P/Mode",
                                                "P/Locked", "P/Nope"]},
              "viewer": {"secret": "v"}},
}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PlcWorker", FakeWorker)
    config = parse_config(CONFIG, tmp_path)
    hub = Hub(config, factory=None)
    hub.worker = hub.workers["P"]
    hub.retry_delay = 0.1
    yield hub
    hub.stop()


def run(hub, coro_fn):
    async def main():
        hub.start(asyncio.get_running_loop())
        return await coro_fn()
    return asyncio.run(main())


def session(hub, token="op"):
    s = Session(hub.config.tokens[token])
    hub.sessions.add(s)
    return s


def audit_lines(hub, n):
    wait_for(lambda: hub.config.audit_log and len(open(hub.config.audit_log).read().splitlines()) >= n)
    return [json.loads(line) for line in open(hub.config.audit_log, encoding="utf-8")]


def test_always_tags_get_a_permanent_tier1_reference(hub):
    assert hub.worker.tiers == {"Word": 1}
    s = Session(hub.config.tokens["op"])
    hub.subscribe(s, 1, ["Word"], tier=3)
    hub.unsubscribe(s, 1)
    assert hub.worker.tiers == {"Word": 1} and hub.refs["P/Word"] == {1: 1}


def test_write_checks_kinds_and_audit(hub):
    hub.worker.cache["SpeedSP"] = [100, "good", 1, None]
    hub.worker.result = (True, None, None, 250.0)
    s = session(hub)

    async def go():
        return [await hub.write(s, name, value) for name, value in [
            ("SpeedSP", 250), ("Q/SpeedSP", 1), (5, 1), ("Other", 1), ("Locked", 1), ("SpeedSP", 1801),
            ("SpeedSP", -1), ("Recipe", {"A": [1, 11]}), ("Recipe", {"A": [1, True, 10], "B": "x"}),
            ("Word.EStop", True), ("Word.0", True)]]

    results = run(hub, go)
    assert results[0] == (True, None, None, 250.0)  # ack value is the read-back
    assert results[1][:3] == (False, "unknown PLC 'Q'", "bad_request")
    assert results[2][2] == "bad_request"
    assert results[3] == (False, "not allowed", "not_allowed", None)
    assert results[4] == (False, "not writable", "not_writable", None)
    assert results[5] == results[6] == results[7] == (False, "out of range", "out_of_range", None)
    assert results[8][0]  # bools and strings are not range checked
    assert results[9][0]  # the pattern matches the name as sent
    assert results[10] == (False, "not allowed", "not_allowed", None)  # ... or as resolved, neither here
    assert hub.worker.writes == [("SpeedSP", 250), ("Recipe", {"A": [1, True, 10], "B": "x"}), ("Word.0", True)]
    assert (hub.writes, hub.write_rejects) == (3, 8)

    hub.stop()  # drains the audit queue
    lines = audit_lines(hub, 11)
    assert lines[0] == {"time": lines[0]["time"], "token": "op", "tag": "P/SpeedSP", "old": 100, "value": 250,
                        "ok": True, "error": None, "kind": None}
    assert lines[1]["tag"] == "Q/SpeedSP" and lines[1]["kind"] == "bad_request"
    assert lines[9]["tag"] == "P/Word.0"  # resolved
    assert all("momentary" not in line for line in lines)


def test_worker_error_reaches_the_ack(hub):
    hub.worker.result = (False, "Value out of range for DINT", "bad_value", None)
    s = session(hub)
    assert run(hub, lambda: hub.write(s, "Mode", 1 << 40)) == (False, "Value out of range for DINT", "bad_value", None)
    assert hub.write_rejects == 0 and hub.writes == 1


def test_momentary_press_hold_timeout(hub):
    s = session(hub)

    async def go():
        assert (await hub.momentary(s, "StartPB", "press"))[0]
        for _ in range(4):  # holds keep it pressed past the 100 ms timeout
            await asyncio.sleep(0.02)
            assert await hub.momentary(s, "StartPB", "hold") is None
        assert hub.worker.writes == [("StartPB", True)]
        await asyncio.sleep(0.25)  # no hold: the watchdog releases it
        assert hub.worker.writes == [("StartPB", True), ("StartPB", False)]
        assert s.pressed == {}

    run(hub, go)
    assert hub.momentary_timeouts == 1
    hub.stop()
    lines = audit_lines(hub, 2)
    assert [(line["value"], line["momentary"]) for line in lines] == [(True, "press"), (False, "timeout")]


def test_momentary_release_and_disconnect(hub):
    s = session(hub)
    hub.worker.types["StartPB"] = {"type": "BOOL"}
    hub.worker.types["Mode"] = {"type": "DINT"}

    async def go():
        await hub.momentary(s, "StartPB", "press")
        assert (await hub.momentary(s, "StartPB", "release"))[0]
        await hub.momentary(s, "StartPB", "press")
        await hub.momentary(s, "Word.EStop", "press")  # a bit: BOOL
        assert set(s.pressed) == {"P/StartPB", "P/Word.0"}
        assert await hub.momentary(s, "Mode", "press") == (False, "momentary needs a BOOL tag or a bit",
                                                            "bad_request", None)
        assert (await hub.momentary(s, "Locked", "press"))[2] == "not_writable"
        assert "P/Locked" not in s.pressed  # rejected: no watchdog
        hub.hold(s, "Start2")  # not pressed: ignored
        with pytest.raises(ValueError):
            await hub.momentary(s, "StartPB", "push")
        hub.drop(s)
        await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)

    run(hub, go)
    assert hub.worker.writes == [("StartPB", True), ("StartPB", False), ("StartPB", True), ("Word.0", True),
                                 ("StartPB", False), ("Word.0", False)]
    assert hub.momentary_timeouts == 0
    hub.stop()
    lines = audit_lines(hub, 7)
    assert [line["momentary"] for line in lines][-2:] == ["disconnect", "disconnect"]


def test_info_once_before_values_and_aliases_share_a_tag(hub):
    w = hub.worker
    w.types.update({"SpeedSP": {"type": "REAL"}, "Word": {"type": "DINT"}, "Trend": {"type": "REAL", "dims": [16]}})
    w.descriptions.update({"SpeedSP": "from the controller", "Trend": "Trend data"})
    w.cache.update({"Word": [5, "good", 1, None], "Word.0": [True, "good", 1, None]})
    s, v = session(hub), session(hub, "viewer")

    hub.subscribe(s, 1, ["SpeedSP", "Speed", "Word", "Word.EStop", "Word.0", "Trend", "Nope"])
    info = s.control.popleft()
    assert info["op"] == "info" and not s.control
    d = info["d"]
    assert d["SpeedSP"] == {"type": "REAL", "writable": True, "rate_ms": 100, "group": None, "min": 0, "max": 1800,
                            "units": "rpm", "description": "Setpoint"}  # the rule description wins
    assert d["Speed"] == {"type": None, "writable": False, "rate_ms": 20, "group": "fast", "units": "rpm",
                          "deadband": 0.5}
    assert d["Word"] == {"type": "DINT", "writable": False, "rate_ms": 100, "group": None,
                         "bits": {"EStop": 0, "Door": 1}}  # Word itself matches no token pattern
    assert d["Word.EStop"] == {"type": "BOOL", "writable": True, "rate_ms": 100, "group": None}
    assert d["Word.0"] == {"type": "BOOL", "writable": False, "rate_ms": 100, "group": None}
    assert d["Trend"] == {"type": "REAL", "dims": [16], "writable": False, "rate_ms": 100, "group": None,
                          "description": "Trend data"}
    assert s.pending == {"Word": [5, "good", 1, None], "Word.EStop": [True, "good", 1, None],
                         "Word.0": [True, "good", 1, None]}
    assert hub.refs["P/Word.0"] == {1: 1}  # two names, one reference
    assert hub.subscribers["P/Word.0"] == {s: {"Word.EStop", "Word.0"}}

    hub.subscribe(v, 1, ["SpeedSP"])
    assert v.control.popleft()["d"]["SpeedSP"]["writable"] is False  # per token
    hub.subscribe(s, 2, ["SpeedSP"])  # already known to this session: no info again
    assert not s.control

    s.pending.clear()
    hub._fan_values("P", {"Word.0": [False, "good", 2, None], "Other": [1, "good", 2, None]})
    hub._fan_values("P", {"Word.0": [True, "good", 3, None]})
    assert s.pending == {"Word.EStop": [True, "good", 3, None], "Word.0": [True, "good", 3, None]}  # coalesced

    hub.unsubscribe(s, 1)
    assert "P/Word.0" not in hub.subscribers and "P/Word.0" not in hub.refs
    assert hub.subscribers["P/SpeedSP"] == {v: {"SpeedSP"}, s: {"SpeedSP"}}
    assert hub.refs["P/Word"] == {1: 1}  # always


def test_replacing_a_subscription_keeps_the_index(hub):
    s = session(hub)
    hub.subscribe(s, 1, ["Speed", "P/Speed"])
    hub.subscribe(s, 1, ["Speed"])
    assert hub.subscribers["P/Speed"] == {s: {"Speed"}} and hub.refs["P/Speed"] == {1: 1}
    assert hub.worker.tiers["Speed"] == 1


def test_info_sent_again_on_meta_connect_and_descriptions(hub):
    s = session(hub)
    hub.subscribe(s, 1, ["Speed", "Word.EStop"])
    s.control.clear()
    hub.worker.types["Speed"] = {"type": "REAL"}
    hub._status("P", "connected")
    assert s.control.popleft() == {"op": "status", "plcs": {"P": "connected"}}
    assert s.control.popleft()["d"]["Speed"]["type"] == "REAL"
    hub._status("P", "disconnected")
    assert [m["op"] for m in s.control] == ["status"]
    s.control.clear()
    hub._meta("P")
    assert s.control.popleft() == {"op": "meta", "plc": "P"}
    assert set(s.control.popleft()["d"]) == {"Speed", "Word.EStop"}
    hub.worker.descriptions["Speed"] = "Spindle speed"
    hub._send_info("P", {"Speed"})
    assert s.control.popleft()["d"] == {"Speed": {"type": "REAL", "writable": False, "rate_ms": 20, "group": "fast",
                                                  "units": "rpm", "deadband": 0.5, "description": "Spindle speed"}}


def test_sender_sends_control_in_order_then_one_values_message(hub):
    sent = []

    async def go():
        s = Session(hub.config.tokens["op"])
        s.push("A", [1, "good", 1, None])
        s.send({"op": "ack", "id": 1})
        s.push("A", [2, "good", 2, None])
        s.push("B", [3, "good", 2, None])
        s.send({"op": "info", "d": {}})

        async def send_json(msg):
            sent.append(msg)

        task = asyncio.create_task(s.run_sender(send_json))
        await asyncio.sleep(0.01)
        task.cancel()

    run(hub, go)
    assert sent == [{"op": "ack", "id": 1}, {"op": "info", "d": {}},
                    {"op": "values", "d": {"A": [2, "good", 2, None], "B": [3, "good", 2, None]}}]


def test_a_press_handled_after_its_session_dropped_is_released_at_once(hub):
    s = session(hub)

    async def go():
        task = asyncio.create_task(hub.momentary(s, "StartPB", "press"))
        hub.drop(s)  # the close came in before the press task ran
        await task

    run(hub, go)
    assert hub.worker.writes == [("StartPB", True), ("StartPB", False)] and s.pressed == {}
    hub.stop()
    assert [line["momentary"] for line in audit_lines(hub, 2)] == ["press", "disconnect"]
    assert hub.momentary_timeouts == 0


def test_a_release_lost_to_comm_is_sent_again_on_connect(hub):
    s = session(hub)

    async def go():
        await hub.momentary(s, "StartPB", "press")
        hub.worker.state = "disconnected"
        hub.worker.result = (False, "PLC not connected", "refused", None)
        await asyncio.sleep(0.2)  # the watchdog's release is refused
        assert hub.owed == {"P/StartPB": (s, "StartPB", "timeout")}
        await asyncio.sleep(0.2)  # not retried while disconnected
        assert len(hub.worker.writes) == 2
        hub.worker.state = "connected"
        hub.worker.result = (True, None, None, False)
        hub._status("P", "connected")
        assert hub.owed == {}
        hub.drop(s)

    run(hub, go)
    assert hub.worker.writes == [("StartPB", True), ("StartPB", False), ("StartPB", False)]
    hub.stop()
    lines = audit_lines(hub, 3)
    assert [(line["ok"], line["momentary"]) for line in lines] == [(True, "press"), (False, "timeout"),
                                                                  (True, "timeout")]


def test_shutdown_still_writes_and_audits_the_release_of_a_pressed_button(tmp_path):
    fake = FakePLC()
    config = parse_config({"server": {"cycle_ms": 50, "pages_file": ""},
                           "plc": {"P": {"path": "x", "rule": [{"match": "StartPB", "writable": True}]}},
                           "token": {"op": {"secret": "s", "write": ["P/StartPB"]}}}, tmp_path)
    hub = Hub(config, lambda cfg: FakeDriver(fake))

    async def go():
        hub.start(asyncio.get_running_loop())
        await asyncio.to_thread(wait_for, lambda: hub.workers["P"].state == "connected")
        s = session(hub)
        assert (await hub.momentary(s, "StartPB", "press"))[0]
        hub.stop()  # the session is still open: its release is queued and the worker runs it before it stops

    asyncio.run(go())
    assert fake.writes == [("StartPB", True), ("StartPB", False)]
    assert [line["momentary"] for line in audit_lines(hub, 2)] == ["press", "disconnect"]


def test_audit_off_and_the_audit_thread_survives_any_error(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PlcWorker", FakeWorker)
    off = Hub(parse_config({**CONFIG, "server": {**CONFIG["server"], "audit_log": ""}}, tmp_path), None)
    s = session(off)
    run(off, lambda: off.write(s, "SpeedSP", 1))
    assert off.config.audit_log is None and off._audit.qsize() == 0
    off.stop()

    hub = Hub(parse_config(CONFIG, tmp_path), None)
    path, hub.config.audit_log = hub.config.audit_log, 1.5  # open() raises TypeError
    s = session(hub)
    run(hub, lambda: hub.write(s, "SpeedSP", 1))
    wait_for(lambda: hub._audit.qsize() == 0)
    hub.config.audit_log = path
    hub.audit("op", "P/SpeedSP", None, 2, (True, None, None, 2))
    wait_for(lambda: os.path.exists(path))
    assert audit_lines(hub, 1)[0]["value"] == 2 and hub._audit_thread.is_alive()
    hub.stop()


def test_info_is_sent_again_for_a_name_subscribed_again(hub):
    s = session(hub)
    hub.worker.state = "disconnected"
    hub.subscribe(s, 1, ["Speed"])
    assert s.control.popleft()["d"]["Speed"]["type"] is None
    hub.unsubscribe(s, 1)
    hub.worker.types["Speed"] = {"type": "REAL"}
    hub._status("P", "connected")
    s.control.clear()
    hub.subscribe(s, 2, ["Speed"])
    assert s.control.popleft()["d"]["Speed"]["type"] == "REAL"


def test_writes_with_an_element_count_or_no_value_are_bad_requests(hub):
    s = session(hub)

    async def go():
        return [await hub.write(s, name, value) for name, value in
                [("SpeedSP{1}", 5000), ("SpeedSP", None), ("StartPB{1}", True)]]

    assert [r[:3] for r in run(hub, go)] == [
        (False, "element counts are not allowed in writes", "bad_request"),
        (False, "missing value", "bad_request"),
        (False, "element counts are not allowed in writes", "bad_request")]
    assert hub.worker.writes == [] and hub.write_rejects == 3


@pytest.mark.parametrize("kind", ["refused", "table_changed"])
def test_a_release_failing_while_connected_is_retried(hub, kind):
    s = session(hub)

    async def go():
        await hub.momentary(s, "StartPB", "press")
        hub.worker.result = (False, "controller busy", kind, None)  # ciplink's reload backoff: still connected
        assert (await hub.momentary(s, "StartPB", "release"))[2] == kind
        await asyncio.sleep(0.15)  # retried after retry_delay, failing again
        assert hub.worker.writes[-1] == ("StartPB", False) and len(hub.worker.writes) == 3
        hub.worker.result = (True, None, None, False)
        await asyncio.sleep(0.25)
        assert hub.owed == {}

    run(hub, go)
    assert hub.worker.writes == [("StartPB", True)] + [("StartPB", False)] * 3


def test_an_owed_release_is_not_sent_once_the_tag_is_pressed_again(hub):
    s = session(hub)

    async def go():
        await hub.momentary(s, "StartPB", "press")
        hub.worker.result = (False, "controller busy", "refused", None)
        await hub.momentary(s, "StartPB", "release")
        hub.worker.result = (True, None, None, True)
        await hub.momentary(s, "StartPB", "press")  # forgets the owed release
        await asyncio.sleep(0.15)
        hub.drop(s)

    run(hub, go)
    assert hub.worker.writes == [("StartPB", True), ("StartPB", False), ("StartPB", True), ("StartPB", False)]


def test_status_resend_tolerates_an_owed_entry_popped_meanwhile(hub):
    s = session(hub)
    hub.owed = {"P/StartPB": (s, "StartPB", "release"), "P/StartX": (s, "StartX", "release")}
    real_submit = hub._submit

    def submit(*args):  # a worker thread pops the other entry while the first is resent
        hub.owed.pop("P/StartX", None) if args[1] == "StartPB" else hub.owed.pop("P/StartPB", None)
        return real_submit(*args)

    hub._submit = submit

    async def go():
        hub._status("P", "connected")

    run(hub, go)
    assert len(hub.worker.writes) == 1 and hub.owed == {}


def test_a_learned_page_of_a_renamed_plc_and_drop_always_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PlcWorker", FakeWorker)
    pages = tmp_path / "pages.json"
    pages.write_text(json.dumps({"/main": ["OLD/Speed", "P/SpeedSP", "junk"]}))  # OLD was renamed to P
    hub = Hub(parse_config({**CONFIG, "server": {**CONFIG["server"], "pages_file": str(pages)}}, tmp_path), None)
    assert hub.pages == {"/main": {"P/SpeedSP"}}
    s = session(hub)

    async def go():
        hub.subscribe(s, 1, None, 1, "/main")
        await hub.momentary(s, "StartPB", "press")
        hub.refs.clear()  # whatever breaks an unsubscribe, drop still releases and forgets the session
        with pytest.raises(KeyError):
            hub.drop(s)

    run(hub, go)
    assert s not in hub.sessions and s.pressed == {}
    assert hub.workers["P"].writes == [("StartPB", True), ("StartPB", False)]
    hub.stop()


def test_index_spelling_does_not_bypass_element_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PlcWorker", FakeWorker)
    rules = [{"match": "Tank[2].Level", "writable": False}, {"match": "Tank[3].Level", "max": 5},
             {"match": "Tank[*].Level", "writable": True}]
    config = parse_config({"server": CONFIG["server"], "plc": {"P": {"path": "x", "rule": rules}},
                           "token": {"op": {"secret": "s", "write": ["P/*"]}}}, tmp_path)
    hub = Hub(config, None)
    s = session(hub)

    async def go():
        return [(await hub.write(s, name, value))[2] for name, value in
                [("Tank[02].Level", 1.0), ("Tank[ 2].Level", 1.0), ("Tank[03].Level", 9.0), ("Tank[+3].Level", 4.0)]]

    assert run(hub, go) == ["not_writable", "not_writable", "out_of_range", None]
    assert hub.workers["P"].writes == [("Tank[3].Level", 4.0)]
    hub.stop()


def test_an_array_written_by_name_is_refused(hub):
    s = session(hub)
    hub.worker.types.update({"Recipe": {"type": "REAL", "dims": [4]}, "StartPB": {"type": "BOOL", "dims": [64]}})

    async def go():
        return [await hub.write(s, "Recipe", 99.0), await hub.momentary(s, "StartPB", "press")]

    assert [r[:3] for r in run(hub, go)] == [(False, "write an element of the array", "bad_request")] * 2
    assert hub.worker.writes == []
