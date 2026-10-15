"""One thread per PLC: owns the driver, polls the subscribed tags by tier and poll group, runs the queued writes."""

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future

from . import planner
from .rules import BIT
from .values import bool_array, merge, tag_list, to_value, type_map, type_name

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


def _as_list(result):
    return result if isinstance(result, list) else [result]


class PlcWorker:
    backoff = (1, 2, 5, 10)  # seconds between reconnect attempts, the last one repeats
    missing_retry = 10  # seconds before a tag the PLC doesn't have is asked for again, unless the tag table reloads
    group_retry = 60  # seconds before a struct whose whole read failed is read whole again, unless it reloads

    def __init__(self, cfg, factory, cycle_ms, on_values=None, on_status=None, on_meta=None,
                 tier2_period_s=3, grace_s=10, planner=True, whole_max_bytes=1500, rules=None, on_info=None):
        self.name = cfg.name
        self.cfg = cfg
        self.factory = factory  # cfg -> driver, not yet opened
        self.cycle = cycle_ms / 1000
        self.tier2_period = tier2_period_s
        self.grace_s = grace_s
        self.planner = planner
        self.whole_max_bytes = whole_max_bytes
        self.rules = rules
        self.on_values = on_values or (lambda plc, changes: None)  # called from this thread
        self.on_status = on_status or (lambda plc, state: None)
        self.on_meta = on_meta or (lambda plc: None)
        self.on_info = on_info or (lambda plc, descriptions: None)

        self.state = "connecting"
        self.lock = threading.Lock()  # guards tiers, grace, cache, published and the schedule
        self.tiers = {}  # tag -> 1 (read at its rate), 2 (round robin, every tier2_period) or 3 (not read)
        self.grace = {}  # tag that left tier 1 -> (clock time it leaves tier 2, tier after that or None)
        self._tier2 = None  # tier-2 tags in name order, None when to be rebuilt
        self._slice = 0
        self._last_read = {}  # tier-1 tag -> clock time of its last read
        self._last_cycle = None  # clock time of the last cycle_ms boundary
        self._groups = {}  # tag -> its poll group table or None
        self._group_reads = {}  # group name -> (previous, last) clock time it was read
        self._plans = {}  # frozenset of tags -> read chunks, cleared when the subscriptions or tag table change
        self._no_group = {}  # lowercase struct path whose whole read failed -> clock time it failed
        self._new_tier2 = set()  # tags just added at tier 2: read in the next tick, then in their slice
        self.cache = {}  # tag -> [value, quality, t, error], always the latest
        self.published = {}  # tag -> the entry last given to on_values (deadband reference)
        self.missing = {}  # tag -> clock time to ask for it again; skipped until then
        self.writes = queue.Queue()  # (tag, value, Future)
        self.tag_list = []  # for GET /tags, rebuilt here after a connect or reload
        self.types = {}
        self.version = None  # the driver's tag table version
        self._descriptions = {}  # tag -> controller description or None, for this version
        self._want_descriptions = set()  # tags that entered tiers, described in the next tick
        self._plc = None  # the connected driver, for type_info

        self.last_cycle_ms = None
        self.last_ok_ts = None
        self.reads_per_cycle = 0  # read services sent in the last tick, after planning
        self.tags_per_cycle = 0
        self.group_fallbacks = 0  # whole struct reads that failed, their members read one by one instead
        self.overruns = 0  # ticks that took longer than the tick
        self.reconnects = 0
        self.reloads = 0

        self._stop = threading.Event()
        self._wake = threading.Event()  # set by stop() and by a queued write
        self.clock = time.monotonic  # tests swap it and wait for a fake clock
        self.wait = self._sleep  # returns True once stopped
        self._thread = threading.Thread(target=self.run, name=f"plc-{self.name}", daemon=True)

    # called from other threads

    def start(self):
        self._thread.start()

    def stop(self, timeout=5):
        """Stops the thread; writes queued until then still run (a momentary release must reach the PLC)."""
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        self._fail_writes("server stopping")  # any queued after the thread ended

    @property
    def tags(self) -> set:
        return set(self.tiers)

    def tier_counts(self) -> dict:
        with self.lock:
            tiers = list(self.tiers.values())
        return {f"tier{n}": tiers.count(n) for n in (1, 2, 3)}

    def group_stats(self) -> dict:
        """{group: {rate_ms, tags (at tier 1 now), last_period_ms}} for /stats."""
        if self.rules is None:
            return {}
        with self.lock:
            counts = {}
            for tag, tier in self.tiers.items():
                group = self._group(tag) if tier == 1 else None
                if group:
                    counts[group["name"]] = counts.get(group["name"], 0) + 1
            reads = dict(self._group_reads)
        result = {}
        for group in self.rules.groups(self.name):
            prev, last = reads.get(group["name"], (None, None))
            period = round((last - prev) * 1000, 1) if prev is not None else None
            result[group["name"]] = {"rate_ms": group["rate_ms"], "tags": counts.get(group["name"], 0),
                                     "last_period_ms": period}
        return result

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
                self.grace[tag] = (self.clock() + self.grace_s, tier)
            elif tag in self.grace:
                self.grace[tag] = (self.grace[tag][0], tier)
            else:
                self._set(tag, tier)

    def _set(self, tag, tier):  # holding the lock
        self.grace.pop(tag, None)
        if tier is None:
            self.tiers.pop(tag, None)
            self.cache.pop(tag, None)
            self.published.pop(tag, None)
            self.missing.pop(tag, None)
            self._groups.pop(tag, None)
            self._want_descriptions.discard(tag)
        else:
            if tier == 2 and self.tiers.get(tag) in (None, 3):  # never read yet: once now, not a period later
                self._new_tier2.add(tag)
            if tag not in self.tiers and not BIT.match(tag):
                self._want_descriptions.add(tag)
            self.tiers[tag] = tier
        if tier != 2:
            self._new_tier2.discard(tag)
        if tier != 1:  # back at tier 1 it is due at the next tick
            self._last_read.pop(tag, None)
        self._tier2 = None
        self._plans.clear()

    def _group(self, tag):  # holding the lock
        if self.rules is None:
            return None
        if tag not in self._groups:
            self._groups[tag] = self.rules.group(self.name, tag)
        return self._groups[tag]

    def _rate(self, tag) -> float:
        group = self._group(tag)
        return group["rate_ms"] / 1000 if group else self.cycle

    def _cycle_tags(self, now) -> tuple:
        """(tags to read this tick, tick in seconds, whether it is a cycle_ms boundary), holding the lock.
        Tier-1 tags when due at their rate; the tier-2 slice, grace expiry and version check on boundaries."""
        tier1 = [tag for tag, tier in self.tiers.items() if tier == 1]
        tick = min([self.cycle] + [self._rate(tag) for tag in tier1])
        half = tick / 2
        boundary = self._last_cycle is None or now + half >= self._last_cycle + self.cycle
        tags = []
        if boundary:
            self._last_cycle = now
            for tag, (deadline, tier) in list(self.grace.items()):
                if deadline <= now:
                    self._set(tag, tier)
            tier1 = [tag for tag, tier in self.tiers.items() if tier == 1]
        groups = set()
        for tag in tier1:
            if now + half >= self._last_read.get(tag, -math.inf) + self._rate(tag):
                self._last_read[tag] = now
                tags.append(tag)
                if group := self._group(tag):
                    groups.add(group["name"])
        for name in groups:
            self._group_reads[name] = (self._group_reads.get(name, (None, None))[1], now)
        if boundary:
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
        return [tag for tag in tags if self.missing.get(tag, 0) <= now], tick, boundary

    def snapshot(self, tags) -> dict:
        with self.lock:
            return {tag: list(self.cache[tag]) for tag in tags if tag in self.cache}

    def write(self, tag, value) -> Future:
        """Queues a write, run between the packets of the current tick; the Future gets
        (ok, error, kind, read-back value). Never retried."""
        future = Future()
        if self.state != "connected":
            future.set_result((False, "PLC not connected", "refused", None))
        else:
            self.writes.put((tag, value, future))
            self._wake.set()
        return future

    def type_info(self, tag) -> dict:
        """{"type", "dims"?} from the connected driver; a bit is BOOL, {"type": None} when unknown."""
        if BIT.match(tag):
            return {"type": "BOOL"}
        plc = self._plc
        if plc is None or not hasattr(plc, "get_tag_info"):
            return {"type": None}
        try:
            info = plc.get_tag_info(tag)
        except Exception:
            return {"type": None}
        bools = bool_array(info)  # ciplink's BOOL[n]: a DWORD array, its dims counting 32-bit words
        result = {"type": "BOOL" if bools else type_name(info) or None}
        if not tag.endswith("]"):  # a whole array, not an element
            if info.get("dim"):
                result["dims"] = list(info.get("dimensions") or [0] * info["dim"])[:info["dim"]]
            elif info.get("array"):
                result["dims"] = [info["array"]]
            if bools and "dims" in result:
                result["dims"][-1] *= 32
        return result

    def description(self, tag):
        return self._descriptions.get(tag)

    # the worker thread

    def _sleep(self, seconds) -> bool:
        """The default wait: returns early when a write is queued, True once stopped."""
        self._wake.clear()
        if self.writes.empty():  # checked after the clear, so a write queued now still wakes it
            self._wake.wait(seconds)
        return self._stop.is_set()

    def _idle(self, plc, start, tick) -> bool:
        """Sleeps until the next tick, running each write as it is queued; True once stopped."""
        while (remaining := tick - (self.clock() - start)) > 1e-4:  # not a float rounding error
            if self.wait(remaining):
                return True
            self._run_writes(plc)
        return False

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
                self._plc = plc
                self._set_state("connected")
                self._poll(plc)
            except Exception as err:  # CommError, or anything else the driver raised
                log.warning("%s: %r", self.name, err)
            finally:
                self._plc = None
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
        self.state = "stopped"  # later writes are refused
        self._fail_writes("server stopping")

    def _poll(self, plc):
        while not self._stop.is_set():
            start = self.clock()
            with self.lock:
                tags, tick, boundary = self._cycle_tags(start)
            services = 0
            if tags:
                results, services = self._read(plc, tags, start)
                self._apply(results, start)
            self.reads_per_cycle = services
            self.tags_per_cycle = len(tags)
            self._describe(plc)
            if boundary:
                self._check_version(plc)
            self._run_writes(plc)
            self.last_ok_ts = now_ms()
            elapsed = self.clock() - start
            self.last_cycle_ms = round(elapsed * 1000, 1)
            if elapsed > tick:
                self.overruns += 1
            elif self._idle(plc, start, tick):
                break
        self._run_writes(plc)  # stopping: the last queued writes (a momentary release) still go out

    def _apply(self, results, start, deadband=True):
        """Caches read results and publishes the changes: quality or error changed, or the value moved past
        the deadband from the last published value."""
        t = now_ms()
        changes = {}
        key = lambda e: (e[0], e[1], e[3])
        with self.lock:
            for tag, result in results.items():
                if tag not in self.tiers:  # unsubscribed during the read
                    continue
                old = self.cache.get(tag)
                entry = merge(old, result, t)
                if entry[1] == "missing":  # don't ask (and make the driver log) every tick
                    self.missing[tag] = start + self.missing_retry
                else:
                    self.missing.pop(tag, None)
                if old is None or key(old) != key(entry):
                    self.cache[tag] = entry
                elif deadband:
                    continue
                pub = self.published.get(tag)  # a read-back (no deadband) is published unless already published
                if pub is not None and (self._within_deadband(tag, pub, entry) if deadband else key(pub) == key(entry)):
                    continue
                self.published[tag] = entry
                changes[tag] = list(entry)
        if changes:
            self.on_values(self.name, changes)

    def _within_deadband(self, tag, pub, entry) -> bool:
        if self.rules is None or pub[1] != "good" or entry[1] != "good" or pub[3] != entry[3]:
            return False
        band = self.rules.effective(self.name, tag).get("deadband")
        number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        return bool(band) and number(pub[0]) and number(entry[0]) and abs(entry[0] - pub[0]) < band

    def _read(self, plc, tags, start):
        """({tag: Tag result}, read services sent), through the planner unless it is off. Queued writes run
        between the packet chunks, after the chunk's results are published."""
        if not self.planner or not hasattr(plc, "get_tag_info"):
            parts = [[(tag, [(tag, None)]) for tag in tags]]
        else:
            retry = [name for name, failed_at in self._no_group.items() if start - failed_at >= self.group_retry]
            if retry:  # read those whole again
                for name in retry:
                    del self._no_group[name]
                self._plans.clear()
            key = frozenset(tags)
            parts = self._plans.get(key)
            if parts is None:
                if len(self._plans) > 100:  # tier-2 slices and groups make a few sets per subscription change
                    self._plans.clear()
                budget = getattr(plc, "connection_size", None)
                budget = budget if isinstance(budget, int) and budget > 0 else planner.PACKET_BYTES
                together = None
                if self.rules is not None:
                    together = lambda path: self.rules.effective(self.name, path).get("together")
                reads = planner.plan(tags, plc.get_tag_info, self.whole_max_bytes, frozenset(self._no_group),
                                     budget, together)
                parts = planner.chunks(reads, plc.get_tag_info, budget)
                self._plans[key] = parts

        def between(partial):
            self._apply(partial, start)
            self._run_writes(plc)

        results, failed, services = planner.execute(plc, parts, between)
        if failed:
            self.group_fallbacks += len(failed)
            log.warning("%s: whole read of %s failed, reading the members instead", self.name, ", ".join(failed))
            self._no_group.update((name.lower(), start) for name in failed)
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
                future.set_result((False, str(err), "comm", None))
                raise
            if result.error is not None:
                future.set_result((False, str(result.error), result.error_kind.value, None))
                continue
            bit = BIT.match(tag)
            names = [tag, bit[1]] if bit else [tag]
            try:
                results = dict(zip(names, _as_list(plc.read(*names))))
            except Exception:
                future.set_result((True, None, None, None))
                raise
            value, quality, _ = to_value(results[tag])
            future.set_result((True, None, None, value if quality == "good" else None))
            self._apply(results, self.clock(), deadband=False)

    def _describe(self, plc):
        """Reads the controller descriptions of tags that entered tiers, in one call, and tells the hub."""
        with self.lock:
            tags = sorted(self._want_descriptions)
            self._want_descriptions.clear()
        if self.rules is not None:  # a rule description wins: no need to ask
            tags = [t for t in tags if not self.rules.effective(self.name, t).get("description")]
        tags = [t for t in tags if t not in self._descriptions]
        if not tags or not hasattr(plc, "get_tag_descriptions"):
            return
        try:
            results = plc.get_tag_descriptions(*tags)
            found = {t: (r.value if r.error is None else None) for t, r in zip(tags, results)}
        except Exception as err:
            log.info("%s: descriptions not read: %r", self.name, err)
            found = {}
        descriptions = {t: found.get(t) for t in tags}
        self._descriptions.update(descriptions)
        self.on_info(self.name, descriptions)

    def _check_version(self, plc, connecting=False):
        """Counts a reload and tells the clients when the driver's tag table version changed."""
        version = getattr(plc, "_tag_table_version", None)
        changed = version != self.version
        reloaded = changed and self.version is not None
        if reloaded:
            self.reloads += 1
            log.info("%s: tag table reloaded (version %s -> %s)", self.name, self.version, version)
        self.version = version
        if changed or connecting:
            self.tag_list = tag_list(plc)
            self.types = type_map(plc)
            with self.lock:
                self.missing.clear()  # a new or renamed tag may exist now
                self._no_group.clear()
                self._plans.clear()  # members or struct sizes may have changed
                if changed:  # descriptions are cached per version: ask again for the subscribed tags
                    self._descriptions = {}
                    self._want_descriptions.update(t for t in self.tiers if not BIT.match(t))
        if reloaded:
            self.on_meta(self.name)

    def _fail_writes(self, error):
        while True:
            try:
                _, _, future = self.writes.get_nowait()
            except queue.Empty:
                return
            future.set_result((False, error, "comm", None))

    def _mark_stale(self):
        changes = {}
        with self.lock:
            for tag, entry in self.cache.items():
                if entry[1] != "stale":
                    entry[1], entry[3] = "stale", None
                    self.published[tag] = list(entry)
                    changes[tag] = list(entry)
        if changes:
            self.on_values(self.name, changes)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            log.info("%s: %s", self.name, state)
            self.on_status(self.name, state)
