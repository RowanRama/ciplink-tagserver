/** The WebSocket connection and the tag value store. No React in here. */

export type Quality = "good" | "bad" | "missing" | "stale" | "pending";

export interface TagState {
  value: unknown;
  quality: Quality;
  /** ms since the epoch of the last change, null before any value */
  t: number | null;
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
}

export interface TagStoreOptions {
  /** called with the PLC name when its tag table was reloaded */
  onMeta?: (plc: string) => void;
}

export const PENDING: TagState = Object.freeze({ value: null, quality: "pending", t: null }) as TagState;
const BACKOFF = [1000, 2000, 5000, 10000];

type Listener = () => void;

export class TagStore {
  info: ServerInfo = { state: "disconnected", plcs: {}, canWrite: false };
  private values = new Map<string, TagState>();
  private listeners = new Map<string, Set<Listener>>();
  private infoListeners = new Set<Listener>();

  // subscriptions: tag -> tier -> count; tags are sent in groups, one server subscription id per group
  private refs = new Map<string, Map<number, number>>();
  private current = new Map<string, { id: number; tier: number }>();
  private groups = new Map<number, { tier: number; tags: Set<string> }>();
  private dirty = new Set<string>();
  private flushQueued = false;

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

  get(name: string): TagState {
    return this.values.get(name) ?? PENDING;
  }

  listen(name: string, listener: Listener): () => void {
    let set = this.listeners.get(name);
    if (!set) this.listeners.set(name, (set = new Set()));
    set.add(listener);
    return () => {
      set!.delete(listener);
      if (!set!.size) this.listeners.delete(name);
    };
  }

  listenInfo(listener: Listener): () => void {
    this.infoListeners.add(listener);
    return () => this.infoListeners.delete(listener);
  }

  private set(name: string, state: TagState) {
    this.values.set(name, state);
    this.listeners.get(name)?.forEach((l) => l());
  }

  private setInfo(patch: Partial<ServerInfo>) {
    this.info = { ...this.info, ...patch };
    this.infoListeners.forEach((l) => l());
  }

  // subscriptions

  /** Counts one more user of the tag; returns the release function. */
  acquire(name: string, tier = 1): () => void {
    let tiers = this.refs.get(name);
    if (!tiers) this.refs.set(name, (tiers = new Map()));
    tiers.set(tier, (tiers.get(tier) ?? 0) + 1);
    this.touch(name);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const n = tiers!.get(tier)! - 1;
      if (n) tiers!.set(tier, n);
      else tiers!.delete(tier);
      if (!tiers!.size) this.refs.delete(name);
      this.touch(name);
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
    this.send({ op: "sub", id, tags: [...group.tags], tier: group.tier });
  }

  // writes

  write(name: string, value: unknown): Promise<WriteResult> {
    if (this.info.state !== "connected") return Promise.resolve({ ok: false, error: "not connected" });
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.writes.set(id, resolve);
      this.send({ op: "write", id, tag: name, value });
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
      if (entry.quality !== "stale") this.set(name, { ...entry, quality: "stale" });
    }
    for (const resolve of this.writes.values()) resolve({ ok: false, error: "disconnected" });
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
        break;
      case "values":
        for (const [name, [value, quality, t]] of Object.entries(msg.d as Record<string, [unknown, Quality, number]>)) {
          this.set(name, { value, quality, t });
        }
        break;
      case "ack":
      case "error": {
        const resolve = this.writes.get(msg.id);
        if (resolve) {
          this.writes.delete(msg.id);
          resolve({ ok: msg.op === "ack" && !!msg.ok, error: msg.error ?? null });
        }
        break;
      }
      case "status":
        this.setInfo({ plcs: msg.plcs ?? {} });
        break;
      case "meta":
        this.options.onMeta?.(msg.plc);
        break;
    }
  }
}
