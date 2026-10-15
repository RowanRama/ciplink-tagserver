import json
import os
import time

import pytest

from ciplink_tagserver.config import PlcConfig
from ciplink_tagserver.server import Hub
from ciplink_tagserver.worker import PlcWorker

from conftest import BIG, FakeDriver, FakePLC, hello, plant, tag_def, until, wait_for


class FakeClock:
    """Swaps a worker's clock and wait: wait(s) advances the time by s and returns True after n waits.
    at: {wait number: fn(worker)} runs after that wait (between ticks)."""

    def __init__(self, worker, n=10**9, at=None):
        self.now, self.waits, self.n, self.at, self.worker = 1000.0, [], n, at or {}, worker
        worker.clock = lambda: self.now
        worker.wait = self.wait
        worker.fake_clock = self

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        if len(self.waits) in self.at:
            self.at[len(self.waits)](self.worker)
        return len(self.waits) >= self.n


def poll(worker, driver, n, at=None):
    """Runs n ticks of a connected worker in this thread on a FakeClock; returns the clock."""
    clock = getattr(worker, "fake_clock", None) or FakeClock(worker)
    clock.waits, clock.n, clock.at = [], n, at or {}
    worker._check_version(driver, connecting=True)
    worker._plc, worker.state = driver, "connected"
    worker._poll(driver)
    return clock


def run_cycles(fake, n, setup, **kw):
    """Runs a worker for n poll cycles with a fake clock; returns it (stopped)."""
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), kw.pop("cycle_ms", 100), **kw)
    FakeClock(worker, n)
    setup(worker)
    worker.start()
    worker._thread.join(5)
    return worker


def test_tier1_every_cycle_tier2_round_robin_tier3_never():
    fake = FakePLC()
    t2 = [f"B{i}" for i in range(10)]
    fake.values.update({"A": 1, "C": 3, **{t: 0 for t in t2}})

    def setup(w):
        w.add_tags(["A"], 1)
        w.add_tags(t2, 2)
        w.add_tags(["C"], 3)

    # 100 ms cycles, 0.5 s period: 5 slices of 2
    worker = run_cycles(fake, 21, setup, tier2_period_s=0.5)
    assert sorted(fake.reads[0]) == sorted(["A", *t2])  # new tier-2 tags are read once at once
    reads = fake.reads[1:21]
    assert all("A" in r for r in reads)
    assert not any("C" in r for r in fake.reads)
    for i in range(0, 20, 5):  # then every tier-2 tag once per 5 cycles
        window = [t for r in reads[i:i + 5] for t in r if t.startswith("B")]
        assert sorted(window) == sorted(t2)
    assert all(len(r) == 3 for r in reads)
    assert worker.tags_per_cycle == 3 and worker.tier_counts() == {"tier1": 1, "tier2": 10, "tier3": 1}


def test_a_new_tier2_tag_is_read_in_the_next_cycle_then_in_its_slice():
    worker = PlcWorker(PlcConfig("P", "x"), None, 100, tier2_period_s=10)  # 100 slices
    worker.add_tags([f"B{i}" for i in range(5)], 2)
    now = time.monotonic()
    tags = lambda: worker._cycle_tags(now)[0]
    assert len(tags()) == 5
    assert len(tags()) <= 1
    worker.add_tags(["New"], 2)
    assert "New" in tags()
    worker.set_tier("Up", 1)
    worker.set_tier("Up", 2)  # was read every cycle: no extra read
    assert "New" not in tags() and "Up" not in tags()


def test_failed_group_is_read_whole_again_after_group_retry(monkeypatch):
    monkeypatch.setattr(PlcWorker, "group_retry", 1.0)
    fake = FakePLC()
    plant(fake)
    fake.errors["Program:Station1.Motor"] = "General error"
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 10)
    worker.add_tags(["Program:Station1.Motor.Speed", "Program:Station1.Motor.Current"])
    worker.start()
    wait_for(lambda: worker.group_fallbacks == 1)
    failed_at = time.monotonic()
    del fake.errors["Program:Station1.Motor"]
    wait_for(lambda: worker.reads_per_cycle == 2)  # members one by one meanwhile
    wait_for(lambda: worker.reads_per_cycle == 1)  # whole again after group_retry
    assert time.monotonic() - failed_at > 0.9
    assert worker.group_fallbacks == 1
    worker.stop()


