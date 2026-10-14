from ciplink_tagserver.config import PlcConfig
from ciplink_tagserver.worker import PlcWorker

from conftest import FakeDriver, FakePLC, wait_for


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
