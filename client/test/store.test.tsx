import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { derive, httpBase, tagTree, type IntRef, type TagArray, type TagRef } from "../src/index.js";
import { FakeWebSocket, connectedStore, flush } from "./helpers.js";

beforeEach(() => {
  FakeWebSocket.all = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("values", () => {
  it("carry an error, keep the value the server keeps on bad, and clear the error on stale", async () => {
    const { store, ws } = await connectedStore();
    store.acquire("A");
    ws.receive({ op: "values", d: { A: [5, "good", 1, null] } });
    expect(store.get("A")).toEqual({ value: 5, quality: "good", t: 1, error: null });
    ws.receive({ op: "values", d: { A: [5, "bad", 2, { kind: "comm", message: "timeout" }] } });
    expect(store.get("A")).toEqual({ value: 5, quality: "bad", t: 2, error: { kind: "comm", message: "timeout" } });
    ws.receive({ op: "values", d: { B: [1, "good", 3] } }); // a 0.2 server's 3-tuple
    expect(store.get("B").error).toBeNull();
    ws.drop();
    expect(store.get("A")).toEqual({ value: 5, quality: "stale", t: 2, error: null });
  });

  it("are all applied before any listener runs (a coalesced message is never torn)", async () => {
    const { store, ws } = await connectedStore();
    const sum = derive((get) => (get<number>("A") ?? 0) + (get<number>("B") ?? 0));
    const seen: unknown[] = [];
    store.listen(sum, () => seen.push(store.get(sum).value));
    ws.receive({ op: "values", d: { A: [1, "good", 1], B: [1, "good", 1] } });
    ws.receive({ op: "values", d: { A: [2, "good", 2], B: [2, "good", 2] } });
    expect(seen).toEqual([2, 4]);
  });
});

describe("bits from words", () => {
  it("subscribe the word once and compute the bits client-side, BigInt-safe", async () => {
    const { store, ws } = await connectedStore();
    store.acquire("W.0");
    store.acquire("W.60");
    store.acquire("PLC1/S.3", 2);
    await flush();
    expect(ws.ops("sub")).toEqual([
      { op: "sub", id: 1, tags: ["W"], tier: 1 },
      { op: "sub", id: 2, tags: ["PLC1/S"], tier: 2 },
    ]);
    ws.receive({ op: "values", d: { W: [2 ** 60 + 2 ** 8, "good", 1], "PLC1/S": [8, "good", 1] } });
    expect(store.get("W.0").value).toBe(false);
    expect(store.get("W.8").value).toBe(true);
    expect(store.get("W.60").value).toBe(true);
    expect(store.get("PLC1/S.3")).toEqual({ value: true, quality: "good", t: 1, error: null });
    ws.receive({ op: "values", d: { W: [-1, "good", 1] } }); // two's complement: every bit set
    expect(store.get("W.63").value).toBe(true);
  });

  it("keep a bit's state when only other bits change, and only notify on its changes", async () => {
    const { store, ws } = await connectedStore();
    store.acquire("W.0");
    ws.receive({ op: "values", d: { W: [1, "good", 1] } });
    const calls = vi.fn();
    store.listen("W.0", calls);
    const before = store.get("W.0");
    ws.receive({ op: "values", d: { W: [3, "good", 2] } });
    expect(store.get("W.0")).toBe(before);
    expect(calls).not.toHaveBeenCalled();
    ws.receive({ op: "values", d: { W: [2, "good", 3] } });
    expect(store.get("W.0")).toEqual({ value: false, quality: "good", t: 3, error: null });
    expect(calls).toHaveBeenCalledTimes(1);
  });

  it("read DWORD arrays by element and give bad for anything else", async () => {
    const { store, ws } = await connectedStore();
    ws.receive({ op: "values", d: { D: [[false, true], "good", 1], S: ["text", "good", 1], M: [null, "missing", 1] } });
    expect(store.get("D.1").value).toBe(true);
    expect(store.get("S.1")).toMatchObject({ value: null, quality: "bad", error: { kind: "bad_request" } });
    expect(store.get("M.1")).toMatchObject({ value: null, quality: "missing" });
    expect(store.get("Missing.1").quality).toBe("pending");
  });
});

describe("info", () => {
  it("is kept per name, and a bit's is built from its word's", async () => {
    const { store, ws } = await connectedStore();
    expect(store.tagInfo("W")).toBeNull();
    const calls = vi.fn();
    store.listenTagInfo("W.2", calls);
    ws.receive({ op: "info", d: { W: { type: "DINT", writable: true, rate_ms: 250, group: null, bits: { EStop: 0 }, units: "x" } } });
    expect(store.tagInfo("W")!.type).toBe("DINT");
    // no writable: a rule or token pattern may allow the bit and not its word, so the server decides
    expect(store.tagInfo("W.2")).toEqual({ type: "BOOL", rate_ms: 250, group: null });
    expect(store.tagInfo("W.2")).toBe(store.tagInfo("W.2"));
    expect(calls).toHaveBeenCalledTimes(1);
    expect(store.tagInfo(derive(() => 1))).toBeNull();
  });
});

describe("write pre-check", () => {
  it("rejects locally when info says not writable or out of range, and sends nothing", async () => {
    const { store, ws } = await connectedStore();
    ws.receive({
      op: "info",
      d: { RO: { type: "REAL", writable: false }, SP: { type: "REAL", writable: true, min: 0, max: 100 }, W: { type: "DINT", writable: true, min: 0, max: 5 } },
    });
    expect(await store.write("RO", 1)).toEqual({ ok: false, error: "not writable", kind: "not_writable", value: null });
    expect(await store.write("SP", 101)).toEqual({ ok: false, error: "out of range", kind: "out_of_range", value: null });
    expect(await store.write("SP", [1, -1])).toMatchObject({ kind: "out_of_range" });
    expect(await store.write(derive(() => 1), 1)).toMatchObject({ kind: "not_writable" });
    expect(ws.ops("write")).toEqual([]);

    // bits are never range checked and go to the server as X.<n>
    const bit = store.write("W.7", true);
    const sent = ws.ops("write")[0];
    expect(sent).toMatchObject({ op: "write", tag: "W.7", value: true });
    ws.receive({ op: "ack", id: sent.id, ok: true, error: null, kind: null, value: 128 });
    expect(await bit).toEqual({ ok: true, error: null, kind: null, value: 128 });

    const ok = store.write("SP", 50);
    const s2 = ws.ops("write")[1];
    ws.receive({ op: "ack", id: s2.id, ok: false, error: "bad", kind: "bad_value", value: null });
    expect(await ok).toEqual({ ok: false, error: "bad", kind: "bad_value", value: null });
  });
});

describe("bit writes", () => {
  it("are not rejected locally because the word is not writable", async () => {
    const { store, ws } = await connectedStore();
    ws.receive({ op: "info", d: { Cmd: { type: "DINT", writable: false } } });
    void store.write("Cmd.0", true);
    void store.momentary("Cmd.3").press();
    expect(ws.ops("write")).toMatchObject([{ tag: "Cmd.0", value: true }]);
    expect(ws.ops("momentary")).toMatchObject([{ tag: "Cmd.3", action: "press" }]);
  });
});

describe("derive", () => {
  it("is pending while an input is, then takes the worst quality and the latest t", async () => {
    const { store, ws } = await connectedStore();
    const avg = derive((get) => ((get<number>("A") ?? 0) + (get<number>("B") ?? 0)) / 2, "avg");
    expect(String(avg)).toBe("avg");
    const release = store.acquire(avg, 2);
    await flush();
    expect(ws.ops("sub")).toEqual([{ op: "sub", id: 1, tags: ["A", "B"], tier: 2 }]);
    expect(store.get(avg).quality).toBe("pending");
    ws.receive({ op: "values", d: { A: [2, "good", 5] } });
    expect(store.get(avg)).toEqual({ value: null, quality: "pending", t: null, error: null });
    ws.receive({ op: "values", d: { B: [4, "stale", 7] } });
    expect(store.get(avg)).toEqual({ value: 3, quality: "stale", t: 7, error: null });
    ws.receive({ op: "values", d: { A: [2, "bad", 9, { kind: "comm", message: "x" }] } });
    expect(store.get(avg)).toEqual({ value: 3, quality: "bad", t: 9, error: { kind: "comm", message: "x" } });
    ws.receive({ op: "values", d: { B: [null, "missing", 10, { kind: "not_found", message: "y" }] } });
    expect(store.get(avg).quality).toBe("bad"); // bad > missing
    release();
    await flush();
    expect(ws.ops("unsub")).toEqual([{ op: "unsub", id: 1 }]);
  });

  it("tracks dependencies again on every run (conditional reads)", async () => {
    const { store, ws } = await connectedStore();
    let runs = 0;
    const pick = derive((get) => {
      runs++;
      return get("Sel") ? get("A") : get("B");
    });
    const calls = vi.fn();
    store.listen(pick, calls);
    store.acquire(pick);
    await flush();
    expect(ws.ops("sub").slice(-1)[0].tags).toEqual(["Sel", "B"]); // Sel pending reads as null
    ws.receive({ op: "values", d: { Sel: [false, "good", 1] } });
    await flush();
    ws.receive({ op: "values", d: { B: [20, "good", 1] } });
    expect(store.get(pick).value).toBe(20);
    ws.receive({ op: "values", d: { Sel: [true, "good", 2] } });
    await flush();
    ws.receive({ op: "values", d: { A: [10, "good", 2] } });
    expect(store.get(pick).value).toBe(10);
    // B is no longer read: its changes don't re-run fn and it is unsubscribed
    const n = runs;
    ws.receive({ op: "values", d: { B: [21, "good", 3] } });
    expect(runs).toBe(n);
    expect(ws.sent.filter((m) => m.op === "sub" || m.op === "unsub")).toEqual([
      { op: "sub", id: 1, tags: ["Sel", "B"], tier: 1 },
      { op: "sub", id: 1, tags: ["Sel"], tier: 1 },
      { op: "sub", id: 2, tags: ["A"], tier: 1 },
    ]);
  });

  it("gives bad with kind derive when fn throws, and recovers", async () => {
    const { store, ws } = await connectedStore();
    const d = derive((get) => {
      const v = get<number>("A");
      if (v! < 0) throw new Error("negative");
      return Math.sqrt(v!);
    });
    store.listen(d, () => {});
    ws.receive({ op: "values", d: { A: [-1, "good", 1] } });
    expect(store.get(d)).toEqual({ value: null, quality: "bad", t: 1, error: { kind: "derive", message: "negative" } });
    ws.receive({ op: "values", d: { A: [9, "good", 2] } });
    expect(store.get(d)).toEqual({ value: 3, quality: "good", t: 2, error: null });
  });

  it("nests, and marks cycles bad instead of looping", async () => {
    const { store, ws } = await connectedStore();
    const double = derive((get) => (get<number>("A") ?? 0) * 2);
    const plusOne = derive((get) => (get(double) ?? 0) + 1);
    store.listen(plusOne, () => {});
    ws.receive({ op: "values", d: { A: [5, "good", 1] } });
    expect(store.get(plusOne).value).toBe(11);

    const self: any = derive((get) => (get<number>(self) ?? 0) + 1);
    store.listen(self, () => {});
    expect(store.get(self)).toMatchObject({ quality: "bad", error: { kind: "derive", message: "cycle" } });

    const a: any = derive((get) => get(b));
    const b: any = derive((get) => get(a));
    store.listen(a, () => {});
    expect(store.get(a)).toMatchObject({ quality: "bad", error: { message: "cycle" } });
    expect(store.get(b)).toMatchObject({ quality: "bad", error: { message: "cycle" } });
  });

  it("works with bits and tree refs as inputs, and never reaches the server", async () => {
    const { store, ws } = await connectedStore();
    const tree = tagTree<{ W: IntRef<false> }>(null);
    const any = derive((get) => get(tree.W.bit(1)) || get(tree.W.bit(2)), "anyFault");
    store.acquire(any);
    await flush();
    expect(ws.ops("sub").slice(-1)[0].tags).toEqual(["W"]);
    ws.receive({ op: "values", d: { W: [4, "good", 1] } });
    expect(store.get(any).value).toBe(true);
    expect(JSON.stringify(ws.sent)).not.toContain("anyFault");
  });
});

describe("momentary", () => {
  it("presses, holds every 300 ms and releases once", async () => {
    vi.useFakeTimers();
    const { store, ws } = await connectedStore();
    const m = store.momentary("Bench.StartPB");
    const pressed = m.press();
    const press = ws.ops("momentary")[0];
    expect(press).toMatchObject({ op: "momentary", tag: "Bench.StartPB", action: "press" });
    ws.receive({ op: "ack", id: press.id, ok: true, error: null, kind: null, value: true });
    expect(await pressed).toMatchObject({ ok: true, value: true });
    vi.advanceTimersByTime(299);
    expect(ws.ops("momentary")).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(ws.ops("momentary")[1]).toEqual({ op: "momentary", tag: "Bench.StartPB", action: "hold" });
    vi.advanceTimersByTime(600);
    expect(ws.ops("momentary")).toHaveLength(4);

    const released = m.release();
    const rel = ws.ops("momentary")[4];
    expect(rel).toMatchObject({ action: "release" });
    ws.receive({ op: "ack", id: rel.id, ok: true, error: null, kind: null, value: false });
    expect(await released).toMatchObject({ ok: true });
    expect(await m.release()).toMatchObject({ ok: true }); // a second release sends nothing
    vi.advanceTimersByTime(1000);
    expect(ws.ops("momentary")).toHaveLength(5);
  });

  it("is checked like a write: nothing is sent or held when not writable", async () => {
    vi.useFakeTimers();
    const { store, ws } = await connectedStore();
    ws.receive({ op: "info", d: { PB: { type: "BOOL", writable: false } } });
    expect(await store.momentary("PB").press()).toMatchObject({ ok: false, kind: "not_writable" });
    vi.advanceTimersByTime(1000);
    expect(ws.ops("momentary")).toEqual([]);
  });
});

describe("tree", () => {
  interface Tank {
    Level: TagRef<number, false>;
    Status: IntRef<false> & { Fault: TagRef<boolean, false> };
  }
  interface Tags {
    Tank: TagArray<Tank, 4>;
    Grid: TagArray<TagArray<TagRef<number, true>, 4>, 3>;
    SpeedSP: TagRef<number, true>;
    "Program:Bench": { Mode: TagRef<number, false> };
  }
  const meta = { dims: { Tank: [4], Grid: [3, 4] }, bits: { "Tank[].Status": { Fault: 2 } } };

  it("builds names from property access", () => {
    const t = tagTree<Tags>(null, meta);
    expect(String(t.SpeedSP)).toBe("SpeedSP");
    expect(String(t.Tank[2].Level)).toBe("Tank[2].Level");
    expect(`${t.Grid[1][2]}`).toBe("Grid[1,2]");
    expect(String(t.Tank[0].Status.bit(5))).toBe("Tank[0].Status.5");
    expect(String(t.Tank[3].Status.Fault)).toBe("Tank[3].Status.2");
    expect(String(t["Program:Bench"].Mode)).toBe("Program:Bench.Mode");
    expect(t.Tank.length).toBe(4);
    expect(t.Grid.length).toBe(3);
    expect(t.Grid[0].length).toBe(4);
    expect((t as any).then).toBeUndefined();
    expect(JSON.stringify({ tag: t.SpeedSP })).toBe('{"tag":"SpeedSP"}');
  });

  it("qualifies names with the PLC", () => {
    const t = tagTree<Tags>("PLC1", meta);
    expect(String(t.Tank[2].Level)).toBe("PLC1/Tank[2].Level");
    expect(String(t.Tank[1].Status.Fault)).toBe("PLC1/Tank[1].Status.2");
  });
});

describe("httpBase", () => {
  it("maps the ws URL to the HTTP base", () => {
    expect(httpBase("ws://host:8765/ws")).toBe("http://host:8765");
    expect(httpBase("wss://host/plc/ws")).toBe("https://host/plc");
    expect(httpBase("ws://host:8765")).toBe("http://host:8765");
  });
});

describe("derive read with get() only", () => {
  it("keeps no entry or listeners, and returns the same object while unchanged", async () => {
    const { store, ws } = await connectedStore();
    for (let i = 0; i < 100; i++) store.get(derive((get) => get<number>("A")));
    const internals = store as any;
    expect(internals.derived.size).toBe(0);
    expect(internals.listeners.get("A")?.size ?? 0).toBe(0);
    ws.receive({ op: "values", d: { A: [5, "good", 1] } });
    const d = derive((get) => (get<number>("A") ?? 0) + 1);
    const first = store.get(d);
    expect(first).toMatchObject({ value: 6, quality: "good" });
    expect(store.get(d)).toBe(first);
    store.listen(d, () => {}); // a later listener starts from that state
    expect(store.get(d)).toBe(first);
  });
});