def test_grace_demotion():
    worker = PlcWorker(PlcConfig("P", "x"), None, 100, grace_s=10)
    clock = FakeClock(worker)

    def after(seconds):  # one cycle that many seconds later
        clock.now += seconds
        worker._cycle_tags(clock.now)

    worker.set_tier("T", 1)
    worker.set_tier("T", None)  # last tier-1 subscription gone
    assert worker.tiers == {"T": 2}
    after(5)  # still polled, at tier 2, during the grace
    assert worker.tiers == {"T": 2}
    after(6)
    assert worker.tiers == {}

    worker.set_tier("T", 1)
    worker.set_tier("T", 3)  # only a tier-3 subscription left: tier 2 for the grace, then 3
    after(5)
    assert worker.tiers == {"T": 2}
    after(6)
    assert worker.tiers == {"T": 3}

    worker.set_tier("T", 1)
    worker.set_tier("T", 2)  # still subscribed at tier 2: no grace, stays
    after(11)
    assert worker.tiers == {"T": 2}
    worker.set_tier("T", None)  # leaving tier 2 needs no grace
    assert worker.tiers == {}

    worker.set_tier("T", 1)
    worker.set_tier("T", None)
    worker.set_tier("T", 1)  # back within the grace
    after(11)
    assert worker.tiers == {"T": 1}


def test_hub_tiers_and_grace(make):
    client, fakes, _ = make(server="grace_s = 0.3")
    fakes["PLC1"].values.update({"A": 1, "B": 2})
    stats = lambda: client.get("/stats").json()["plcs"]["PLC1"]
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["A", "B"]})
        ws.send_json({"op": "sub", "id": 2, "tags": ["B"], "tier": 3})
        ws.send_json({"op": "sub", "id": 3, "tags": ["A"], "tier": 2})
        until(ws, lambda m: m["op"] == "ack" and m["id"] == 3)
        assert client.app.state.hub.workers["PLC1"].tiers == {"A": 1, "B": 1}  # the lowest tier wins
        ws.send_json({"op": "unsub", "id": 1})
        until(ws, lambda m: m["op"] == "ack")
        assert client.app.state.hub.workers["PLC1"].tiers == {"A": 2, "B": 2}  # B in its grace
        wait_for(lambda: client.app.state.hub.workers["PLC1"].tiers == {"A": 2, "B": 3})
        assert (stats()["tier1"], stats()["tier2"], stats()["tier3"]) == (0, 1, 1)
        ws.send_json({"op": "sub", "id": 4, "tags": ["X"], "tier": 4})
        assert "tier" in until(ws, lambda m: m["op"] == "error")["error"]


def test_page_learning_and_page_subscriptions(make, monkeypatch):
    monkeypatch.setattr(Hub, "save_delay", 0.01)
    client, fakes, config = make()
    fakes["PLC1"].values.update({"A": 1, "B": 2, "C": 3})
    hub = client.app.state.hub
    with client.websocket_connect("/ws") as nav, client.websocket_connect("/ws") as page:
        hello(nav)
        hello(page)
        nav.send_json({"op": "sub", "id": 1, "page": "/line1", "tier": 2})  # not learned yet: empty
        assert until(nav, lambda m: m["op"] == "ack")["ok"]
        assert hub.workers["PLC1"].tiers == {}

        page.send_json({"op": "sub", "id": 1, "tags": ["A", "PLC1/B"], "page": "/line1"})
        until(page, lambda m: m["op"] == "values")
        page.send_json({"op": "sub", "id": 2, "tags": ["C"], "page": "/line1", "tier": 2})  # tier 2 doesn't teach
        until(page, lambda m: m["op"] == "ack" and m["id"] == 2)
        assert hub.pages == {"/line1": {"PLC1/A", "PLC1/B"}}
        page.send_json({"op": "unsub", "id": 1})
        until(page, lambda m: m["op"] == "ack" and m["id"] == 1)
        # the page subscription followed what /line1 learned, so A and B stay polled at tier 2
        assert hub.workers["PLC1"].tiers == {"A": 2, "B": 2, "C": 2}
        d = {}
        while len(d) < 2:  # its values are keyed by the plain names (one PLC)
            d.update(until(nav, lambda m: m["op"] == "values")["d"])
        assert d["A"][:2] == [1, "good"] and d["B"][:2] == [2, "good"]

        nav.send_json({"op": "sub", "id": 5})
        assert until(nav, lambda m: m["op"] == "error")["error"] == "missing field 'tags'"

    wait_for(lambda: os.path.exists(config.pages_file))
    assert json.load(open(config.pages_file)) == {"/line1": ["PLC1/A", "PLC1/B"]}

    client2, _, _ = make()  # a restart loads what was learned
    assert client2.app.state.hub.pages == {"/line1": {"PLC1/A", "PLC1/B"}}


