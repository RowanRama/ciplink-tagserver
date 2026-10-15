import { act, render } from "@testing-library/react";
import { expect } from "vitest";
import type { ReactNode } from "react";
import { TagServerProvider, TagStore } from "../src/index.js";

export class FakeWebSocket {
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

export const last = () => FakeWebSocket.all[FakeWebSocket.all.length - 1];
export const flush = () => act(async () => {}); // lets the microtask that batches subscriptions run

export async function connect(ws = last(), write = true) {
  act(() => ws.onopen!());
  expect(ws.sent[0]).toEqual({ op: "hello", v: 1, token: "T" });
  ws.receive({ op: "welcome", v: 1, plcs: { PLC1: "connected" }, write });
  await flush();
}

/** A connected store without React. */
export async function connectedStore() {
  const store = new TagStore("ws://test/ws", "T");
  store.connect();
  await connect();
  return { store, ws: last() };
}

export function wrap(ui: ReactNode) {
  return render(
    <TagServerProvider url="ws://test:8765/ws" token="T">
      {ui}
    </TagServerProvider>,
  );
}
