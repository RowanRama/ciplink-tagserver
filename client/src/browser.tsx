import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { box, TagValue } from "./components.js";
import { useTagInfo, useTagStore } from "./react.js";
import { httpBase } from "./store.js";

/** The parts of `GET /tags` the browser uses. */
export interface TagList {
  plc: string;
  version?: number | null;
  tags: { name: string; type: string | null; dims?: number[] }[];
  types: Record<string, { members: Record<string, { type: string | null; dims?: number[] }> }>;
}

export const PAGE = 50;

export interface TagBrowserProps {
  /** the PLC to list (`/tags?plc=`); the server's only PLC when not given */
  plc?: string;
  /** the tag server's HTTP base, e.g. "http://127.0.0.1:8765"; derived from the provider's ws URL by default */
  url?: string;
  /** qualify names as `PLC/tag` (default: when `plc` is given) */
  qualified?: boolean;
  /** width of the name column in characters (default 36) */
  nameWidth?: number;
  className?: string;
}

/** Copies text to the clipboard, falling back to a hidden textarea where the Clipboard API is missing. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {}
    area.remove();
    return ok;
  }
}

const cell = (width: string, extra?: CSSProperties): CSSProperties => ({ ...box, width, flex: "none", textOverflow: "ellipsis", ...extra });
// a fixed height: a glyph from a fallback font (✓, ▾) must not make the row taller
const button: CSSProperties = { ...box, width: "6ch", height: "1.5em", lineHeight: 1, padding: 0, flex: "none", textAlign: "center" };

/** A tree of a PLC's tags from `GET /tags`, structs expanded through its types. Only rows on screen subscribe
 * (tier 1): children appear when their parent is expanded, arrays and the top level are paged 50 at a time. */
export function TagBrowser({ plc, url, qualified, nameWidth = 36, className }: TagBrowserProps) {
  const store = useTagStore();
  const [list, setList] = useState<TagList | null>(null);
  const [status, setStatus] = useState("loading…");
  const [reload, setReload] = useState(0);
  const base = url ?? httpBase(store.url);
  useEffect(() => store.listenMeta((p) => (!plc || p === plc) && setReload((n) => n + 1)), [store, plc]);
  useEffect(() => {
    let live = true;
    const query = plc ? `?plc=${encodeURIComponent(plc)}` : "";
    fetch(`${base}/tags${query}`, { headers: { Authorization: `Bearer ${store.token}` } })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const body = (await r.json()) as TagList;
        if (!live) return;
        setList(body);
        setStatus(`${body.tags.length} tags${body.version != null ? `, table version ${body.version}` : ""}`);
      })
      .catch((e) => live && setStatus(`could not load tags: ${e instanceof Error ? e.message : e}`));
    return () => {
      live = false;
    };
  }, [base, plc, store, reload]);
  const prefix = qualified ?? !!plc ? `${plc ?? list?.plc}/` : "";
  const ctx: Ctx = { types: list?.types ?? {}, nameWidth };
  return (
    <div className={className ? `ciplink-browser ${className}` : "ciplink-browser"} style={{ fontVariantNumeric: "tabular-nums" }}>
      <div className="ciplink-browser-status" style={{ height: "1.5em", lineHeight: "1.5em", ...box, display: "block" }}>
        {status}
      </div>
      {list ? (
        <Paged total={list.tags.length} depth={0} ctx={ctx}>
          {(i) => {
            const t = list.tags[i];
            return <Row key={t.name} path={prefix + t.name} label={t.name} type={t.type} dims={t.dims} depth={0} ctx={ctx} />;
          }}
        </Paged>
      ) : null}
    </div>
  );
}

interface Ctx {
  types: TagList["types"];
  nameWidth: number;
}

interface RowProps {
  path: string;
  label: string;
  type: string | null;
  dims?: number[];
  depth: number;
  ctx: Ctx;
}