MIXED = ["Line1.Motor.Speed", "Line1.Motor.Current", "Line1.Ready", "Line1.Data[1]", "Line1.Motor",
         "Arr[2].Motor.Speed", "Arr[2].Ready", "Program:Station1.Motor.Speed", "Program:Station1.Motor.Count.3",
         "Valve1.Open", "Valve1.EnableIn", "Pump1.Run", "Pump1.Speed", "Big1.A", "Big1.B", "Speed", "Count",
         "Line2.Data", "Line2.Ready", "Nope.X", "Name1"]


@pytest.mark.parametrize("error", [None, "Line1"])
def test_planner_gives_the_same_results_as_plain_reads(error):
    snapshots, services = [], []
    for planner_on in (True, False):
        fake = FakePLC()
        plant(fake)
        if error:
            fake.errors[error] = "General error"
        worker = run_cycles(fake, 3, lambda w: w.add_tags(MIXED), planner=planner_on)
        snapshots.append({t: v[:2] for t, v in worker.snapshot(MIXED).items()})
        services.append(worker.reads_per_cycle)
        if error and planner_on:
            assert worker.group_fallbacks == 1  # then not grouped again
    assert snapshots[0] == snapshots[1]
    assert len(snapshots[0]) == len(MIXED)
    assert services[1] == len(MIXED) - 1 and services[0] < services[1]  # Nope.X is skipped once missing


def test_replans_after_a_tag_table_reload():
    fake = FakePLC()
    plant(fake)
    tags = ["Big1.A", "Big1.B", "Line1.Motor.Speed", "Line1.Motor.Current"]
    worker = PlcWorker(PlcConfig("P", "x"), lambda cfg: FakeDriver(fake), 10)
    worker.add_tags(tags)
    worker.start()
    wait_for(lambda: worker.reads_per_cycle == 3)  # Big1 is too big, Line1.Motor read whole
    small_big = dict(BIG, template={"structure_size": 8})
    fake.tags = dict(fake.tags, Big1=tag_def(small_big))
    fake.version = 2  # an online edit: the type is now small
    wait_for(lambda: worker.reads_per_cycle == 2)
    fake.reads.clear()
    wait_for(lambda: len(fake.reads) > 2)
    assert set(fake.reads[-1]) == {"Big1", "Line1.Motor"}
    worker.stop()


def test_stats_counts_after_planning(make):
    client, fakes, _ = make(setup=lambda f: plant(f["PLC1"]))
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Line1.Motor.Speed", "Line1.Motor.Current", "Speed"]})
        ws.send_json({"op": "sub", "id": 2, "tags": ["Count"], "tier": 2})
        until(ws, lambda m: m["op"] == "ack" and m["id"] == 2)
        wait_for(lambda: client.get("/stats").json()["plcs"]["PLC1"]["tags_per_cycle"] == 3)
        plc = client.get("/stats").json()["plcs"]["PLC1"]
        assert (plc["tier1"], plc["tier2"], plc["reads_per_cycle"], plc["subscriptions"]) == (3, 1, 2, 4)
