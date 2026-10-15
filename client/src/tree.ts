/** Typed tag references: the generated tag tree (`tagTree`) and client-side derived values (`derive`). */

declare const V: unique symbol;
declare const W: unique symbol;

/** A tag name with its value type V and whether it may be written (W); `String(ref)` is the name.
 * V and W exist only for the compiler. */
export interface TagRef<Val, Wr extends boolean = boolean> {
  readonly [V]: Val;
  readonly [W]: Wr;
  toString(): string;
}

/** An integer tag: `.bit(n)` is its bit n (`"X.n"`), read from the word client-side. */
export type IntRef<Wr extends boolean = boolean> = TagRef<number, Wr> & { bit(n: number): TagRef<boolean, Wr> };

/** An array level of the tree: indexable, with `length` the size of that dimension. */
export type TagArray<T, N extends number = number> = { readonly [i: number]: T; readonly length: N };

/** A value computed in the client from other tags (see `derive`); never written, never sent to the server. */
export type DerivedRef<Val> = TagRef<Val, false> & { readonly derived: true; readonly name: string };

/** Anything that names a tag: a string as the server takes it, a tree ref, or a derived ref. */
export type TagName = string | TagRef<any, any>;

/** The `get` passed to a derive function: the current value of a tag (null while unknown), recorded as a dependency. */
export type DeriveGet = <Val>(ref: TagRef<Val, any> | string) => Val | null;

/** Paths in `meta` are unqualified with each index group written `[]`: `"Tank"`, `"Tank[].Status"`, `"Grid[].X"`. */
export interface TreeMeta {
  /** array dimensions by path, e.g. `{"Tank": [4], "Grid": [3, 4]}`; arrays not listed are taken as 1-D */
  dims?: Record<string, number[]>;
  /** bit names of integer tags by path, e.g. `{"Bench.StatusWord": {"EStop": 0}}` */
  bits?: Record<string, Record<string, number>>;
}

/** A Proxy that builds tag names from property access: `tagTree<PLC1Tags>("PLC1").Tank[2].Level` is
 * `"PLC1/Tank[2].Level"` (unqualified with `plc = null`). Members named `bit`, `length`, `then` or `toString`
 * can only be reached with string names. */
export function tagTree<T>(plc: string | null, meta: TreeMeta = {}): T {
  const node = (name: string, path: string, idx: number[]): unknown => {
    const full = idx.length ? `${name}[${idx.join(",")}]` : name;
    const str = () => full;
    return new Proxy(
      {},
      {
        get(_, prop) {
          if (prop === Symbol.toPrimitive || prop === "toString" || prop === "toJSON") return str;
          if (typeof prop === "symbol" || prop === "then") return undefined;
          if (prop === "length") return meta.dims?.[path]?.[idx.length];
          if (prop === "bit") return (n: number) => node(`${full}.${n}`, `${path}.${n}`, []);
          if (/^\d+$/.test(prop)) {
            const next = [...idx, Number(prop)];
            if (next.length < (meta.dims?.[path]?.length ?? 1)) return node(name, path, next);
            return node(`${name}[${next.join(",")}]`, `${path}[]`, []);
          }
          const bit = meta.bits?.[path]?.[prop];
          if (bit !== undefined) return node(`${full}.${bit}`, `${path}.${bit}`, []);
          if (!path) return node(plc ? `${plc}/${prop}` : prop, prop, []);
          return node(`${full}.${prop}`, `${path}.${prop}`, []);
        },
      },
    );
  };
  return node(plc ?? "", "", []) as T;
}

/** @internal the runtime shape of a derived ref */
export interface DerivedImpl {
  readonly derived: true;
  readonly name: string;
  readonly fn: (get: DeriveGet) => unknown;
}

let derivedCount = 0;

/** A value computed from other tags: `fn(get)` re-runs when any tag it read changes, and its dependencies are
 * tracked again each time. Create it at module scope or in `useMemo`. */
export function derive<T>(fn: (get: DeriveGet) => T, name?: string): DerivedRef<T> {
  const n = name ?? `derive:${++derivedCount}`;
  const ref: DerivedImpl & { toString(): string } = { derived: true, name: n, fn, toString: () => n };
  return ref as unknown as DerivedRef<T>;
}

export function isDerived(name: unknown): name is DerivedImpl {
  return typeof name === "object" && name !== null && (name as DerivedImpl).derived === true && typeof (name as DerivedImpl).fn === "function";
}
