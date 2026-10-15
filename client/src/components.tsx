import { createElement, useEffect, useId, useRef, useState, type CSSProperties, type ElementType, type ReactNode } from "react";
import { useMomentary, useTag, useTagInfo, useTagStore, useWriteTag } from "./react.js";
import type { Quality } from "./store.js";
import type { TagName, TagRef } from "./tree.js";

// ---- TagLink: prefetches the target page's tags while the link is on screen

const pages = new Map<string, TagName[]>();

/** Seeds the tags of the page at `path` for <TagLink to={path}>, on top of those the server learned. */
export function registerPage(path: string, tags: TagName[]) {
  pages.set(path, tags);
}

export interface TagLinkProps {
  to: string;
  /** a router's link component, e.g. react-router's Link; a plain <a href> by default */
  as?: ElementType;
  children?: ReactNode;
  [prop: string]: unknown;
}

export function TagLink({ to, as, ...rest }: TagLinkProps) {
  const store = useTagStore();
  useEffect(() => {
    // the tags the server learned for the page, and any registered by hand, at tier 2
    const releases = [store.acquirePage(to), ...(pages.get(to) ?? []).map((name) => store.acquire(name, 2))];
    return () => releases.forEach((release) => release());
  }, [store, to]);
  return as ? createElement(as, { to, ...rest }) : createElement("a", { href: to, ...rest });
}

// ---- fixed-size displays and controls: nothing moves when a value, its quality or a pending state changes

const MARKS: Record<Quality, string> = { good: "", pending: "…", stale: "~", bad: "!", missing: "?" };
const TITLES: Record<Quality, string> = {
  good: "good",
  pending: "waiting for the first value",
  stale: "stale: connection lost, last value shown",
  bad: "bad: the read failed, last value shown",
  missing: "missing: no such tag, or not readable",
};
const COLORS: Record<Quality, string | undefined> = {
  good: undefined,
  pending: "#888",
  stale: "#888",
  bad: "#c62828",
  missing: "#c62828",
};

export const box: CSSProperties = { display: "inline-block", whiteSpace: "nowrap", overflow: "hidden", verticalAlign: "bottom" };
const fixed = (width: string): CSSProperties => ({ ...box, width, boxSizing: "border-box", flex: "none" });
// read by screen readers, takes no space
const hidden: CSSProperties = { position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)", whiteSpace: "nowrap" };

function Mark({ quality, title }: { quality: Quality; title?: string }) {
  return (
    <span className="ciplink-mark" title={title ?? TITLES[quality]} style={{ ...box, width: "1.5ch", textAlign: "center" }}>
      {MARKS[quality]}
    </span>
  );
}

