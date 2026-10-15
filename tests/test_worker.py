import time

import pytest
from ciplink import CommError, ErrorKind, Tag, TagError

from ciplink_tagserver.config import PlcConfig, parse_config
from ciplink_tagserver.rules import Rules
from ciplink_tagserver.values import type_map
from ciplink_tagserver.worker import PlcWorker

from conftest import FakeDriver, FakePLC, hello, plant, tag_def, udt, until, wait_for
from test_tiers import poll


def test_reconnect_backoff_schedule(monkeypatch):
    monkeypatch.setattr(PlcWorker, "backoff", (1, 2, 5, 10))
    fake = FakePLC()
    fake.fail = True
    states = []
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 250,
                       on_status=lambda plc, state: states.append(state))
    delays = []

    def fake_wait(seconds):  # a fake clock: record the delay and return at once
        delays.append(seconds)
        if len(delays) == 6:
            fake.fail = False  # the 7th attempt connects
        return False

    worker.wait = fake_wait
    worker.start()
    wait_for(lambda: worker.state == "connected")
    worker.stop()
    assert delays[:6] == [1, 2, 5, 10, 10, 10]
    assert fake.opens == 7
    assert worker.reconnects == 6
    assert states[:3] == ["disconnected", "connecting", "disconnected"]


def test_backoff_restarts_after_a_good_connection(monkeypatch):
    monkeypatch.setattr(PlcWorker, "backoff", (1, 2, 5, 10))
    fake = FakePLC()
    fake.fail = True
    fake.values["T"] = 1  # read every cycle, so the lost connection is noticed
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 10)
    worker.add_tags(["T"])
    delays = []

    def fake_wait(seconds):
        if seconds >= 1:  # a backoff wait; cycle waits are under 10 ms
            delays.append(seconds)
            if len(delays) == 2:
                fake.fail = False
            return len(delays) == 3  # stop the worker
        fake.fail = True  # lose the connection after one good cycle
        return False

    worker.wait = fake_wait
    worker.start()
    worker._thread.join(5)
    assert delays == [1, 2, 1]


def test_cycle_stats_and_overruns():
    fake = FakePLC()
    fake.values["T"] = 1
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 0)  # every cycle overruns
    worker.add_tags(["T", "U"])
    worker.start()
    wait_for(lambda: worker.overruns > 3)
    worker.stop()
    assert worker.reads_per_cycle == 1  # U doesn't exist, so it is skipped after its first read
    assert worker.last_cycle_ms is not None
    assert worker.snapshot(["T", "U"]) and worker.snapshot(["T"])["T"][:2] == [1, "good"]


def test_missing_tags_are_skipped_until_retry_or_reload(monkeypatch):
    fake = FakePLC()
    fake.values["T"] = 1
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 10)
    worker.add_tags(["T", "Gone"])
    worker.start()
    wait_for(lambda: len(fake.reads) > 5)
    assert sum("Gone" in r for r in fake.reads) == 1  # asked once, then skipped
    assert worker.snapshot(["Gone"])["Gone"][1] == "missing"

    fake.values["Gone"] = 7  # created online: the tag table reloads
    fake.version = 2
    wait_for(lambda: worker.snapshot(["Gone"])["Gone"][:2] == [7, "good"])

    monkeypatch.setattr(PlcWorker, "missing_retry", 0.05)
    del fake.values["Gone"]  # deleted without a reload being seen: retried after missing_retry
    wait_for(lambda: worker.snapshot(["Gone"])["Gone"][1] == "missing")
    fake.values["Gone"] = 8
    wait_for(lambda: worker.snapshot(["Gone"])["Gone"][:2] == [8, "good"])
    worker.stop()


def make_worker(fake, rules=(), driver=None, cycle_ms=100, **kw):
    """A worker with rules for PLC P, and the driver poll() runs it on; published entries go to worker.out."""
    config = parse_config({"plc": {"P": {"path": "x", "rule": list(rules)}}})
    out = []
    worker = PlcWorker(config.plcs["P"], None, cycle_ms, on_values=lambda plc, changes: out.append(changes),
                       rules=Rules(config), **kw)
    worker.out = out
    return worker, driver or FakeDriver(fake)


