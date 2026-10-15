import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import {
  TagLamp,
  TagLink,
  TagServerProvider,
  TagValue,
  registerPage,
  useTag,
  useTagServer,
  useTags,
  useWriteTag,
  type WriteResult,
} from "../src/index.js";

class FakeWebSocket {
  static all: FakeWebSocket[] = [];
  sent: any[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {}
  // test side
  receive(msg: object) {
    act(() => this.onmessage!({ data: JSON.stringify(msg) }));
  }
  drop(code = 1006) {
    act(() => this.onclose!({ code }));
  }
  ops(op: string) {
    return this.sent.filter((m) => m.op === op);
  }
}

const last = () => FakeWebSocket.all[FakeWebSocket.all.length - 1];
const flush = () => act(async () => {}); // lets the microtask that batches subscriptions run

async function connect(ws = last(), write = true) {
  act(() => ws.onopen!());
  expect(ws.sent[0]).toEqual({ op: "hello", v: 1, token: "T" });
  ws.receive({ op: "welcome", v: 1, plcs: { PLC1: "connected" }, write });
  await flush();
}

function wrap(ui: React.ReactNode, onMeta?: (plc: string) => void) {
  return render(
    <TagServerProvider url="ws://test/ws" token="T" onMeta={onMeta}>
      {ui}
    </TagServerProvider>,
  );
}

const renders: Record<string, number> = {};
function Show({ name, tier }: { name: string; tier?: number }) {
  const { value, quality } = useTag(name, { tier });
  renders[name] = (renders[name] ?? 0) + 1;
  return <span data-testid={name}>{`${value}|${quality}`}</span>;
}
const text = (name: string) => screen.getByTestId(name).textContent;

beforeEach(() => {
  FakeWebSocket.all = [];
  for (const k of Object.keys(renders)) delete renders[k];
  vi.stubGlobal("WebSocket", FakeWebSocket);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("subscriptions", () => {
  it("batches subs made in one tick and ref counts them across components", async () => {
    function App({ shown }: { shown: string[] }) {
      return (
        <>
          {shown.map((n, i) => (
            <Show key={i} name={n} />
          ))}
        </>
      );
    }
    const { rerender } = wrap(<App shown={["A", "B", "A"]} />);
    await connect();
    const ws = last();
    expect(ws.ops("sub")).toEqual([{ op: "sub", id: 1, tags: ["A", "B"], tier: 1 }]);

    // one of the two A's goes away: nothing is sent
    rerender(
      <TagServerProvider url="ws://test/ws" token="T">
        <App shown={["A", "B"]} />
      </TagServerProvider>,
    );
    await flush();
    expect(ws.ops("sub")).toHaveLength(1);
    expect(ws.ops("unsub")).toHaveLength(0);

    // B goes: the group is re-sent with A only
    rerender(
      <TagServerProvider url="ws://test/ws" token="T">
        <App shown={["A"]} />
      </TagServerProvider>,
    );
    await flush();
    expect(ws.ops("sub")[1]).toEqual({ op: "sub", id: 1, tags: ["A"], tier: 1 });

    // the last A goes: unsub
    rerender(
      <TagServerProvider url="ws://test/ws" token="T">
        <App shown={[]} />
      </TagServerProvider>,
    );
    await flush();
    expect(ws.ops("unsub")).toEqual([{ op: "unsub", id: 1 }]);
  });

  it("shows pending, then the snapshot, then changes", async () => {
    wrap(<Show name="PLC1/Line1.Speed" />);
    expect(text("PLC1/Line1.Speed")).toBe("null|pending");
    await connect();
    const ws = last();
    ws.receive({ op: "ack", id: 1, ok: true, error: null });
    ws.receive({ op: "values", d: { "PLC1/Line1.Speed": [12.5, "good", 1000] } });
    expect(text("PLC1/Line1.Speed")).toBe("12.5|good");
    ws.receive({ op: "values", d: { "PLC1/Line1.Speed": [13, "good", 1250] } });
    expect(text("PLC1/Line1.Speed")).toBe("13|good");
    ws.receive({ op: "values", d: { "PLC1/Line1.Speed": [null, "bad", 1500] } });
    expect(text("PLC1/Line1.Speed")).toBe("null|bad");
  });

  it("re-renders only the components of the tag that changed", async () => {
    wrap(
      <>
        <Show name="A" />
        <Show name="B" />
      </>,
    );
    await connect();
    const ws = last();
    ws.receive({ op: "values", d: { A: [1, "good", 1], B: [1, "good", 1] } });
    const a = renders.A;
    ws.receive({ op: "values", d: { B: [2, "good", 2] } });
    ws.receive({ op: "values", d: { B: [3, "good", 3] } });
    expect(text("B")).toBe("3|good");
    expect(renders.A).toBe(a);
  });

  it("useTags returns a record", async () => {
    function Both() {
      const tags = useTags(["A", "B"]);
      return <span data-testid="both">{`${tags.A.value},${tags.B.quality}`}</span>;
    }
    wrap(<Both />);
    await connect();
    expect(last().ops("sub")[0].tags).toEqual(["A", "B"]);
    last().receive({ op: "values", d: { A: [7, "good", 1] } });
    expect(text("both")).toBe("7,pending");
  });

  it("TagLink subscribes the target page at tier 2, plus any registered tags", async () => {
    registerPage("/station1", ["Program:Station1.DoorOpen"]);
    wrap(<TagLink to="/station1">Station 1</TagLink>);
    await connect();
    expect(last().ops("sub")).toEqual([
      { op: "sub", id: 1, page: "/station1", tier: 2 },
      { op: "sub", id: 2, tags: ["Program:Station1.DoorOpen"], tier: 2 },
    ]);
    expect(screen.getByText("Station 1").getAttribute("href")).toBe("/station1");
  });

  it("TagLink page subscriptions are shared and dropped with the last link", async () => {
    function Links({ n }: { n: number }) {
      return (
        <>
          {Array.from({ length: n }, (_, i) => (
            <TagLink key={i} to="/line2">
              Line 2
            </TagLink>
          ))}
        </>
      );
    }
    const ui = (n: number) => (
      <TagServerProvider url="ws://test/ws" token="T">
        <Links n={n} />
      </TagServerProvider>
    );
    const { rerender } = render(ui(1));
    await connect();
    rerender(ui(2));
    await flush();
    expect(last().ops("sub")).toEqual([{ op: "sub", id: 1, page: "/line2", tier: 2 }]);
    rerender(ui(1));
    await flush();
    expect(last().ops("unsub")).toEqual([]);
    rerender(ui(0));
    await flush();
    expect(last().ops("unsub")).toEqual([{ op: "unsub", id: 1 }]);
  });

  it("tier-1 subscriptions carry the provider's page", async () => {
    const ui = (page: string, names: string[]) => (
      <TagServerProvider url="ws://test/ws" token="T" page={page}>
        {names.map((name) => (
          <Show key={name} name={name} />
        ))}
        <Show name="Slow" tier={2} />
      </TagServerProvider>
    );
    const { rerender } = render(ui("/line1", ["A", "B"]));
    await connect();
    expect(last().ops("sub")).toEqual([
      { op: "sub", id: 1, tags: ["A", "B"], tier: 1, page: "/line1" },
      { op: "sub", id: 2, tags: ["Slow"], tier: 2 },
    ]);
    rerender(ui("/line2", ["C"]));
    await flush();
    expect(last().ops("sub").slice(2)).toEqual([{ op: "sub", id: 3, tags: ["C"], tier: 1, page: "/line2" }]);
  });
});

describe("connection", () => {
  it("marks values stale while disconnected and resubscribes on reconnect", async () => {
    vi.useFakeTimers();
    function State() {
      const { state, plcs } = useTagServer();
      return (
        <>
          <span data-testid="state">{state}</span>
          <span data-testid="plc">{plcs.PLC1}</span>
        </>
      );
    }
    wrap(
      <>
        <Show name="A" />
        <State />
      </>,
    );
    await connect();
    const first = last();
    first.receive({ op: "values", d: { A: [5, "good", 1] } });
    first.drop();
    expect(text("A")).toBe("5|stale");
    expect(text("state")).toBe("disconnected");
    expect(text("plc")).toBe("unknown"); // not "connected" while the server is unreachable

    act(() => vi.advanceTimersByTime(1000));
    expect(FakeWebSocket.all).toHaveLength(2);
    await connect();
    expect(last().ops("sub")).toEqual([{ op: "sub", id: 1, tags: ["A"], tier: 1 }]);
    expect(text("state")).toBe("connected");
    expect(text("plc")).toBe("connected");

    // backoff grows 1, 2, 5, 10 s while it keeps failing
    last().drop();
    act(() => vi.advanceTimersByTime(999));
    expect(FakeWebSocket.all).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1));
    expect(FakeWebSocket.all).toHaveLength(3);
    last().drop();
    act(() => vi.advanceTimersByTime(1999));
    expect(FakeWebSocket.all).toHaveLength(3);
    act(() => vi.advanceTimersByTime(1));
    expect(FakeWebSocket.all).toHaveLength(4);
  });

  it("surfaces a 4401 close as unauthorized and stops reconnecting", async () => {
    vi.useFakeTimers();
    function State() {
      return <span data-testid="state">{useTagServer().state}</span>;
    }
    wrap(<State />);
    act(() => last().onopen!());
    last().drop(4401);
    expect(text("state")).toBe("unauthorized");
    act(() => vi.advanceTimersByTime(30000));
    expect(FakeWebSocket.all).toHaveLength(1);
  });

  it("exposes PLC states, canWrite and meta", async () => {
    const onMeta = vi.fn();
    function Info() {
      const { plcs, canWrite } = useTagServer();
      return <span data-testid="info">{`${plcs.PLC1}|${canWrite}`}</span>;
    }
    wrap(<Info />, onMeta);
    await connect(last(), false);
    expect(text("info")).toBe("connected|false");
    last().receive({ op: "status", plcs: { PLC1: "disconnected" } });
    expect(text("info")).toBe("disconnected|false");
    last().receive({ op: "meta", plc: "PLC1" });
    expect(onMeta).toHaveBeenCalledWith("PLC1");
  });
});

describe("writes", () => {
  let write: (value: unknown) => Promise<WriteResult>;
  function Writer() {
    write = useWriteTag("Line1.SpeedSP");
    return null;
  }

  it("resolves with the ack", async () => {
    wrap(<Writer />);
    expect(await write(1)).toEqual({ ok: false, error: "not connected", kind: "comm", value: null });
    await connect();
    const ws = last();
    const pending = write(42.5);
    const sent = ws.ops("write")[0];
    expect(sent).toMatchObject({ op: "write", tag: "Line1.SpeedSP", value: 42.5 });
    ws.receive({ op: "ack", id: sent.id, ok: true, error: null });
    expect(await pending).toEqual({ ok: true, error: null, kind: null, value: null });

    const denied = write(1);
    ws.receive({ op: "ack", id: ws.ops("write")[1].id, ok: false, error: "not allowed" });
    expect(await denied).toMatchObject({ ok: false, error: "not allowed" }); // a 0.2 ack has no kind

    const lost = write(2);
    ws.drop();
    expect(await lost).toEqual({ ok: false, error: "disconnected", kind: "comm", value: null });
  });
});

describe("displays", () => {
  it("keep a fixed width and show quality as a marker", async () => {
    wrap(
      <>
        <TagValue name="A" decimals={1} unit="m/s" width={6} />
        <TagLamp name="B" />
      </>,
    );
    const value = document.querySelector(".ciplink-value") as HTMLElement;
    const lamp = document.querySelector(".ciplink-lamp") as HTMLElement;
    const widths = () => [...value.children].map((c) => (c as HTMLElement).style.width);
    expect(value.dataset.quality).toBe("pending");
    const before = widths();
    expect(before).toEqual(["6ch", "4ch", "1.5ch"]);

    await connect();
    last().receive({ op: "values", d: { A: [3.14159, "good", 1], B: [true, "good", 1] } });
    expect(value.textContent).toBe("3.1m/s");
    expect(value.dataset.quality).toBe("good");
    last().receive({ op: "values", d: { A: [12345.67, "good", 2] } });
    expect(value.textContent).toBe("12345.7m/s");
    expect(widths()).toEqual(before);

    last().drop();
    expect(value.dataset.quality).toBe("stale");
    expect(lamp.dataset.quality).toBe("stale");
    expect(value.querySelector(".ciplink-mark")!.getAttribute("title")).toMatch(/stale/);
    expect(widths()).toEqual(before);
  });
});
