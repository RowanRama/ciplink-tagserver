"""One thread per PLC: owns the driver, polls the subscribed tags by tier, runs the queued writes."""

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future

from . import planner
from .values import tag_list, to_value

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


class PlcWorker:
    backoff = (1, 2, 5, 10)  # seconds between reconnect attempts, the last one repeats
    missing_retry = 10  # seconds before a tag the PLC doesn't have is asked for again, unless the tag table reloads
    group_retry = 60  # seconds before a struct whose whole read failed is read whole again, unless it reloads

    def __init__(self, cfg, factory, cycle_ms, on_values=None, on_status=None, on_meta=None,
                 tier2_period_s=3, grace_s=10, planner=True, whole_max_bytes=1500):
        self.name = cfg.name
        self.cfg = cfg
        self.factory = factory  # cfg -> driver, not yet opened
        self.cycle = cycle_ms / 1000
        self.tier2_period = tier2_period_s
        self.grace_s = grace_s
        self.planner = planner
        self.whole_max_bytes = whole_max_bytes
        self.on_values = on_values or (lambda plc, changes: None)  # called from this thread
        self.on_status = on_status or (lambda plc, state: None)
        self.on_meta = on_meta or (lambda plc: None)

        self.state = "connecting"
        self.lock = threading.Lock()  # guards tiers, grace and cache
        self.tiers = {}  # tag -> 1 (read every cycle), 2 (round robin, every tier2_period) or 3 (not read)
        self.grace = {}  # tag that left tier 1 -> (monotonic time it leaves tier 2, tier after that or None)
        self._tier2 = None  # tier-2 tags in name order, None when to be rebuilt
        self._slice = 0
        self._plans = {}  # frozenset of tags -> read plan, cleared when the subscriptions or tag table change
        self._no_group = {}  # lowercase struct path whose whole read failed -> monotonic time it failed
        self._new_tier2 = set()  # tags just added at tier 2: read in the next cycle, then in their slice
        self.cache = {}  # tag -> [value, quality, t]
        self.missing = {}  # tag -> monotonic time to ask for it again; skipped until then
        self.writes = queue.Queue()  # (tag, value, Future)
        self.tag_list = []  # for GET /tags, rebuilt here after a connect or reload

        self.last_cycle_ms = None
        self.last_ok_ts = None
        self.reads_per_cycle = 0  # read services sent, after planning
        self.tags_per_cycle = 0
        self.group_fallbacks = 0  # whole struct reads that failed, their members read one by one instead
        self.overruns = 0
        self.reconnects = 0
        self.reloads = 0
        self._version = None

        self._stop = threading.Event()
        self.wait = self._stop.wait  # returns True once stopped; tests swap it for a fake clock
        self._thread = threading.Thread(target=self.run, name=f"plc-{self.name}", daemon=True)

    # called from other threads

    def start(self):
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def tags(self) -> set:
        return set(self.tiers)

    def tier_counts(self) -> dict:
        with self.lock:
            tiers = list(self.tiers.values())
        return {f"tier{n}": tiers.count(n) for n in (1, 2, 3)}

    def add_tags(self, tags, tier=1):
        with self.lock:
            for tag in tags:
                self._set(tag, tier)

    def remove_tags(self, tags):
        """Stops polling the tags at once, without the grace period."""
        with self.lock:
            for tag in tags:
                self._set(tag, None)

    def set_tier(self, tag, tier):
        """A tag's tier, None when nothing subscribes it. A tag leaving tier 1 for tier 3 or nothing is read
        at tier 2 for grace_s first, so a page visited again finds its values fresh."""
        with self.lock:
            if (tier is not None and tier <= 2) or self.grace_s <= 0:
                self._set(tag, tier)
            elif self.tiers.get(tag) == 1:
                self._set(tag, 2)
                self.grace[tag] = (time.monotonic() + self.grace_s, tier)
            elif tag in self.grace:
                self.grace[tag] = (self.grace[tag][0], tier)
            else:
                self._set(tag, tier)

    def _set(self, tag, tier):  # holding the lock
        self.grace.pop(tag, None)
        if tier is None:
            self.tiers.pop(tag, None)
            self.cache.pop(tag, None)
            self.missing.pop(tag, None)
        else:
            if tier == 2 and self.tiers.get(tag) in (None, 3):  # never read yet: once now, not a period later
                self._new_tier2.add(tag)
            self.tiers[tag] = tier
        if tier != 2:
            self._new_tier2.discard(tag)
        self._tier2 = None
        self._plans.clear()

    def _cycle_tags(self, now) -> list:
        """This cycle's reads: all of tier 1 and the next slice of tier 2 (holding the lock)."""
        for tag, (deadline, tier) in list(self.grace.items()):
            if deadline <= now:
                self._set(tag, tier)
        tags = [tag for tag, tier in self.tiers.items() if tier == 1]
        if self._tier2 is None:  # in name order, so members of one struct tend to share a slice
            self._tier2 = sorted((tag for tag, tier in self.tiers.items() if tier == 2), key=str.lower)
        if self._tier2:
            slices = max(1, round(self.tier2_period / self.cycle)) if self.cycle > 0 else 1
            size = math.ceil(len(self._tier2) / slices)
            i = self._slice % slices
            self._slice += 1
            tags += self._tier2[i * size:(i + 1) * size]
        if self._new_tier2:
            tags += sorted(self._new_tier2 - set(tags))
            self._new_tier2.clear()
        return [tag for tag in tags if self.missing.get(tag, 0) <= now]

    def snapshot(self, tags) -> dict:
        with self.lock:
            return {tag: list(self.cache[tag]) for tag in tags if tag in self.cache}

    def write(self, tag, value) -> Future:
        """Queues a write for the next cycle; the Future gets (ok, error). Never retried."""
        future = Future()
        if self.state != "connected":
            future.set_result((False, "PLC not connected"))
        else:
            self.writes.put((tag, value, future))
        return future

    # the worker thread

    def run(self):
        attempt = 0
        while not self._stop.is_set():
            plc = None
            try:
                plc = self.factory(self.cfg)
                plc.open()
                self._fail_writes("PLC disconnected")  # any left over from before the connect
                self._check_version(plc, connecting=True)
                attempt = 0
                self._set_state("connected")
                self._poll(plc)
            except Exception as err:  # CommError, or anything else the driver raised
                log.warning("%s: %r", self.name, err)
            finally:
                if plc is not None:
                    try:
                        plc.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            self._fail_writes("PLC disconnected")
            self._mark_stale()
            self._set_state("disconnected")
            delay = self.backoff[min(attempt, len(self.backoff) - 1)]
            attempt += 1
            if self.wait(delay):
                break
            self.reconnects += 1
            self._set_state("connecting")

    def _poll(self, plc):
        while not self._stop.is_set():
            start = time.monotonic()
            with self.lock:
                tags = self._cycle_tags(start)
            services = 0
            if tags:
                results, services = self._read(plc, tags)
                t = now_ms()
                changes = {}
                with self.lock:
                    for tag in tags:
                        result = results[tag]
                        if tag not in self.tiers:  # unsubscribed during the read
                            continue
                        value, quality = to_value(result)
                        if quality == "missing":  # don't ask (and make the driver log) every cycle
                            self.missing[tag] = start + self.missing_retry
                        else:
                            self.missing.pop(tag, None)
                        old = self.cache.get(tag)
                        if old is None or old[0] != value or old[1] != quality:
                            self.cache[tag] = [value, quality, t]
                            changes[tag] = [value, quality, t]
                if changes:
                    self.on_values(self.name, changes)
            self.reads_per_cycle = services
            self.tags_per_cycle = len(tags)
            self._check_version(plc)
            self._run_writes(plc)
            self.last_ok_ts = now_ms()
            elapsed = time.monotonic() - start
            self.last_cycle_ms = round(elapsed * 1000, 1)
            if elapsed > self.cycle:
                self.overruns += 1
            elif self.wait(self.cycle - elapsed):
                return

    def _read(self, plc, tags):
        """({tag: Tag result}, read services sent), through the planner unless it is off."""
        if not self.planner or not hasattr(plc, "get_tag_info"):
            reads = [(tag, [(tag, None)]) for tag in tags]
        else:
            now = time.monotonic()
            retry = [name for name, failed_at in self._no_group.items() if now - failed_at >= self.group_retry]
            if retry:  # read those whole again
                for name in retry:
                    del self._no_group[name]
                self._plans.clear()
            key = frozenset(tags)
            reads = self._plans.get(key)
            if reads is None:
                if len(self._plans) > 100:  # tier-2 slices make a few sets per subscription change
                    self._plans.clear()
                budget = getattr(plc, "connection_size", None)
                budget = budget if isinstance(budget, int) and budget > 0 else planner.PACKET_BYTES
                reads = planner.plan(tags, plc.get_tag_info, self.whole_max_bytes, frozenset(self._no_group), budget)
                self._plans[key] = reads
        results, failed, services = planner.execute(plc, reads)
        if failed:
            self.group_fallbacks += len(failed)
            log.warning("%s: whole read of %s failed, reading the members instead", self.name, ", ".join(failed))
            self._no_group.update((name.lower(), time.monotonic()) for name in failed)
            self._plans.clear()
        return results, services

    def _run_writes(self, plc):
        while True:
            try:
                tag, value, future = self.writes.get_nowait()
            except queue.Empty:
                return
            try:
                result = plc.write(tag, value)
            except Exception as err:
                future.set_result((False, str(err)))
                raise
            future.set_result((result.error is None, result.error))

    def _check_version(self, plc, connecting=False):
        """Counts a reload and tells the clients when the driver's tag table version changed."""
        version = getattr(plc, "_tag_table_version", None)
        changed = version != self._version
        if changed and self._version is not None:
            self.reloads += 1
            log.info("%s: tag table reloaded (version %s -> %s)", self.name, self._version, version)
            self.on_meta(self.name)
        self._version = version
        if changed or connecting:
            self.tag_list = tag_list(plc)
            with self.lock:
                self.missing.clear()  # a new or renamed tag may exist now
                self._no_group.clear()
                self._plans.clear()  # members or struct sizes may have changed

    def _fail_writes(self, error):
        while True:
            try:
                _, _, future = self.writes.get_nowait()
            except queue.Empty:
                return
            future.set_result((False, error))

    def _mark_stale(self):
        changes = {}
        with self.lock:
            for tag, entry in self.cache.items():
                if entry[1] != "stale":
                    entry[1] = "stale"
                    changes[tag] = list(entry)
        if changes:
            self.on_values(self.name, changes)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            log.info("%s: %s", self.name, state)
            self.on_status(self.name, state)
