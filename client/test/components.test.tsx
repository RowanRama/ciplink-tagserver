import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMemo, useState } from "react";
import {
  MomentaryButton,
  NumericInput,
  TagBrowser,
  TagLamp,
  TagServerProvider,
  TagValue,
  derive,
  tagTree,
  useTag,
  useTagInfo,
  useTags,
  type IntRef,
  type TagRef,
} from "../src/index.js";
import { FakeWebSocket, connect, flush, last, wrap } from "./helpers.js";

beforeEach(() => {
  FakeWebSocket.all = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Every element's inline width and visibility-independent structure: must not change between states. */
const layout = (root: Element) =>
  [root, ...root.querySelectorAll("*")].map((el) => `${el.tagName}:${(el as HTMLElement).style.width}`).join(" ");

const tree = tagTree<{ W: IntRef<false>; SP: TagRef<number, true>; PB: TagRef<boolean, true> }>(null);

describe("hooks with refs", () => {
  it("useTag, useTags and useTagInfo take tree refs, bits and derived refs", async () => {
    function View() {
      const avg = useMemo(() => derive((get) => ((get<number>("A") ?? 0) + (get<number>("B") ?? 0)) / 2, "avg"), []);
      const bit = useTag(tree.W.bit(1));
      const d = useTag(avg);
      const all = useTags([tree.SP, avg]);
      const info = useTagInfo(tree.W.bit(1));
      return <span data-testid="v">{`${bit.value}|${d.value}|${d.quality}|${all.SP.value}|${all.avg.value}|${info?.type}`}</span>;
    }
    wrap(<View />);
    await connect();
    expect(last().ops("sub")[0].tags.sort()).toEqual(["A", "B", "SP", "W"]);
    last().receive({ op: "info", d: { W: { type: "DINT", writable: false, rate_ms: 250, group: null } } });
    last().receive({ op: "values", d: { W: [2, "good", 1], A: [1, "good", 1], B: [3, "good", 1], SP: [7, "good", 1] } });
    expect(screen.getByTestId("v").textContent).toBe("true|2|good|7|2|BOOL");
  });
});

describe("useTags with derived refs", () => {
  it("keys them by identity: a new ref with the same name subscribes its own inputs", async () => {
    let setI: (i: number) => void = () => {};
    function View() {
      const [i, set] = useState(0);
      setI = set;
      const d = useMemo(() => derive((get) => get<number>(`Tank[${i}].Level`), "lvl"), [i]);
      const all = useTags([d]);
      return <span data-testid="v">{`${all.lvl.value}|${all.lvl.quality}`}</span>;
    }
    wrap(<View />);
    await connect();
    act(() => setI(2));
    await flush();
    const active = new Map<number, string[]>();
    for (const m of last().sent) if (m.op === "sub") active.set(m.id, m.tags); else if (m.op === "unsub") active.delete(m.id);
    expect([...active.values()].flat()).toEqual(["Tank[2].Level"]);
    last().receive({ op: "values", d: { "Tank[2].Level": [5, "good", 1] } });
    expect(screen.getByTestId("v").textContent).toBe("5|good");
  });
});

describe("TagValue and TagLamp", () => {
  it("TagValue reserves the unit slot and fills it from info, so a late info moves nothing", async () => {
    wrap(<TagValue name="Speed" />);
    const value = document.querySelector(".ciplink-value")!;
    const before = layout(value);
    await connect();
    last().receive({ op: "values", d: { Speed: [12.5, "good", 1] } });
    last().receive({ op: "info", d: { Speed: { type: "REAL", writable: false, units: "rpm" } } });
    expect(value.textContent).toBe("12.5rpm");
    expect(layout(value)).toBe(before);
  });

  it("TagLamp shows the kept value on bad, with its mark and the error", async () => {
    wrap(<TagLamp name="Run" />);
    await connect();
    last().receive({ op: "values", d: { Run: [true, "good", 1] } });
    last().receive({ op: "values", d: { Run: [true, "bad", 2, { kind: "comm", message: "timeout" }] } });
    const lamp = document.querySelector(".ciplink-lamp") as HTMLElement;
    expect(lamp.dataset.quality).toBe("bad");
    expect(lamp.title).toMatch(/: on \(bad\)/);
    expect(screen.getByRole("img", { name: "Run: on (bad)" })).toBe(lamp); // not by colour alone
    expect(lamp.querySelector(".ciplink-mark")!.getAttribute("title")).toMatch(/timeout/);
  });
});

describe("NumericInput", () => {
  it("confirms inline in reserved space, writes and shows the result, without moving anything", async () => {
    window.confirm = vi.fn();
    wrap(<NumericInput name={tree.SP} decimals={1} />);
    await connect();
    const ws = last();
    ws.receive({ op: "info", d: { SP: { type: "REAL", writable: true, min: 0, max: 1800 } } });
    ws.receive({ op: "values", d: { SP: [100, "good", 1] } });
    const root = document.querySelector(".ciplink-input")!;
    const input = screen.getByRole("textbox") as HTMLInputElement;
    const confirm = root.querySelector(".ciplink-confirm") as HTMLElement;
    expect(input.value).toBe("100.0");
    expect(confirm.style.visibility).toBe("hidden");
    const before = layout(root);

    // out of range: rejected before asking
    fireEvent.change(input, { target: { value: "2000" } });
    fireEvent.click(screen.getByText("Set"));
    expect(root.querySelector(".ciplink-result")!.textContent).toBe("out of range");
    expect(confirm.style.visibility).toBe("hidden");

    fireEvent.change(input, { target: { value: "1200" } });
    fireEvent.click(screen.getByText("Set"));
    expect(confirm.style.visibility).toBe("visible");
    expect(ws.ops("write")).toEqual([]);
    expect(layout(root)).toBe(before);

    fireEvent.click(screen.getByText("OK"));
    const sent = ws.ops("write")[0];
    expect(sent).toMatchObject({ tag: "SP", value: 1200 });
    expect(root.querySelector(".ciplink-result")!.textContent).toBe("…");
    expect(layout(root)).toBe(before);
    ws.receive({ op: "ack", id: sent.id, ok: true, error: null, kind: null, value: 1200 });
    await flush();
    expect(root.querySelector(".ciplink-result")!.textContent).toBe("✓");
    expect(confirm.style.visibility).toBe("hidden");
    expect(layout(root)).toBe(before);
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("keeps the focus in the input after a confirmed write", async () => {
    wrap(<NumericInput name="SP" />);
    await connect();
    last().receive({ op: "values", d: { SP: [1, "good", 1] } });
    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const ok = screen.getByText("OK");
    ok.focus();
    fireEvent.click(ok);
    expect(document.activeElement).toBe(input);
    expect(input.disabled).toBe(false);
    const sent = last().ops("write")[0];
    last().receive({ op: "ack", id: sent.id, ok: true, error: null, kind: null, value: 2 });
    await flush();
    expect(document.activeElement).toBe(input);
    const status = screen.getByRole("status");
    expect(status.textContent).toBe("✓");
    expect(input.getAttribute("aria-describedby")).toBe(status.id);
  });

  it("keeps the focus in the input after Cancel", async () => {
    wrap(<NumericInput name="SP" />);
    await connect();
    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const cancel = screen.getByText("Cancel");
    cancel.focus();
    fireEvent.click(cancel);
    expect(document.activeElement).toBe(input);
    expect(last().ops("write")).toEqual([]);
  });

  it("is disabled when info says not writable, and Escape cancels", async () => {
    wrap(<NumericInput name="RO" confirm={false} />);
    await connect();
    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("");
    last().receive({ op: "info", d: { RO: { type: "REAL", writable: false } } });
    expect(input.disabled).toBe(true);
  });
});

describe("MomentaryButton", () => {
  async function setup() {
    vi.useFakeTimers();
    const view = wrap(<MomentaryButton name={tree.PB}>Start</MomentaryButton>);
    await connect();
    const button = screen.getByText("Start");
    const actions = () => last().ops("momentary").map((m) => m.action);
    return { view, button, actions };
  }

  it("presses on pointerdown, holds every 300 ms, releases once on pointerup and lost capture", async () => {
    const { button, actions } = await setup();
    const before = layout(button.parentElement!);
    fireEvent.pointerDown(button, { pointerId: 1 });
    expect(actions()).toEqual(["press"]);
    expect(button.dataset.pressed).toBe("true");
    act(() => vi.advanceTimersByTime(650));
    expect(actions()).toEqual(["press", "hold", "hold"]);
    expect(layout(button.parentElement!)).toBe(before);
    fireEvent.pointerUp(button, { pointerId: 1 });
    fireEvent(button, new Event("lostpointercapture", { bubbles: true }));
    act(() => vi.advanceTimersByTime(1000));
    expect(actions()).toEqual(["press", "hold", "hold", "release"]);
    expect(button.dataset.pressed).toBe("false");
  });

  it.each([
    ["pointercancel", (b: HTMLElement) => fireEvent.pointerCancel(b)],
    ["lost capture", (b: HTMLElement) => fireEvent(b, new Event("lostpointercapture", { bubbles: true }))],
    ["window blur", () => act(() => void window.dispatchEvent(new Event("blur")))],
  ])("releases on %s", async (_, end) => {
    const { button, actions } = await setup();
    fireEvent.pointerDown(button, { pointerId: 1 });
    end(button);
    expect(actions()).toEqual(["press", "release"]);
  });

  it("subscribes its tag, so info disables it when not writable", async () => {
    const { button } = await setup();
    expect(last().ops("sub").flatMap((m) => m.tags)).toEqual(["PB"]);
    last().receive({ op: "info", d: { PB: { type: "BOOL", writable: false } } });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("ignores other mouse buttons, and Space or Enter press and release it", async () => {
    const { button, actions } = await setup();
    fireEvent.pointerDown(button, { pointerId: 1, button: 2 });
    expect(actions()).toEqual([]);
    fireEvent.keyDown(button, { key: " " });
    fireEvent.keyDown(button, { key: " ", repeat: true });
    expect(actions()).toEqual(["press"]);
    fireEvent.keyUp(button, { key: " " });
    fireEvent.keyDown(button, { key: "Enter" });
    fireEvent.blur(button);
    expect(actions()).toEqual(["press", "release", "press", "release"]);
  });

  it("releases on unmount", async () => {
    const { view, button, actions } = await setup();
    fireEvent.pointerDown(button, { pointerId: 1 });
    view.rerender(<TagServerProvider url="ws://test:8765/ws" token="T">{null}</TagServerProvider>);
    expect(actions()).toEqual(["press", "release"]);
  });

  it("shows a failed press in its fixed mark slot", async () => {
    const { button } = await setup();
    const before = layout(button.parentElement!);
    fireEvent.pointerDown(button, { pointerId: 1 });
    const press = last().ops("momentary")[0];
    last().receive({ op: "ack", id: press.id, ok: false, error: "not allowed", kind: "not_allowed", value: null });
    await act(async () => {});
    const mark = button.parentElement!.querySelector(".ciplink-mark")!;
    expect(mark.querySelector("[aria-hidden]")!.textContent).toBe("!");
    expect(mark.getAttribute("role")).toBe("status");
    expect(mark.textContent).toContain("not allowed"); // read out, takes no space
    expect(button.getAttribute("aria-describedby")).toBe(mark.id);
    expect(mark.getAttribute("title")).toBe("not allowed");
    expect(layout(button.parentElement!)).toBe(before);
  });
});

describe("TagBrowser", () => {
  const body = {
    plc: "PLC1",
    version: 3,
    tags: [
      { name: "Bench", type: "BenchData" },
      { name: "Trend", type: "REAL", dims: [120] },
      { name: "Speed", type: "REAL" },
    ],
    types: { BenchData: { members: { Speed: { type: "REAL" }, Setpoints: { type: "REAL", dims: [4] } } } },
  };

  async function setup(props = {}) {
    const fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }));
    vi.stubGlobal("fetch", fetch);
    wrap(<TagBrowser {...props} />);
    await connect();
    await flush();
    return fetch;
  }
  const paths = () => [...document.querySelectorAll("[data-path]")].map((el) => el.getAttribute("data-path")!);
  const row = (path: string) => [...document.querySelectorAll("[data-path]")].find((el) => el.getAttribute("data-path") === path) as HTMLElement;
  const elements = () => paths().filter((p) => p.startsWith("Trend["));
  const subscribed = () => last().ops("sub").flatMap((m) => m.tags ?? []);
  /** the tags subscribed now, replaying sub and unsub */
  const active = () => {
    const ids = new Map<number, string[]>();
    for (const m of last().sent) if (m.op === "sub" && m.tags) ids.set(m.id, m.tags); else if (m.op === "unsub") ids.delete(m.id);
    return [...ids.values()].flat().sort();
  };

  it("fetches /tags from the ws URL's HTTP base with the token, and subscribes only shown leaves", async () => {
    const fetch = await setup({ plc: "PLC1" });
    expect(fetch).toHaveBeenCalledWith("http://test:8765/tags?plc=PLC1", { headers: { Authorization: "Bearer T" } });
    expect(screen.getByText("3 tags, table version 3")).toBeTruthy();
    await flush();
    expect(subscribed()).toEqual(["PLC1/Speed"]);

    fireEvent.click(row("PLC1/Bench").querySelector("button")!);
    await flush();
    expect(subscribed()).toEqual(["PLC1/Speed", "PLC1/Bench.Speed"]);
    expect(row("PLC1/Bench.Setpoints")).toBeTruthy();
    last().receive({ op: "info", d: { "PLC1/Bench.Speed": { type: "REAL", writable: true } } });
    expect(row("PLC1/Bench.Speed").textContent).toContain("RW");
  });

  it("pages large arrays 50 at a time", async () => {
    await setup();
    fireEvent.click(row("Trend").querySelector("button")!);
    await flush();
    expect(elements()).toHaveLength(50);
    expect(subscribed()).toHaveLength(51);
    expect(screen.getByText("0–49 of 120")).toBeTruthy();
    const next = screen.getByLabelText("next");
    fireEvent.click(next);
    await flush();
    expect(row("Trend[50]")).toBeTruthy();
    expect(row("Trend[0]")).toBeUndefined();
    fireEvent.click(next);
    await flush();
    expect(elements()).toHaveLength(20);
    expect(active()).toEqual(["Speed", ...Array.from({ length: 20 }, (_, i) => `Trend[${100 + i}]`)].sort());
  });

  it("expands an array of a UDT into its elements only, which expand into the members", async () => {
    body.tags.push({ name: "Tank", type: "TankData", dims: [2] });
    (body.types as any).TankData = { members: { Level: { type: "REAL" } } };
    try {
      await setup();
      const expander = row("Tank").querySelector("button")!;
      expect(expander.getAttribute("aria-label")).toBe("Tank");
      fireEvent.click(expander);
      await flush();
      expect(paths().filter((p) => p.startsWith("Tank"))).toEqual(["Tank", "Tank[0]", "Tank[1]"]);
      fireEvent.click(row("Tank[1]").querySelector("button")!);
      await flush();
      expect(paths().filter((p) => p.startsWith("Tank"))).toEqual(["Tank", "Tank[0]", "Tank[1]", "Tank[1].Level"]);
      expect(active()).toEqual(["Speed", "Tank[1].Level"]);
    } finally {
      body.tags.pop();
      delete (body.types as any).TankData;
    }
  });

  it("keeps its buttons at a fixed height, so a copy mark never moves the rows", async () => {
    await setup();
    const copy = row("Speed").querySelector(".ciplink-copy") as HTMLElement;
    expect(copy.style.height).toBe("1.5em");
    expect(copy.style.lineHeight).toBe("1");
  });

  it("goes back to the last page when a reload shrinks the list", async () => {
    const many = { ...body, tags: Array.from({ length: 120 }, (_, i) => ({ name: `T${i}`, type: "DINT" })) };
    const few = { ...body, tags: many.tags.slice(0, 30) };
    let current = many;
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => current })));
    wrap(<TagBrowser />);
    await connect();
    await flush();
    fireEvent.click(screen.getByLabelText("next"));
    fireEvent.click(screen.getByLabelText("next"));
    await flush();
    expect(paths()).toHaveLength(20);
    current = few;
    last().receive({ op: "meta", plc: "PLC1" });
    await flush();
    await flush();
    expect(paths()).toHaveLength(30);
  });

  it("copies the path, falling back when the Clipboard API is missing", async () => {
    await setup();
    vi.stubGlobal("navigator", { clipboard: undefined });
    const exec = vi.fn(() => true);
    (document as any).execCommand = exec;
    const copy = row("Speed").querySelector(".ciplink-copy") as HTMLElement;
    fireEvent.click(copy);
    await flush();
    expect(exec).toHaveBeenCalledWith("copy");
    expect(copy.textContent).toBe("✓");
  });
});
