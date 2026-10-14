"""One thread per PLC: owns the driver, polls the subscribed tags, runs the queued writes."""

import logging
import queue
import threading
import time
from concurrent.futures import Future

from .values import tag_list, to_value

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


class PlcWorker:
    backoff = (1, 2, 5, 10)  # seconds between reconnect attempts, the last one repeats
    missing_retry = 10  # seconds before a tag the PLC doesn't have is asked for again, unless the tag table reloads

    def __init__(self, cfg, factory, cycle_ms, on_values=None, on_status=None, on_meta=None):
        self.name = cfg.name
        self.cfg = cfg
        self.factory = factory  # cfg -> driver, not yet opened
        self.cycle = cycle_ms / 1000
        self.on_values = on_values or (lambda plc, changes: None)  # called from this thread
        self.on_status = on_status or (lambda plc, state: None)
        self.on_meta = on_meta or (lambda plc: None)

        self.state = "connecting"
        self.lock = threading.Lock()  # guards tags and cache
        self.tags = set()  # tag names polled each cycle
        self.cache = {}  # tag -> [value, quality, t]
        self.missing = {}  # tag -> monotonic time to ask for it again; skipped until then
        self.writes = queue.Queue()  # (tag, value, Future)
        self.tag_list = []  # for GET /tags, rebuilt here after a connect or reload

        self.last_cycle_ms = None
        self.last_ok_ts = None
        self.reads_per_cycle = 0
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

    def add_tags(self, tags):
        with self.lock:
            self.tags.update(tags)

    def remove_tags(self, tags):
        with self.lock:
            for tag in tags:
                self.tags.discard(tag)
                self.cache.pop(tag, None)
                self.missing.pop(tag, None)

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
                tags = [tag for tag in self.tags if self.missing.get(tag, 0) <= start]
            if tags:
                results = plc.read(*tags)
                if not isinstance(results, list):
                    results = [results]
                t = now_ms()
                changes = {}
                with self.lock:
                    for tag, result in zip(tags, results):
                        if tag not in self.tags:  # unsubscribed during the read
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
            self.reads_per_cycle = len(tags)
            self._check_version(plc)
            self._run_writes(plc)
            self.last_ok_ts = now_ms()
            elapsed = time.monotonic() - start
            self.last_cycle_ms = round(elapsed * 1000, 1)
            if elapsed > self.cycle:
                self.overruns += 1
            elif self.wait(self.cycle - elapsed):
                return

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
