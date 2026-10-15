/** The WebSocket connection and the tag value store. No React in here. */

import { isDerived, type DerivedImpl, type DeriveGet, type TagName } from "./tree.js";

export type Quality = "good" | "bad" | "missing" | "stale" | "pending";

export interface TagError {
  kind: string;
  message: string;
}

export interface TagState<V = unknown> {
  /** `bad` keeps the last good value */
  value: V | null;
  quality: Quality;
  /** ms since the epoch of the last change, null before any value */
  t: number | null;
  /** why the quality is bad or missing, null otherwise */
  error: TagError | null;
}

/** What the server says about a subscribed tag (only the fields that apply). */
export interface TagInfo {
  type: string | null;
  writable?: boolean;
  min?: number;
  max?: number;
  units?: string;
  deadband?: number;
  description?: string;
  bits?: Record<string, number>;
  dims?: number[];
  rate_ms?: number;
  group?: string | null;
}

export type ConnectionState = "connecting" | "connected" | "disconnected" | "unauthorized";

export interface ServerInfo {
  state: ConnectionState;
  /** PLC name -> "connecting" | "connected" | "disconnected", from welcome and status messages */
  plcs: Record<string, string>;
  /** whether the token has any write patterns */
  canWrite: boolean;
}

export interface WriteResult {
  ok: boolean;
  error: string | null;
  /** null on success; `comm`, `not_allowed`, `not_writable`, `out_of_range`, `bad_value`, ... otherwise */
  kind: string | null;
  /** the value read back after the write, or null */
  value: unknown;
}

/** A momentary (push button) tag: `press` writes true and holds it, `release` writes false. */
export interface Momentary {
  press(): Promise<WriteResult>;
  release(): Promise<WriteResult>;
}

export interface TagStoreOptions {
  /** called with the PLC name when its tag table was reloaded */
  onMeta?: (plc: string) => void;
}

export const PENDING: TagState = Object.freeze({ value: null, quality: "pending", t: null, error: null }) as TagState;
const CYCLE: TagState = { value: null, quality: "bad", t: null, error: { kind: "derive", message: "cycle" } };
/** how often a pressed momentary tag sends `hold` */
export const HOLD_MS = 300;
const BACKOFF = [1000, 2000, 5000, 10000];
const RANK: Record<Quality, number> = { good: 0, stale: 1, missing: 2, bad: 3, pending: 4 };
const BIT = /^(.+)\.(\d+)$/;

type Listener = () => void;

const fail = (kind: string, error: string): WriteResult => ({ ok: false, error, kind, value: null });
const OK: WriteResult = { ok: true, error: null, kind: null, value: null };

function same(a: TagState, b: TagState, t = true) {
  return a.quality === b.quality && Object.is(a.value, b.value) && (!t || a.t === b.t) &&
    a.error?.kind === b.error?.kind && a.error?.message === b.error?.message;
}

/** The bit n of a word's state: numbers bit by bit (BigInt, so LINT works), arrays (DWORD) by element. */
function bitOf(word: TagState, n: number): TagState {
  const v = word.value;
  if (v === null || v === undefined) return { ...word, value: null };
  if (typeof v === "number" && Number.isFinite(v)) return { ...word, value: ((BigInt(Math.trunc(v)) >> BigInt(n)) & 1n) === 1n };
  if (Array.isArray(v)) return { ...word, value: !!v[n] };
  return { value: null, quality: "bad", t: word.t, error: { kind: "bad_request", message: "not an integer" } };
}

/** The first number outside [min, max] in a value (numbers in lists and dicts too). */
function outOfRange(value: unknown, min?: number, max?: number): boolean {
  if (typeof value === "number") return (min !== undefined && value < min) || (max !== undefined && value > max);
  if (value && typeof value === "object") return Object.values(value).some((v) => outOfRange(v, min, max));
  return false;
}

/** The HTTP base of a tag server from its WebSocket URL: ws -> http, wss -> https, a trailing /ws dropped. */
export function httpBase(wsUrl: string): string {
  return wsUrl.replace(/^ws(s?):/, "http$1:").replace(/\/ws\/?$/, "").replace(/\/$/, "");
}

interface Derived {
  ref: DerivedImpl;
  state: TagState;
  deps: Map<unknown, { name: TagName; off: () => void; release?: () => void }>;
  tiers: Map<number, number>;
  listeners: Set<Listener>;
}

