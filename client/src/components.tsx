import { createElement, useEffect, type CSSProperties, type ElementType, type ReactNode } from "react";
import { useTag, useTagStore } from "./react.js";
import type { Quality } from "./store.js";

// ---- TagLink: prefetches the target page's tags while the link is on screen

const pages = new Map<string, string[]>();

/** Tells <TagLink to={path}> which tags the page at `path` shows. */
export function registerPage(path: string, tags: string[]) {
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
    const releases = (pages.get(to) ?? []).map((name) => store.acquire(name, 2));
    return () => releases.forEach((release) => release());
  }, [store, to]);
  return as ? createElement(as, { to, ...rest }) : createElement("a", { href: to, ...rest });
}

// ---- fixed-width displays: nothing moves when a value or its quality changes

const MARKS: Record<Quality, string> = { good: "", pending: "…", stale: "~", bad: "!", missing: "?" };
const TITLES: Record<Quality, string> = {
  good: "good",
  pending: "waiting for the first value",
  stale: "stale: connection lost, last value shown",
  bad: "bad: the read failed",
  missing: "missing: no such tag, or not readable",
};
const COLORS: Record<Quality, string | undefined> = {
  good: undefined,
  pending: "#888",
  stale: "#888",
  bad: "#c62828",
  missing: "#c62828",
};

const box: CSSProperties = { display: "inline-block", whiteSpace: "nowrap", overflow: "hidden", verticalAlign: "bottom" };

function Mark({ quality }: { quality: Quality }) {
  return (
    <span className="ciplink-mark" title={TITLES[quality]} style={{ ...box, width: "1.5ch", textAlign: "center" }}>
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

export interface TagValueProps {
  name: string;
  decimals?: number;
  unit?: string;
  /** width of the value in characters (default 8); longer text is cut with an ellipsis */
  width?: number;
  tier?: number;
  className?: string;
}

/** A tag's value in a fixed-width, tabular-numbers box, with its unit and a quality marker. */
export function TagValue({ name, decimals, unit, width = 8, tier, className }: TagValueProps) {
  const { value, quality } = useTag(name, { tier });
  const text = format(value, decimals);
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
      {unit ? <span style={{ ...box, width: `${unit.length + 1}ch`, textAlign: "right" }}>{unit}</span> : null}
      <Mark quality={quality} />
    </span>
  );
}

export interface TagLampProps {
  name: string;
  onColor?: string;
  offColor?: string;
  tier?: number;
  className?: string;
}

/** A fixed-size lamp: lit when the value is truthy, hollow for stale, bad or missing. */
export function TagLamp({ name, onColor = "#2e7d32", offColor = "#9e9e9e", tier, className }: TagLampProps) {
  const { value, quality } = useTag(name, { tier });
  const known = quality === "good" || quality === "stale";
  const color = known ? (value ? onColor : offColor) : "transparent";
  return (
    <span
      className={className ? `ciplink-lamp ${className}` : "ciplink-lamp"}
      data-quality={quality}
      title={`${name}: ${known ? (value ? "on" : "off") : "--"} (${quality})`}
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
        <Mark quality={quality} />
      </span>
    </span>
  );
}