def published(worker, tag):
    return [changes[tag] for changes in worker.out if tag in changes]


def test_deadband_against_the_last_published_value():
    fake = FakePLC()
    fake.values.update({"Speed": 10.0, "Flag": True})
    worker, driver = make_worker(fake, [{"match": ["Speed", "Flag"], "deadband": 0.5}])
    worker.add_tags(["Speed", "Flag"])
    seen = []

    def step(**values):
        def run(w):
            seen.append(w.snapshot(["Speed"])["Speed"][0])
            fake.values.update(values)
        return run

    def fail(w):
        fake.errors["Speed"] = TagError("lost reply", ErrorKind.COMM)

    def recover(w):
        del fake.errors["Speed"]
        fake.values["Speed"] = 10.7

    poll(worker, driver, 6, at={1: step(Speed=10.3), 2: step(Speed=10.6, Flag=False), 3: fail, 4: recover})
    assert seen == [10.0, 10.3]  # the cache holds the latest value even when it is not published
    speeds = [(e[0], e[1], e[3]) for e in published(worker, "Speed")]
    assert speeds == [
        (10.0, "good", None),
        (10.6, "good", None),  # 10.3 is within 0.5 of 10.0, 10.6 is not
        (10.6, "bad", {"kind": "comm", "message": "lost reply"}),  # bad keeps the last value
        (10.7, "good", None),  # a quality change always publishes
    ]
    assert [e[0] for e in published(worker, "Flag")] == [True, False]  # bools have no deadband


def test_missing_has_no_value_and_stale_clears_the_error():
    fake = FakePLC()
    fake.values["T"] = 1
    fake.errors["Bad"] = TagError("busy", ErrorKind.REFUSED)
    worker, driver = make_worker(fake)
    worker.add_tags(["T", "Nope", "Bad"])
    poll(worker, driver, 1)
    snap = worker.snapshot(["T", "Nope", "Bad"])
    assert snap["Nope"][:2] == [None, "missing"] and snap["Nope"][3]["kind"] == "not_found"
    assert snap["Bad"][1] == "bad" and "Bad" not in worker.missing  # transient: retried next tick
    worker._mark_stale()
    assert all(e[1] == "stale" and e[3] is None for e in worker.snapshot(["T", "Nope", "Bad"]).values())
    assert worker.snapshot(["T"])["T"][0] == 1


class LogDriver(FakeDriver):
    """Logs reads and writes in order; on_read runs once, at the first read."""

    connection_size = 120  # a few tags per packet, so a cycle reads in several chunks

    def __init__(self, plc, events):
        super().__init__(plc)
        self.events, self.on_read = events, None

    def read(self, *tags):
        self.events.append(("read", tags))
        if self.on_read:
            self.on_read, on_read = None, self.on_read
            on_read()
        return super().read(*tags)

    def write(self, tag, value):
        self.events.append(("write", tag))
        return super().write(tag, value)


def test_a_write_queued_during_a_multi_chunk_cycle_runs_before_the_next_chunk():
    fake = FakePLC()
    tags = [f"LongTagName{i:02}" for i in range(20)]
    fake.tags = {t: tag_def("DINT") for t in tags}
    fake.values.update({t: i for i, t in enumerate(tags)})
    events = []
    worker, driver = make_worker(fake, driver=LogDriver(fake, events))
    worker.on_values = lambda plc, changes: events.append(("publish", sorted(changes)))
    worker.add_tags(tags)
    futures = []
    driver.on_read = lambda: futures.append(worker.write("SpeedSP", 5))
    poll(worker, driver, 1)
    reads = [e for e in events if e[0] == "read"]
    assert len(reads) > 3  # several chunks
    first = events.index(reads[0])
    # the first chunk's values are published, then the write and its read-back run before the second chunk
    assert events[first + 1] == ("publish", sorted(reads[0][1]))
    assert events[first + 2:first + 4] == [("write", "SpeedSP"), ("read", ("SpeedSP",))]
    assert events[first + 4] == reads[2] and reads[2][1][0] in tags
    assert futures[0].result(0) == (True, None, None, 5)
    assert len(worker.snapshot(tags)) == 20