const keyOf = (name: TagName): unknown => (isDerived(name) ? name : String(name));
const minTier = (tiers: Map<number, number>) => (tiers.size ? Math.min(...tiers.keys()) : undefined);

export class TagStore {
  info: ServerInfo = { state: "disconnected", plcs: {}, canWrite: false };
  private values = new Map<string, TagState>();
  private listeners = new Map<string, Set<Listener>>();
  private infoListeners = new Set<Listener>();
  private metaListeners = new Set<(plc: string) => void>();
  private infos = new Map<string, TagInfo>();
  private tagInfoListeners = new Map<string, Set<Listener>>();
  // bit states and infos, cached against the word's so snapshots stay stable
  private bits = new Map<string, { src: TagState; state: TagState }>();
  private bitInfos = new Map<string, { src: TagInfo | undefined; info: TagInfo | null }>();
  private derived = new Map<DerivedImpl, Derived>();
  private computing = new Set<DerivedImpl>();
  // derived refs read with get() only: computed on each get, with no entry or listeners that would never be
  // disposed; the last state is kept (weakly) so an unchanged result is the same object
  private loose = new WeakMap<DerivedImpl, TagState>();

  // subscriptions: tag -> tier -> count; tags are sent in groups, one server subscription id per group
  private refs = new Map<string, Map<number, number>>();
  private current = new Map<string, { id: number; tier: number }>();
  private groups = new Map<number, { tier: number; tags: Set<string> }>();
  private dirty = new Set<string>();
  private flushQueued = false;
  // page subscriptions (the tags the server learned for a page): page -> server id and users
  private pageSubs = new Map<string, { id: number; tier: number; count: number }>();

  /** The current page (route path): tier-1 subscriptions sent while it is set carry it, so the server learns
   * which tags each page shows. */
  page: string | undefined;

  private nextId = 1;
  private writes = new Map<number, (result: WriteResult) => void>();
  private ws: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private closed = true;

  constructor(
    readonly url: string,
    readonly token: string,
    public options: TagStoreOptions = {},
  ) {}

  // values

  /** The tag's state. A bit `X.<n>` is computed from the word `X`; a derived ref from its inputs. */
  get(name: TagName): TagState {
    if (isDerived(name)) {
      const e = this.derived.get(name);
      if (e) return e.state;
      const state = this.computing.has(name) ? CYCLE : this.evaluate(name).state;
      const prev = this.loose.get(name);
      if (prev && same(prev, state)) return prev;
      this.loose.set(name, state);
      return state;
    }
    const s = String(name);
    const bit = BIT.exec(s);
    if (!bit) return this.values.get(s) ?? PENDING;
    const word = this.values.get(bit[1]) ?? PENDING;
    const c = this.bits.get(s);
    if (c?.src === word) return c.state;
    let state = bitOf(word, Number(bit[2]));
    // an unchanged bit keeps its state (and t) when other bits of the word change
    if (c && same(c.state, state, false)) state = c.state;
    this.bits.set(s, { src: word, state });
    return state;
  }

  listen(name: TagName, listener: Listener): () => void {
    if (isDerived(name)) {
      const e = this.entry(name);
      e.listeners.add(listener);
      return () => {
        e.listeners.delete(listener);
        this.dispose(e);
      };
    }
    const s = String(name);
    const bit = BIT.exec(s);
    if (!bit) return this.listenKey(this.listeners, s, listener);
    let prev = this.get(s);
    return this.listenKey(this.listeners, bit[1], () => {
      const now = this.get(s);
      if (now !== prev) {
        prev = now;
        listener();
      }
    });
  }

  listenInfo(listener: Listener): () => void {
    this.infoListeners.add(listener);
    return () => this.infoListeners.delete(listener);
  }

  /** Called with the PLC name when its tag table was reloaded. */
  listenMeta(listener: (plc: string) => void): () => void {
    this.metaListeners.add(listener);
    return () => this.metaListeners.delete(listener);
  }

  private listenKey(map: Map<string, Set<Listener>>, key: string, listener: Listener): () => void {
    let set = map.get(key);
    if (!set) map.set(key, (set = new Set()));
    set.add(listener);
    return () => {
      set!.delete(listener);
      if (!set!.size && map.get(key) === set) map.delete(key);
    };
  }