function Row({ path, label, type, dims, depth, ctx }: RowProps) {
  const [open, setOpen] = useState(false);
  const array = !!dims?.length;
  const members = type && !array ? ctx.types[type]?.members : undefined; // an array's elements have the members
  const leaf = !array && !members;
  const typeText = array ? `${type}[${dims!.join(",")}]` : type ?? "?";
  return (
    <>
      <div className="ciplink-row" data-path={path} style={{ display: "flex", alignItems: "center", whiteSpace: "nowrap" }}>
        <span style={cell(`${ctx.nameWidth}ch`, { paddingLeft: `${depth * 2}ch`, boxSizing: "border-box" })} title={path}>
          {leaf ? (
            <span style={cell("2ch")} />
          ) : (
            <button type="button" aria-label={label} aria-expanded={open} onClick={() => setOpen(!open)} style={{ ...button, width: "2ch", border: "none", background: "none" }}>
              {open ? "▾" : "▸"}
            </button>
          )}
          {label}
        </span>
        <span style={cell("15ch")}>{leaf ? <TagValue name={path} width={9} /> : null}</span>
        <span style={cell("12ch", { paddingLeft: "1ch" })} title={typeText}>
          {typeText}
        </span>
        <span style={cell("3ch")}>{leaf ? <Access path={path} /> : null}</span>
        <CopyButton text={path} />
      </div>
      {open && array ? <Elements path={path} type={type} dims={dims!} depth={depth + 1} ctx={ctx} /> : null}
      {open && members
        ? Object.entries(members).map(([m, info]) => (
            <Row key={m} path={`${path}.${m}`} label={m} type={info.type} dims={info.dims} depth={depth + 1} ctx={ctx} />
          ))
        : null}
    </>
  );
}

function Access({ path }: { path: string }) {
  const info = useTagInfo(path);
  return <>{info?.writable === undefined ? "" : info.writable ? "RW" : "RO"}</>;
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState<boolean | null>(null);
  useEffect(() => {
    if (done === null) return;
    const timer = setTimeout(() => setDone(null), 1500);
    return () => clearTimeout(timer);
  }, [done]);
  return (
    <button type="button" className="ciplink-copy" title={`copy ${text}`} onClick={() => copyText(text).then(setDone)} style={button}>
      {done === null ? "Copy" : done ? "✓" : "!"}
    </button>
  );
}

function Elements({ path, type, dims, depth, ctx }: { path: string; type: string | null; dims: number[]; depth: number; ctx: Ctx }) {
  const total = dims.reduce((a, b) => a * b, 1);
  return (
    <Paged total={total} depth={depth} ctx={ctx}>
      {(i) => {
        // row-major index tuple, merged as [a,b] like the server's names
        const idx: number[] = [];
        for (let k = dims.length - 1, r = i; k >= 0; k--) {
          idx.unshift(r % dims[k]);
          r = Math.floor(r / dims[k]);
        }
        const name = `[${idx.join(",")}]`;
        return <Row key={i} path={path + name} label={name} type={type} depth={depth} ctx={ctx} />;
      }}
    </Paged>
  );
}

/** Renders `PAGE` rows at a time, with a pager row (buttons first, so they never move) when there are more. */
function Paged({ total, depth, ctx, children }: { total: number; depth: number; ctx: Ctx; children: (i: number) => ReactNode }) {
  const [kept, setStart] = useState(0);
  const start = Math.min(kept, Math.max(0, Math.floor((total - 1) / PAGE) * PAGE)); // the list may have shrunk
  const end = Math.min(start + PAGE, total);
  const rows = [];
  for (let i = start; i < end; i++) rows.push(children(i));
  return (
    <>
      {total > PAGE ? (
        <div className="ciplink-pager" style={{ display: "flex", alignItems: "center", whiteSpace: "nowrap", paddingLeft: `${depth * 2 + 2}ch` }}>
          <button type="button" aria-label="previous" disabled={start === 0} onClick={() => setStart(start - PAGE)} style={button}>
            ‹
          </button>
          <button type="button" aria-label="next" disabled={end >= total} onClick={() => setStart(start + PAGE)} style={button}>
            ›
          </button>
          <span style={{ ...box, paddingLeft: "1ch", width: `${ctx.nameWidth}ch` }}>{`${start}–${end - 1} of ${total}`}</span>
        </div>
      ) : null}
      {rows}
    </>
  );
}
