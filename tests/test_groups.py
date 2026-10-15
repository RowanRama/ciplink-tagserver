"""Poll groups: their rate applies at tier 1 only; the worker ticks at the smallest active rate."""

from ciplink_tagserver.config import parse_config
from ciplink_tagserver.rules import Rules
from ciplink_tagserver.worker import PlcWorker

from conftest import FakeDriver, FakePLC, hello, until, wait_for
from test_tiers import poll

GROUPS = [{"name": "fast", "rate_ms": 20, "match": "Fast*"}, {"name": "slow", "rate_ms": 500, "match": "Slow"}]


def worker_for(fake, rules=(), groups=GROUPS, cycle_ms=100, **kw):
    config = parse_config({"plc": {"P": {"path": "x", "rule": list(rules), "group": list(groups)}}})
    return PlcWorker(config.plcs["P"], lambda cfg: FakeDriver(fake), cycle_ms, rules=Rules(config), **kw), \
        FakeDriver(fake)


def reads_of(fake, tag):
    return sum(tag in r for r in fake.reads)


def test_group_rate_applies_only_at_tier1_and_stops_when_the_page_is_left():
    fake = FakePLC()
    fake.values.update({"FastA": 1, "FastB": 2, "Plain": 3})
    worker, driver = worker_for(fake, grace_s=0)
    worker.add_tags(["FastA", "FastB", "Plain"], 1)
    clock = poll(worker, driver, 20)  # 20 ticks of 20 ms = 4 cycles of 100 ms
    assert set(clock.waits) == {0.02}
    assert reads_of(fake, "FastA") == 20 and reads_of(fake, "FastB") == 20
    assert reads_of(fake, "Plain") == 4
    assert set(fake.reads[0]) == {"FastA", "FastB", "Plain"}  # due in the same tick: one plc.read
    assert worker.group_stats() == {"fast": {"rate_ms": 20, "tags": 2, "last_period_ms": 20.0},
                                    "slow": {"rate_ms": 500, "tags": 0, "last_period_ms": None}}

    worker.set_tier("FastA", 2)  # the page that showed them was left: tier 2 ignores groups
    worker.set_tier("FastB", 2)
    fake.reads.clear()
    clock = poll(worker, driver, 10)
    assert set(clock.waits) == {0.1}  # no group has tier-1 tags: the tick is cycle_ms again
    assert reads_of(fake, "Plain") == 10
    assert reads_of(fake, "FastA") <= 2  # its tier-2 slices (3 s period), not every 20 ms
    assert worker.group_stats()["fast"]["tags"] == 0

    worker.set_tier("FastA", 1)  # back on the page: due at the next tick, at the group rate again
    fake.reads.clear()
    clock = poll(worker, driver, 5)
    assert set(clock.waits) == {0.02} and reads_of(fake, "FastA") == 5


def test_slow_group_reads_less_often_than_the_cycle():
    fake = FakePLC()
    fake.values.update({"Slow": 1, "Plain": 2})
    worker, driver = worker_for(fake)
    worker.add_tags(["Slow", "Plain"], 1)
    clock = poll(worker, driver, 20)  # 2 s
    assert set(clock.waits) == {0.1}  # a group slower than the cycle does not change the tick
    assert reads_of(fake, "Plain") == 20 and reads_of(fake, "Slow") == 4
    assert worker.group_stats()["slow"] == {"rate_ms": 500, "tags": 1, "last_period_ms": 500.0}


def test_tier2_slices_advance_only_on_cycle_boundaries():
    fake = FakePLC()
    t2 = [f"B{i}" for i in range(10)]
    fake.values.update({"Fast": 0, **{t: 0 for t in t2}})
    worker, driver = worker_for(fake, tier2_period_s=0.5)  # 100 ms cycles: 5 slices of 2
    worker.add_tags(["Fast"], 1)
    worker.add_tags(t2, 2)
    poll(worker, driver, 50)  # 50 ticks of 20 ms = 10 cycles
    assert len(fake.reads) == 50 and reads_of(fake, "Fast") == 50
    with_t2 = [i for i, r in enumerate(fake.reads) if any(t.startswith("B") for t in r)]
    assert with_t2 == list(range(0, 50, 5))  # only on the 100 ms boundaries
    assert all(reads_of(fake, t) >= 2 for t in t2)  # every slice came round twice


def test_hub_groups_in_stats_and_info(make):
    extra = '[[plc.PLC1.group]]\nname = "fast"\nrate_ms = 10\nmatch = "Speed"\n'
    client, fakes, _ = make(extra=extra)
    fakes["PLC1"].values.update({"Speed": 1.5, "Count": 2})
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Speed", "Count"]})
        info = until(ws, lambda m: m["op"] == "info")["d"]
        assert info["Speed"]["group"] == "fast" and info["Speed"]["rate_ms"] == 10
        stats = lambda: client.get("/stats").json()["plcs"]["PLC1"]["groups"]["fast"]
        wait_for(lambda: stats()["last_period_ms"] is not None)
        assert stats()["tags"] == 1 and stats()["rate_ms"] == 10