  private set(name: string, state: TagState, notify = true) {
    this.values.set(name, state);
    if (notify) this.listeners.get(name)?.forEach((l) => l());
  }

  private setInfo(patch: Partial<ServerInfo>) {
    this.info = { ...this.info, ...patch };
    this.infoListeners.forEach((l) => l());
  }

  // tag info

  /** The server's info record for a subscribed tag, or null. A bit's is built from its word's, without
   * `writable`: a rule or token pattern can allow the bit and not the word, so the server decides. */
  tagInfo(name: TagName): TagInfo | null {
    if (isDerived(name)) return null;
    const s = String(name);
    const bit = BIT.exec(s);
    if (!bit) return this.infos.get(s) ?? null;
    const word = this.infos.get(bit[1]);
    const c = this.bitInfos.get(s);
    if (c && c.src === word) return c.info;
    const info: TagInfo | null = word
      ? { type: "BOOL", rate_ms: word.rate_ms, group: word.group ?? null }
      : null;
    this.bitInfos.set(s, { src: word, info });
    return info;
  }

  listenTagInfo(name: TagName, listener: Listener): () => void {
    if (isDerived(name)) return () => {};
    const s = String(name);
    return this.listenKey(this.tagInfoListeners, BIT.exec(s)?.[1] ?? s, listener);
  }

  // subscriptions