def test_read_back_after_a_write():
    fake = FakePLC()
    fake.values.update({"Speed": 1.0, "Flags": 0b1010})
    worker, driver = make_worker(fake, [{"match": "Speed", "deadband": 100}])
    worker.add_tags(["Speed", "Flags"])
    poll(worker, driver, 1)
    fake.reads.clear()
    future = worker.write("Speed", 42.5)
    bit = worker.write("Flags.0", True)
    worker._run_writes(driver)
    assert future.result(0) == (True, None, None, 42.5)
    assert bit.result(0) == (True, None, None, True)
    assert fake.reads == [("Speed",), ("Flags.0", "Flags")]  # a bit is read back with its word
    assert worker.snapshot(["Speed"])["Speed"][:2] == [42.5, "good"]
    assert published(worker, "Speed")[-1][0] == 42.5  # published at once, no deadband


def test_write_results():
    fake = FakePLC()
    worker, driver = make_worker(fake)
    assert worker.write("T", 1).result(0) == (False, "PLC not connected", "refused", None)
    worker.state = "connected"
    fake.write_error = TagError("value does not fit", ErrorKind.BAD_VALUE)
    future = worker.write("T", 1)
    worker._run_writes(driver)
    assert future.result(0) == (False, "value does not fit", "bad_value", None)
    fake.write_error, fake.fail = None, True
    future = worker.write("T", 1)
    with pytest.raises(CommError):  # the connection is gone: the worker reconnects
        worker._run_writes(driver)
    assert future.result(0)[0] is False and future.result(0)[2] == "comm"
    future = worker.write("T", 1)
    worker._fail_writes("PLC disconnected")
    assert future.result(0) == (False, "PLC disconnected", "comm", None)


class DescDriver(FakeDriver):
    def __init__(self, plc, calls):
        super().__init__(plc)
        self.calls = calls

    def get_tag_descriptions(self, *names):
        self.calls.append(names)
        if "Boom" in names:
            raise CommError("no reply")
        return [Tag(f"{n}.@Description", f"about {n}", "STRING", None) if n != "B"
                else Tag(f"{n}.@Description", None, "STRING", TagError("none", ErrorKind.NOT_FOUND)) for n in names]


def test_descriptions_are_read_once_per_tag_table_version():
    fake = FakePLC()
    fake.values.update({"A": 5, "B": 1, "C": 2, "R": 3})
    calls, infos = [], []
    worker, driver = make_worker(fake, [{"match": "R", "description": "from the rule"}],
                                 driver=DescDriver(fake, calls), on_info=lambda plc, d: infos.append(d))
    worker.add_tags(["A", "B", "R", "A.1"])
    poll(worker, driver, 3)
    assert calls == [("A", "B")]  # batched, bits and rule descriptions left out, then never again
    assert infos == [{"A": "about A", "B": None}]
    assert worker.description("A") == "about A" and worker.description("B") is None
    worker.add_tags(["C"])
    poll(worker, driver, 3)
    assert calls[1:] == [("C",)]
    fake.version = 2  # a reload: asked again for every subscribed tag
    poll(worker, driver, 3)
    assert calls[2:] == [("A", "B", "C")]
    worker.add_tags(["Boom"])
    poll(worker, driver, 2)  # failure is not an error: no description
    assert worker.description("Boom") is None and infos[-1] == {"Boom": None}


def test_together_reads_the_struct_whole():
    tags = ["Line1.Motor.Speed", "Line1.Ready"]
    for rules, whole in (([], False), ([{"match": "Line1", "together": True}], True)):
        fake = FakePLC()
        plant(fake)
        worker, driver = make_worker(fake, rules, whole_max_bytes=0)  # never whole by cost
        worker.add_tags(tags)
        poll(worker, driver, 1)
        assert (fake.reads[-1] == ("Line1",)) is whole
        assert worker.snapshot(tags)["Line1.Motor.Speed"][:2] == [1.5, "good"]


