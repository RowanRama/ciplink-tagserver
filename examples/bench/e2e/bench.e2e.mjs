// End-to-end run of the Test Bench: starts the mock tag server and the Vite dev server, drives the app in the
// installed Chrome (no browser download) and checks values, poll groups, a write, momentary buttons, the
// watchdog and that no control moves. Run with `npm run e2e`; set TAGSERVER to the command that runs
// ciplink-tagserver (default "uv run ciplink-tagserver", from the repository root) and CHROME to a Chrome binary.
import { execSync, spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const bench = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = resolve(bench, "../..");
const SERVER = "http://127.0.0.1:8765";
const APP = "http://127.0.0.1:5173";
const TOKEN = "bench-hmi";
const TAGSERVER = process.env.TAGSERVER ?? "uv run ciplink-tagserver";
const CHROME = process.env.CHROME ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const AUDIT = resolve(bench, "audit.log");
const win = process.platform === "win32";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` - ${detail}` : ""}`);
  if (!ok) failures.push(name);
}
async function waitFor(fn, timeout = 5000, step = 100) {
  const end = Date.now() + timeout;
  for (;;) {
    let v;
    try {
      v = await fn();
    } catch {}
    if (v || Date.now() > end) return v;
    await sleep(step);
  }
}
const get = (path) => fetch(SERVER + path).then((r) => r.json());
const fast = async () => (await get("/stats")).plcs.PLC1.groups.fast;
const auditLines = () => (existsSync(AUDIT) ? readFileSync(AUDIT, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []);

function start(cmd, cwd) {
  const p = spawn(cmd, { cwd, shell: true, detached: !win, stdio: ["ignore", "pipe", "pipe"] });
  p.log = "";
  p.stdout.on("data", (d) => (p.log += d));
  p.stderr.on("data", (d) => (p.log += d));
  return p;
}
function stop(p) {
  if (!p || p.exitCode !== null) return;
  try {
    if (win) execSync(`taskkill /pid ${p.pid} /T /F`, { stdio: "ignore" });
    else process.kill(-p.pid);
  } catch {}
}

/** A raw protocol client next to the browser: watches values and sends what a buggy client would. */
class Observer {
  values = {};
  acks = {};
  constructor() {
    this.ws = new WebSocket(SERVER.replace("http", "ws") + "/ws");
    this.ready = new Promise((ok) => this.ws.addEventListener("open", ok));
    this.ws.addEventListener("message", (e) => {
      const m = JSON.parse(e.data);
      if (m.op === "values") for (const [k, v] of Object.entries(m.d)) this.values[k] = v;
      if (m.op === "ack") this.acks[m.id] = m;
    });
  }
  async open(tags) {
    await this.ready;
    this.send({ op: "hello", v: 1, token: TOKEN });
    this.send({ op: "sub", id: 1, tags });
  }
  send(msg) {
    this.ws.send(JSON.stringify(msg));
  }
  value(tag) {
    return this.values[tag]?.[0];
  }
}

/** Counts of data-quality over every value and lamp on the page. */
const qualities = (page) =>
  page.evaluate(() => {
    const n = {};
    for (const el of document.querySelectorAll("main [data-quality]")) n[el.dataset.quality] = (n[el.dataset.quality] ?? 0) + 1;
    return n;
  });

/** Records the bounding box of every button, input and link and notes each one that moves or goes away. */
const recordLayout = (page) =>
  page.evaluate(() => {
    const els = [...document.querySelectorAll("button, input, a")];
    const box = (el) => {
      const r = el.getBoundingClientRect();
      return [r.x, r.y, r.width, r.height].map((v) => Math.round(v * 2) / 2).join(",");
    };
    const base = els.map(box);
    const w = window;
    w.__moves = [];
    w.__samples = 0;
    w.__layout = setInterval(() => {
      w.__samples++;
      els.forEach((el, i) => {
        const now = el.isConnected ? box(el) : "gone";
        if (now !== base[i] && w.__moves.length < 20)
          w.__moves.push(`${el.tagName.toLowerCase()} "${el.textContent || el.getAttribute("aria-label")}" ${base[i]} -> ${now}`);
      });
    }, 50);
    return els.length;
  });

let server, vite, browser;
try {
  if (await fetch(SERVER + "/health").then(() => true, () => false)) throw new Error(`${SERVER} is already in use: stop that server first`);
  server = start(`${TAGSERVER} run --config examples/bench/tagserver.toml --mock`, root);
  vite = start(`node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort`, bench);
  const up = await waitFor(async () => (await get("/health")).plcs.PLC1.state === "connected", 60000, 250);
  if (!up) throw new Error(`tag server did not start:\n${server.log}`);
  if (!(await waitFor(() => fetch(APP).then((r) => r.ok), 30000, 250))) throw new Error(`vite did not start:\n${vite.log}`);
  const auditStart = auditLines().length;

  browser = await chromium.launch(existsSync(CHROME) ? { executablePath: CHROME } : { channel: "chrome" });
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: APP });
  const page = await context.newPage();
  await page.goto(`${APP}/#/`);

  // values arrive with good quality; the one unknown tag is missing
  const q = await waitFor(async () => {
    const n = await qualities(page);
    return !n.pending && !n.stale && n.good > 30 ? n : null;
  }, 10000);
  check("values arrive good", !!q && q.missing === 1, JSON.stringify(q ?? (await qualities(page))));
  const missing = await page.locator('[title^="Bench.NoSuchTag:"]').getAttribute("data-quality");
  check("Bench.NoSuchTag is missing", missing === "missing");
  check("bit alias and bit from word show", (await page.locator('[title^="Bench.StatusWord.0:"], [title^="Bench.StatusWord.3:"]').count()) === 2);

  // the fast group runs only while Motion is open
  const before = await fast();
  check("fast group idle on Overview", before.tags === 0, JSON.stringify(before));
  await page.getByRole("link", { name: "Motion" }).click();
  const onMotion = await waitFor(async () => {
    const g = await fast();
    return g.tags === 2 && g.last_period_ms !== null && g.last_period_ms < 120 ? g : null;
  }, 5000);
  check("fast group at 50 ms on Motion", !!onMotion, JSON.stringify(onMotion ?? (await fast())));
  await page.getByRole("link", { name: "Overview" }).click();
  const back = await waitFor(async () => ((await fast()).tags === 0 ? await fast() : null), 3000);
  check("fast group stops after leaving Motion", !!back, JSON.stringify(back ?? (await fast())));
  await waitFor(async () => !(await qualities(page)).pending, 5000);

  // from here on, no control may move: live updates, a write, momentary presses, a disconnect
  const controls = await recordLayout(page);
  await sleep(10000);

  // a NumericInput write, read back
  const input = page.getByLabel("Bench.SpeedSP");
  const field = page.locator(".ciplink-input").filter({ has: input });
  await input.fill("5000");
  await field.locator(".ciplink-set").click();
  check("out of range rejected in the client", (await field.locator(".ciplink-result").textContent()) === "out of range");
  await input.fill("1200");
  await field.locator(".ciplink-set").click();
  await field.getByRole("button", { name: "OK" }).click();
  const written = await waitFor(async () => (await field.locator(".ciplink-result").textContent()) === "✓", 3000);
  check("write acked", !!written, await field.locator(".ciplink-result").textContent());
  const shown = await waitFor(async () => ((await input.inputValue()) === "1200.0" ? "1200.0" : null), 3000);
  check("write read back into the input", !!shown, await input.inputValue());

  // momentary: press, hold past the 1 s watchdog, release
  const obs = new Observer();
  await obs.open(["Bench.StartPB", "Bench.StopPB", "Bench.Running", "Bench.Speed"]);
  await waitFor(() => obs.value("Bench.StartPB") === false, 3000);
  const press = async (p, name) => {
    await p.getByRole("button", { name, exact: true }).hover();
    await p.mouse.down();
  };
  await press(page, "Stop");
  await waitFor(() => obs.value("Bench.Running") === false, 2000);
  await page.mouse.up();
  await press(page, "Start");
  check("press writes true", !!(await waitFor(() => obs.value("Bench.StartPB") === true, 2000)));
  await sleep(1800);
  check("holds keep it pressed past the watchdog", obs.value("Bench.StartPB") === true && obs.value("Bench.Running") === true);
  await page.mouse.up();
  check("release writes false", !!(await waitFor(() => obs.value("Bench.StartPB") === false, 2000)));
  const atSpeed = await waitFor(async () => (await page.locator('[title^="atSpeed:"]').getAttribute("title")) === "atSpeed: on (good)", 5000);
  check("speed follows the setpoint (derived at-speed lamp on)", !!atSpeed, `Speed ${obs.value("Bench.Speed")}`);
  await page.mouse.move(5, 5);
  await page.screenshot({ path: resolve(bench, "screenshot.png") });

  // closing the page while a button is held releases it on the server
  const page2 = await context.newPage();
  await page2.goto(`${APP}/#/`);
  await waitFor(() => page2.getByRole("button", { name: "Start", exact: true }).isEnabled(), 5000);
  await waitFor(async () => (await qualities(page2)).good > 30, 5000);
  await press(page2, "Start");
  await waitFor(() => obs.value("Bench.StartPB") === true, 2000);
  let t0 = Date.now();
  await page2.close();
  const closed = await waitFor(() => obs.value("Bench.StartPB") === false, 3000, 20);
  check("closing the page releases a held button", !!closed, `${Date.now() - t0} ms`);

  // the watchdog: a press with no holds is released by the server after momentary_timeout_ms
  const timeouts = (await get("/stats")).momentary_timeouts;
  obs.send({ op: "momentary", id: 9, tag: "Bench.StartPB", action: "press" });
  await waitFor(() => obs.value("Bench.StartPB") === true, 2000, 20);
  t0 = Date.now();
  const released = await waitFor(() => obs.value("Bench.StartPB") === false, 3000, 20);
  const stats = await get("/stats");
  check("watchdog releases without holds", !!released && stats.momentary_timeouts === timeouts + 1, `${Date.now() - t0} ms, momentary_timeouts ${stats.momentary_timeouts}`);
  const audit = auditLines().slice(auditStart);
  const kinds = audit.filter((l) => l.momentary).map((l) => l.momentary);
  check("audit has the write and every momentary kind", audit.some((l) => l.tag === "PLC1/Bench.SpeedSP" && l.value === 1200 && l.ok) && ["press", "release", "timeout"].every((k) => kinds.includes(k)), kinds.join(","));
  check("page close audited as disconnect or release", kinds.includes("disconnect") || kinds.filter((k) => k === "release").length >= 3, kinds.join(","));

  // the tag browser: expand Bench, its leaf rows subscribe and show RW/RO from info
  const page3 = await context.newPage();
  await page3.goto(`${APP}/#/browser`);
  await page3.locator('.ciplink-row[data-path="Bench"] button[aria-expanded]').click();
  const sp = page3.locator('.ciplink-row[data-path="Bench.SpeedSP"]');
  const row = await waitFor(async () => {
    const [quality, text] = [await sp.locator("[data-quality]").getAttribute("data-quality"), await sp.textContent()];
    return quality === "good" && text.includes("1200") && text.includes("RW") ? text : null;
  }, 5000);
  const ro = await waitFor(async () => ((await page3.locator('.ciplink-row[data-path="Bench.Count"]').textContent()).includes("RO") ? true : null), 5000);
  check("tag browser shows values and RW/RO", !!row && !!ro, (row ?? (await sp.textContent())).replace(/\s+/g, " "));
  // the copy mark and the current-page link change: no browser row or nav link moves
  const navBoxes = (p) =>
    p.evaluate(() => [...document.querySelectorAll("nav a")].map((a) => { const r = a.getBoundingClientRect(); return `${r.x},${r.width}`; }).join(" "));
  check("nav links stay put when the current page changes", (await navBoxes(page3)) === (await navBoxes(page)), `${await navBoxes(page3)} vs ${await navBoxes(page)}`);
  await recordLayout(page3);
  const copy = page3.locator('.ciplink-row[data-path="Bench.Count"] .ciplink-copy');
  await copy.click();
  await waitFor(async () => (await copy.textContent()) !== "Copy", 2000);
  await sleep(300);
  const browserMoves = await page3.evaluate(() => window.__moves);
  check("a copy mark moves no browser row", browserMoves.length === 0, browserMoves.join("; "));
  await page3.close();

  // disconnect: stop the server; values go stale, nothing moves
  stop(server);
  const stale = await waitFor(async () => {
    const n = await qualities(page);
    return (await page.locator("footer").getAttribute("data-state")) !== "connected" && !n.good && n.stale > 30 ? n : null;
  }, 8000);
  check("values stale after the server stops", !!stale, JSON.stringify(stale ?? (await qualities(page))));
  await sleep(2000);
  const { moves, samples } = await page.evaluate(() => ({ moves: window.__moves, samples: window.__samples }));
  check(`no control moved (${controls} controls, ${samples} samples over ~${Math.round(samples / 20)} s)`, moves.length === 0, moves.join("; "));
} catch (err) {
  check("run", false, err?.stack ?? String(err));
} finally {
  await browser?.close();
  stop(vite);
  stop(server);
}
console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