function format(value: unknown, decimals?: number): string {
  if (value === null || value === undefined) return "--";
  if (typeof value === "number" && decimals !== undefined) return value.toFixed(decimals);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const errorTitle = (quality: Quality, error: { message: string } | null) =>
  error ? `${TITLES[quality]}: ${error.message}` : TITLES[quality];

export interface TagValueProps {
  name: TagName;
  decimals?: number;
  /** the unit text; from the tag's info `units` when not given, `""` for no unit slot */
  unit?: string;
  /** width of the unit slot in characters when `unit` is not given (default 4) */
  unitWidth?: number;
  /** width of the value in characters (default 8); longer text is cut with an ellipsis */
  width?: number;
  tier?: number;
  className?: string;
}

/** A tag's value in a fixed-width, tabular-numbers box, with its unit and a quality marker. */
export function TagValue({ name, decimals, unit, unitWidth = 4, width = 8, tier, className }: TagValueProps) {
  const { value, quality, error } = useTag(name, { tier });
  const info = useTagInfo(name);
  const text = format(value, decimals);
  const units = unit ?? info?.units ?? "";
  const slot = unit === undefined ? unitWidth : unit ? unit.length + 1 : 0;
  return (
    <span
      className={className ? `ciplink-value ${className}` : "ciplink-value"}
      data-quality={quality}
      title={`${name}: ${text} (${quality})`}
      style={{ ...box, fontVariantNumeric: "tabular-nums", color: COLORS[quality] }}
    >
      <span style={{ ...box, width: `${width}ch`, textAlign: "right", textOverflow: "ellipsis", textDecoration: quality === "missing" ? "line-through" : undefined }}>
        {text}
      </span>
      {slot ? <span style={{ ...box, width: `${slot}ch`, textAlign: "right", textOverflow: "ellipsis" }}>{units}</span> : null}
      <Mark quality={quality} title={errorTitle(quality, error)} />
    </span>
  );
}

export interface TagLampProps {
  name: TagName;
  onColor?: string;
  offColor?: string;
  tier?: number;
  className?: string;
}

/** A fixed-size lamp: lit when the value is truthy; dashed when stale, hollow while pending or missing. Bad keeps
 * showing the last value, with its mark. */
export function TagLamp({ name, onColor = "#2e7d32", offColor = "#9e9e9e", tier, className }: TagLampProps) {
  const { value, quality, error } = useTag(name, { tier });
  const known = (quality === "good" || quality === "stale" || quality === "bad") && value !== null;
  const color = known ? (value ? onColor : offColor) : "transparent";
  const label = `${name}: ${known ? (value ? "on" : "off") : "--"} (${quality})`;
  return (
    <span
      className={className ? `ciplink-lamp ${className}` : "ciplink-lamp"}
      data-quality={quality}
      role="img"
      aria-label={label}
      title={label}
      style={{ ...box, verticalAlign: "middle" }}
    >
      <span
        style={{
          display: "inline-block",
          boxSizing: "border-box",
          width: "1em",
          height: "1em",
          borderRadius: "50%",
          verticalAlign: "middle",
          background: color,
          border: `2px ${quality === "stale" ? "dashed" : "solid"} ${COLORS[quality] ?? color}`,
          opacity: quality === "stale" ? 0.6 : 1,
        }}
      />
      <span style={{ color: COLORS[quality] }}>
        <Mark quality={quality} title={errorTitle(quality, error)} />
      </span>
    </span>
  );
}

// ---- NumericInput

export interface NumericInputProps {
  /** a writable number tag: a string, or a `TagRef<number, true>` */
  name: string | TagRef<number, true>;
  decimals?: number;
  /** width of the input in characters (default 8) */
  width?: number;
  /** ask for confirmation in place before writing (default true) */
  confirm?: boolean;
  /** width of the result slot in characters (default 12) */
  resultWidth?: number;
  tier?: number;
  className?: string;
}

type Result = { text: string; title: string; ok: boolean } | null;

/** A fixed-width input and Set button for a number tag, with an inline confirm step in reserved space and a
 * fixed-width result slot. Min and max come from the tag's info. */
export function NumericInput({ name, decimals, width = 8, confirm = true, resultWidth = 12, tier, className }: NumericInputProps) {
  const store = useTagStore();
  const { value, quality } = useTag(name as TagName, { tier });
  const info = useTagInfo(name as TagName);
  const write = useWriteTag(name as string);
  const [draft, setDraft] = useState<string | null>(null);
  const [asking, setAsking] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result>(null);
  const input = useRef<HTMLInputElement>(null);
  const resultId = useId();
  const readOnly = info?.writable === false;
  const range = info && (info.min !== undefined || info.max !== undefined) ? ` (${info.min ?? "…"} to ${info.max ?? "…"})` : "";

  const send = async (n: number) => {
    input.current?.focus(); // the OK button is hidden now: keep the keyboard user's place
    setAsking(null);
    setBusy(true);
    setResult({ text: "…", title: "writing", ok: true });
    const r = await write(n);
    setBusy(false);
    setResult(r.ok ? { text: "✓", title: "written", ok: true } : { text: r.error ?? "failed", title: `${r.kind}: ${r.error}`, ok: false });
    if (r.ok) setDraft(null);
  };
  const submit = () => {
    if (draft === null || busy) return;
    const n = Number(draft);
    if (draft.trim() === "" || !Number.isFinite(n)) return setResult({ text: "not a number", title: "not a number", ok: false });
    const rejected = store.check(name as TagName, n);
    if (rejected) return setResult({ text: rejected.error!, title: `${rejected.error}${range}`, ok: false });
    if (confirm) setAsking(n);
    else void send(n);
  };
  const cancel = () => {
    input.current?.focus(); // the Cancel button is hidden now (as in send)
    setAsking(null);
    setDraft(null);
  };

  const shown = draft ?? (typeof value === "number" ? format(value, decimals) : value === null ? "" : String(value));
  const button: CSSProperties = { ...fixed("7ch"), padding: 0, textAlign: "center" };
  return (
    <span className={className ? `ciplink-input ${className}` : "ciplink-input"} data-quality={quality} style={{ ...box, verticalAlign: "middle" }}>
      <input
        type="text"
        inputMode="decimal"
        aria-label={String(name)}
        aria-describedby={resultId}
        title={readOnly ? `${name}: not writable` : `${name}${range}`}
        ref={input}
        value={shown}
        disabled={readOnly}
        readOnly={busy}
        onChange={(e) => {
          setDraft(e.target.value);
          setAsking(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          else if (e.key === "Escape") cancel();
        }}
        style={{ ...fixed(`${width + 2}ch`), fontVariantNumeric: "tabular-nums", textAlign: "right", color: COLORS[quality] }}
      />
      <button type="button" className="ciplink-set" disabled={readOnly || busy || asking !== null || draft === null} onClick={submit} style={button}>
        Set
      </button>
      {confirm ? (
        <span className="ciplink-confirm" style={{ ...fixed("15ch"), visibility: asking === null ? "hidden" : "visible" }}>
          <button type="button" title={asking === null ? undefined : `write ${asking} to ${name}`} onClick={() => asking !== null && send(asking)} style={button}>
            OK
          </button>
          <button type="button" onClick={cancel} style={{ ...button, width: "8ch" }}>
            Cancel
          </button>
        </span>
      ) : null}
      <span
        className="ciplink-result"
        id={resultId}
        role="status"
        aria-live="polite"
        title={result?.title}
        style={{ ...fixed(`${resultWidth}ch`), paddingLeft: "1ch", textOverflow: "ellipsis", color: result && !result.ok ? "#c62828" : undefined }}
      >
        {result?.text ?? ""}
      </span>
    </span>
  );
}

// ---- MomentaryButton

export interface MomentaryButtonProps {
  /** a writable BOOL tag or bit: a string, or a `TagRef<boolean, true>` */
  name: string | TagRef<boolean, true>;
  children?: ReactNode;
  /** width of the button in characters (default 10) */
  width?: number;
  disabled?: boolean;
  className?: string;
}

/** A push button: true while held (press, then hold every 300 ms), false on pointer up or cancel, lost pointer
 * capture, window blur and unmount. The server releases it by itself if the holds stop. */
export function MomentaryButton({ name, children, width = 10, disabled, className }: MomentaryButtonProps) {
  const m = useMomentary(name);
  const info = useTagInfo(name as TagName);
  const [pressed, setPressed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const down = useRef(false);
  const markId = useId();
  const release = () => {
    if (!down.current) return;
    down.current = false;
    setPressed(false);
    void m.release().then((r) => !r.ok && setError(r.error));
  };
  const releaseRef = useRef(release);
  releaseRef.current = release;
  useEffect(() => {
    const blur = () => releaseRef.current();
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("blur", blur);
      blur();
    };
  }, []);
  const off = disabled || info?.writable === false;
  const press = () => {
    if (off || down.current) return false;
    down.current = true;
    setPressed(true);
    setError(null);
    void m.press().then((r) => !r.ok && setError(r.error));
    return true;
  };
  const isKey = (key: string) => key === " " || key === "Enter";
  return (
    <span className={className ? `ciplink-momentary ${className}` : "ciplink-momentary"} style={{ ...box, verticalAlign: "middle" }}>
      <button
        type="button"
        disabled={off}
        aria-pressed={pressed}
        aria-describedby={markId}
        data-pressed={pressed}
        title={off && info?.writable === false ? `${name}: not writable` : String(name)}
        onPointerDown={(e) => {
          if (e.button > 0) return; // the primary button, a touch or a pen tip only
          if (press()) e.currentTarget.setPointerCapture?.(e.pointerId);
        }}
        onPointerUp={release}
        onPointerCancel={release}
        onLostPointerCapture={release}
        onKeyDown={(e) => {
          if (!isKey(e.key)) return;
          e.preventDefault();
          if (!e.repeat) press();
        }}
        onKeyUp={(e) => isKey(e.key) && release()}
        onBlur={release}
        onContextMenu={(e) => e.preventDefault()}
        style={{ ...fixed(`${width}ch`), touchAction: "none", userSelect: "none", filter: pressed ? "brightness(0.8)" : undefined }}
      >
        {children ?? String(name)}
      </button>
      <span id={markId} className="ciplink-mark" role="status" aria-live="polite" title={error ?? undefined} style={{ ...box, width: "1.5ch", textAlign: "center", color: "#c62828" }}>
        <span aria-hidden="true">{error ? "!" : ""}</span>
        <span style={hidden}>{error ?? ""}</span>
      </span>
    </span>
  );
}