def test_type_info_version_and_types():
    fake = FakePLC()
    plant(fake)
    worker, driver = make_worker(fake)
    assert worker.type_info("Speed") == {"type": None}  # not connected
    poll(worker, driver, 1)
    assert worker.version == 1 and worker.types == type_map(driver)
    assert worker.type_info("Speed") == {"type": "REAL"}
    assert worker.type_info("Name1") == {"type": "STRING"}
    assert worker.type_info("Arr") == {"type": "Station", "dims": [5]}
    assert worker.type_info("Arr[1]") == {"type": "Station"}
    assert worker.type_info("Line1.Data") == {"type": "DINT", "dims": [4]}
    assert worker.type_info("Line1.Data[2]") == {"type": "DINT"}
    assert worker.type_info("Count.3") == {"type": "BOOL"}
    assert worker.type_info("Nope") == {"type": None}


def test_always_tags_are_polled_without_subscribers(make):
    client, fakes, _ = make(extra='[[plc.PLC1.rule]]\nmatch = "Count"\nalways = true\n',
                            setup=lambda f: f["PLC1"].values.update(Count=9))
    worker = client.app.state.hub.workers["PLC1"]
    assert worker.tiers == {"Count": 1}
    wait_for(lambda: worker.snapshot(["Count"]).get("Count", [None])[:2] == [9, "good"])
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Count"]})
        assert until(ws, lambda m: m["op"] == "values")["d"]["Count"][:2] == [9, "good"]


def test_read_back_is_published_when_the_deadband_held_back_the_cached_value():
    fake = FakePLC()
    fake.values["Speed"] = 10.0
    worker, driver = make_worker(fake, [{"match": "Speed", "deadband": 0.5}])
    worker.add_tags(["Speed"])
    poll(worker, driver, 2, at={1: lambda w: fake.values.update(Speed=10.3)})
    assert worker.snapshot(["Speed"])["Speed"][0] == 10.3 and published(worker, "Speed")[-1][0] == 10.0
    future = worker.write("Speed", 10.3)
    worker._run_writes(driver)
    assert future.result(0) == (True, None, None, 10.3)
    assert published(worker, "Speed")[-1][0] == 10.3
    n = len(worker.out)
    worker.write("Speed", 10.3)
    worker._run_writes(driver)
    assert len(worker.out) == n  # already published: not again


def test_stop_runs_the_queued_writes_and_refuses_later_ones():
    fake = FakePLC()
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 60000)  # sits in its cycle wait
    worker.start()
    wait_for(lambda: worker.state == "connected" and worker.last_ok_ts is not None)
    future = worker.write("StartPB", False)
    worker.stop()
    assert future.result(1) == (True, None, None, False) and fake.writes == [("StartPB", False)]
    assert worker.write("StartPB", True).result(0) == (False, "PLC not connected", "refused", None)


def test_bool_arrays_are_bool_with_dims_in_bools():
    fake = FakePLC()
    fake.tags = {"Flags": tag_def("DWORD", 1, [2]),  # ciplink's BOOL[64]
                 "Box": tag_def(udt("BoxT", 8, {"Bits": ("DWORD", 1)}))}
    worker, driver = make_worker(fake)
    poll(worker, driver, 1)
    assert worker.type_info("Flags") == {"type": "BOOL", "dims": [64]}
    assert worker.type_info("Flags[3]") == {"type": "BOOL"}
    assert worker.type_info("Box.Bits") == {"type": "BOOL", "dims": [32]}
    assert worker.tag_list[0] == {"name": "Flags", "type": "BOOL", "dims": [64]}
    driver.data_types = {"BoxT": fake.tags["Box"]["data_type"]}
    assert type_map(driver)["BoxT"]["members"]["Bits"] == {"type": "BOOL", "dims": [32]}


def test_a_write_queued_while_the_worker_sleeps_runs_at_once():
    fake = FakePLC()
    fake.values.update({"T": 1, "SP": 0})
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 500)
    worker.add_tags(["T"])
    worker.start()
    wait_for(lambda: worker.state == "connected" and len(fake.reads) >= 1)
    time.sleep(0.02)  # the tick ran: the worker sleeps the rest of its 500 ms
    start = time.monotonic()
    assert worker.write("SP", 5).result(5)[0]
    latency = time.monotonic() - start
    worker.stop()
    assert latency < 0.1 and fake.values["SP"] == 5