  /** Counts one more user of the tag; returns the release function. A bit acquires its word, a derived ref
   * its inputs. */
  acquire(name: TagName, tier = 1): () => void {
    if (isDerived(name)) return this.acquireDerived(name, tier);
    const s = String(name);
    const key = BIT.exec(s)?.[1] ?? s;
    let tiers = this.refs.get(key);
    if (!tiers) this.refs.set(key, (tiers = new Map()));
    tiers.set(tier, (tiers.get(tier) ?? 0) + 1);
    this.touch(key);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const n = tiers!.get(tier)! - 1;
      if (n) tiers!.set(tier, n);
      else tiers!.delete(tier);
      if (!tiers!.size) this.refs.delete(key);
      this.touch(key);
    };
  }

  /** Subscribes the tags the server learned for `page` (from tier-1 subscriptions sent while that page was
   * current), at tier 2 by default; returns the release function. Empty until the page has been visited. */
  acquirePage(page: string, tier = 2): () => void {
    let sub = this.pageSubs.get(page);
    if (!sub) {
      this.pageSubs.set(page, (sub = { id: this.nextId++, tier, count: 0 }));
      this.send({ op: "sub", id: sub.id, page, tier });
    }
    sub.count++;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      if (--sub!.count === 0) {
        this.pageSubs.delete(page);
        this.send({ op: "unsub", id: sub!.id });
      }
    };
  }

  private touch(name: string) {
    this.dirty.add(name);
    if (!this.flushQueued) {
      this.flushQueued = true;
      queueMicrotask(() => this.flush());
    }
  }

  /** Sends the subscription changes made since the last flush: one sub per tier, one unsub or re-sub per changed group. */
  private flush() {
    this.flushQueued = false;
    const adds = new Map<number, string[]>();
    const changed = new Set<number>();
    for (const name of this.dirty) {
      const tiers = this.refs.get(name);
      const want = tiers ? Math.min(...tiers.keys()) : undefined;
      const cur = this.current.get(name);
      if (cur?.tier === want) continue;
      if (cur) {
        this.groups.get(cur.id)!.tags.delete(name);
        this.current.delete(name);
        changed.add(cur.id);
      }
      if (want !== undefined) {
        if (!adds.has(want)) adds.set(want, []);
        adds.get(want)!.push(name);
      }
    }
    this.dirty.clear();
    for (const id of changed) {
      const group = this.groups.get(id)!;
      if (group.tags.size) {
        this.sendSub(id); // reusing an id replaces that subscription
      } else {
        this.groups.delete(id);
        this.send({ op: "unsub", id });
      }
    }
    for (const [tier, tags] of adds) {
      const id = this.nextId++;
      this.groups.set(id, { tier, tags: new Set(tags) });
      for (const name of tags) this.current.set(name, { id, tier });
      this.sendSub(id);
    }
  }

  private sendSub(id: number) {
    const group = this.groups.get(id)!;
    const page = group.tier === 1 && this.page !== undefined ? { page: this.page } : {};
    this.send({ op: "sub", id, tags: [...group.tags], tier: group.tier, ...page });
  }

  // derived values

  private entry(ref: DerivedImpl): Derived {
    let e = this.derived.get(ref);
    if (!e) {
      const state = this.loose.get(ref) ?? PENDING;
      this.derived.set(ref, (e = { ref, state, deps: new Map(), tiers: new Map(), listeners: new Set() }));
      this.recompute(e);
    }
    return e;
  }

  private acquireDerived(ref: DerivedImpl, tier: number): () => void {
    const e = this.entry(ref);
    const change = (d: number) => {
      const before = minTier(e.tiers);
      const n = (e.tiers.get(tier) ?? 0) + d;
      if (n) e.tiers.set(tier, n);
      else e.tiers.delete(tier);
      const after = minTier(e.tiers);
      if (after === before) return;
      for (const dep of e.deps.values()) {
        const old = dep.release;
        dep.release = after === undefined ? undefined : this.acquire(dep.name, after);
        old?.();
      }
    };
    change(1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      change(-1);
      this.dispose(e);
    };
  }

  private dispose(e: Derived) {
    if (e.listeners.size || e.tiers.size || this.derived.get(e.ref) !== e) return;
    this.derived.delete(e.ref);
    for (const dep of e.deps.values()) {
      dep.off();
      dep.release?.();
    }
    e.deps.clear();
  }

  /** Runs the derive function: its state and the inputs it read. */
  private evaluate(ref: DerivedImpl): { state: TagState; seen: Map<unknown, TagName> } {
    this.computing.add(ref);
    const seen = new Map<unknown, TagName>();
    let worst: TagState | null = null;
    let pending = false;
    let t: number | null = null;
    const get: DeriveGet = (input) => {
      const s = this.get(input);
      seen.set(keyOf(input), input);
      if (s.quality === "pending") pending = true;
      else if (!worst || RANK[s.quality] > RANK[worst.quality]) worst = s;
      if (s.t !== null && (t === null || s.t > t)) t = s.t;
      return s.value as any;
    };
    let state: TagState;
    try {
      const value = ref.fn(get);
      const w = worst as TagState | null;
      state = pending ? PENDING : { value, quality: w?.quality ?? "good", t, error: w?.error ?? null };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      state = pending ? PENDING : { value: null, quality: "bad", t, error: { kind: "derive", message } };
    } finally {
      this.computing.delete(ref);
    }
    return { state, seen };
  }

  /** Evaluates the entry, tracking what it reads, and notifies when the result changed. */
  private recompute(e: Derived) {
    if (this.computing.has(e.ref)) return;
    const { seen, ...result } = this.evaluate(e.ref);
    let state = result.state;
    if (this.derived.get(e.ref) !== e) return; // disposed while computing
    const tier = minTier(e.tiers);
    for (const [key, dep] of e.deps) {
      if (seen.has(key)) continue;
      e.deps.delete(key);
      dep.off();
      dep.release?.();
    }
    for (const [key, name] of seen) {
      if (e.deps.has(key)) continue;
      const release = tier === undefined ? undefined : this.acquire(name, tier);
      e.deps.set(key, { name, release, off: this.listen(name, () => this.recompute(e)) });
    }
    if (this.cyclic(e)) state = CYCLE;
    if (same(state, e.state)) return;
    e.state = state;
    e.listeners.forEach((l) => l());
  }

  private cyclic(e: Derived): boolean {
    const seen = new Set<Derived>();
    const walk = (x: Derived): boolean => {
      for (const key of x.deps.keys()) {
        const d = isDerived(key) ? this.derived.get(key) : undefined;
        if (!d) continue;
        if (d === e) return true;
        if (seen.has(d)) continue;
        seen.add(d);
        if (walk(d)) return true;
      }
      return false;
    };
    return walk(e);
  }

  // writes

  /** The local write pre-check: the server's kinds when its info says not writable or out of range, else null. */
  check(name: TagName, value: unknown): WriteResult | null {
    if (isDerived(name)) return fail("not_writable", "not writable");
    const info = this.tagInfo(name);
    if (info?.writable === false) return fail("not_writable", "not writable");
    if (info && typeof value !== "boolean" && outOfRange(value, info.min, info.max)) return fail("out_of_range", "out of range");
    return null;
  }

  write(name: TagName, value: unknown): Promise<WriteResult> {
    return this.request({ op: "write", tag: String(name), value }, name, value);
  }

  /** A handle for a momentary tag: `press` sends press and then hold every HOLD_MS until `release`. */
  momentary(name: TagName): Momentary {
    const tag = String(name);
    let timer: ReturnType<typeof setInterval> | undefined;
    return {
      press: () => {
        const result = this.request({ op: "momentary", tag, action: "press" }, name, true);
        if (timer === undefined && this.info.state === "connected" && !this.check(name, true)) {
          timer = setInterval(() => this.send({ op: "momentary", tag, action: "hold" }), HOLD_MS);
        }
        return result;
      },
      release: () => {
        if (timer === undefined) return Promise.resolve(OK);
        clearInterval(timer);
        timer = undefined;
        return this.request({ op: "momentary", tag, action: "release" }, name, false);
      },
    };
  }

  private request(message: object, name: TagName, value: unknown): Promise<WriteResult> {
    if (this.info.state !== "connected") return Promise.resolve(fail("comm", "not connected"));
    const rejected = this.check(name, value);
    if (rejected) return Promise.resolve(rejected);
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.writes.set(id, resolve);
      this.send({ ...message, id });
    });
  }

  // connection

  connect() {
    this.closed = false;
    this.open();
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.lost("disconnected");
  }

  private open() {
    this.setInfo({ state: "connecting" });
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ op: "hello", v: 1, token: this.token }));
    ws.onmessage = (event) => {
      if (this.ws === ws) this.receive(JSON.parse(event.data));
    };
    ws.onclose = (event) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (event.code === 4401) {
        this.closed = true;
        this.lost("unauthorized");
        return;
      }
      this.lost("disconnected");
      if (!this.closed) {
        const delay = BACKOFF[Math.min(this.attempt++, BACKOFF.length - 1)];
        this.timer = setTimeout(() => this.open(), delay);
      }
    };
  }

  /** Marks every value stale (keeping it), the PLC states unknown, and fails the writes in flight. */
  private lost(state: ConnectionState) {
    for (const [name, entry] of this.values) {
      if (entry.quality !== "stale") this.set(name, { ...entry, quality: "stale", error: null });
    }
    for (const resolve of this.writes.values()) resolve(fail("comm", "disconnected"));
    this.writes.clear();
    // without the server we can't know the PLCs' states, so don't keep showing "connected"
    const plcs = Object.fromEntries(Object.keys(this.info.plcs).map((name) => [name, "unknown"]));
    this.setInfo({ state, plcs });
  }

  private send(message: object) {
    if (this.ws && this.info.state === "connected") this.ws.send(JSON.stringify(message));
  }

  private receive(msg: any) {
    switch (msg.op) {
      case "welcome":
        this.attempt = 0;
        this.setInfo({ state: "connected", plcs: msg.plcs ?? {}, canWrite: !!msg.write });
        for (const id of this.groups.keys()) this.sendSub(id);
        for (const [page, { id, tier }] of this.pageSubs) this.send({ op: "sub", id, page, tier });
        break;
      case "values": {
        // all values first, then the listeners, so derived values never see half a message
        const d = msg.d as Record<string, [unknown, Quality, number, TagError | null | undefined]>;
        for (const [name, [value, quality, t, error]] of Object.entries(d)) this.set(name, { value, quality, t, error: error ?? null }, false);
        for (const name of Object.keys(d)) this.listeners.get(name)?.forEach((l) => l());
        break;
      }
      case "info":
        for (const [name, record] of Object.entries(msg.d as Record<string, TagInfo>)) {
          this.infos.set(name, record);
          this.tagInfoListeners.get(name)?.forEach((l) => l());
        }
        break;
      case "ack":
      case "error": {
        const resolve = this.writes.get(msg.id);
        if (resolve) {
          this.writes.delete(msg.id);
          const ok = msg.op === "ack" && !!msg.ok;
          resolve({ ok, error: msg.error ?? null, kind: msg.kind ?? (ok ? null : "other"), value: msg.value ?? null });
        }
        break;
      }
      case "status":
        this.setInfo({ plcs: msg.plcs ?? {} });
        break;
      case "meta":
        this.options.onMeta?.(msg.plc);
        this.metaListeners.forEach((l) => l(msg.plc));
        break;
    }
  }
}
