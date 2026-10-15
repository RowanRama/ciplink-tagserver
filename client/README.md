# @ciplink/react

React hooks and components for [ciplink-tagserver](https://github.com/RowanRama/ciplink-tagserver):
live PLC tag values over one WebSocket. React 18 or 19, no other runtime dependencies.

```
npm install @ciplink/react
```

## Use

```tsx
import { TagServerProvider, TagValue, TagLamp, useTag, useWriteTag, useTagServer } from "@ciplink/react";

<TagServerProvider url="ws://127.0.0.1:8765/ws" token="change-me" onMeta={(plc) => refetchTagList(plc)}>
  <App />
</TagServerProvider>

function Speed() {
  const { value, quality, t } = useTag("PLC1/Line1.Speed"); // re-renders only when this tag changes
  return <span>{quality === "good" ? value : "--"}</span>;
}
```

- `<TagServerProvider url token onMeta? page?>` opens one WebSocket for everything below it, says hello,
  reconnects after 1, 2, 5, then every 10 s and re-subscribes. While disconnected every value keeps its
  last value with quality `stale`. A rejected token (close code 4401) gives the state `unauthorized` and
  stops reconnecting. `onMeta(plc)` is called when a PLC's tag table was reloaded. `page` is the current
  route path, see below.
- `useTag(name, {tier}?)` returns `{value, quality, t, error}`. Tier 1 (default) is read every cycle, tier 2 every
  few seconds, tier 3 not at all. Quality is `pending` until the first value, then
  `good`, `bad` (the read failed; the last value is kept), `missing` or `stale`. `error` is
  `{kind, message}` for bad and missing, else null. Subscriptions are counted per tag across components: the first
  user subscribes, the last one to unmount unsubscribes, and changes made in the same tick go out together.
- `useTags(names)` returns `{[String(name)]: {value, quality, t, error}}`.
- `useTagInfo(name)` returns the server's info for a subscribed tag, or null until it arrives:
  `{type, writable, min?, max?, units?, deadband?, description?, bits?, dims?, rate_ms, group}`.
- `useWriteTag(name)` returns `async (value) => ({ok, error, kind, value})`, resolved from the server's ack;
  `value` is the value read back after the write. `kind` is null on success, else `comm` (`"not connected"`,
  `"disconnected"`), `not_allowed`, `not_writable`, `out_of_range`, `bad_value`, ... When the tag's info says
  it is not writable or the value is outside `min`/`max`, the write is rejected locally with those kinds and
  nothing is sent (the server checks again).
- `useMomentary(name)` returns `{press(), release()}` for a BOOL tag or bit: `press` writes true and sends a
  hold every 300 ms, `release` writes false. If the holds stop (a crashed tab), the server releases it.
  Released on unmount. Subscribes the tag while mounted, so its info arrives.
- `useTagServer()` returns `{state, plcs, canWrite}`: `state` is `connecting`, `connected`,
  `disconnected` or `unauthorized`; `plcs` maps PLC names to their states; `canWrite` says whether the
  token has any write patterns.
- Tag names are as the server takes them: `PLC1/Line1.Speed`, or `Line1.Speed` with one PLC. Values
  come back under the name you used. Everywhere a name is taken, a tree ref or a derived ref works too.
- **Bits**: `Line1.Status.3` (a word, a dot, digits) subscribes the word and computes bit 3 in the client
  (BigInt, so LINT works; a DWORD array by element). Bit names from rules (`Line1.Status.EStop`) go to the
  server as given. Writes and momentary go to the server as `X.<n>`. A bit's info has no `writable` (a rule
  or token pattern may allow the bit and not its word), so bit writes are never rejected locally as not
  writable: the server decides.

## Components that never move

Every component has a fixed size: nothing shifts when a value gains digits, its quality changes, info arrives,
or a write is pending, asking or done.

- `<TagValue name decimals? unit? unitWidth? width?>`: tabular numbers in a value box `width` characters wide
  (default 8, longer text is cut with an ellipsis), a unit slot and a quality marker. Without `unit` the slot is
  `unitWidth` characters (default 4) filled from the tag's info `units`; `unit=""` drops it.
- `<TagLamp name onColor? offColor?>`: lit when truthy, dashed when stale; bad shows the last value.
- `<NumericInput name decimals? width? confirm? resultWidth?>`: an input and a Set button for a writable number.
  Min and max come from the tag's info and are checked before asking. With `confirm` (default) Set shows
  OK / Cancel in space that is always reserved (never `window.confirm`); the result (`…`, `✓` or the error) goes
  in a fixed-width slot. Enter sets, Escape cancels.
- `<MomentaryButton name width?>Start</MomentaryButton>`: true while held. Presses on primary pointer down
  (with pointer capture) or while Space or Enter is held, releases on pointer up, pointer cancel, lost capture,
  key up, blur and unmount. A failed press shows `!` in a fixed slot.
- `<TagBrowser plc? url? qualified? nameWidth?>`: the PLC's tags from `GET /tags` (the HTTP base comes from the
  provider's ws URL: `ws://host:8765/ws` -> `http://host:8765`), structs expanded through their types. Each row
  has the name, value, type, RW/RO and a copy button for its path. Only leaf rows on screen subscribe (tier 1);
  children appear when expanded, and arrays and the top level are paged 50 at a time. Reloads on `meta`. The
  server must allow the page's origin (0.3 sends CORS headers for `GET`). Don't point a `TagLink` at its page.

The marker is empty for good, `…` pending, `~` stale, `!` bad, `?` missing, with the meaning (and the error) in
its title. Components carry `data-quality` and `ciplink-*` classes for your own CSS.

## Prefetching the next page

```tsx
import { Link, useLocation } from "react-router-dom";
import { TagLink, TagServerProvider } from "@ciplink/react";

function Root() {
  const { pathname } = useLocation();
  return (
    <TagServerProvider url="ws://127.0.0.1:8765/ws" token="change-me" page={pathname}>
      <App />
    </TagServerProvider>
  );
}

<TagLink to="/station1" as={Link}>Station 1</TagLink>   // a plain <a href> without `as`
```

With `page` set, the tier-1 subscriptions carry the current route and the server learns which tags each
page shows (and keeps that across restarts). While a `<TagLink to>` is mounted it subscribes the tags
learned for its target at tier 2, so the page opens with values. A page never visited has no tags yet;
`registerPage(path, tags)` seeds one by hand (those tags are subscribed at tier 2 too).

## Typed tags

`ciplink-tagserver types --url http://127.0.0.1:8765 --token T --plc PLC1 --out src/plc.ts` writes interfaces
for the PLC's tags and UDTs and a tree built with `tagTree`:

```tsx
import { PLC1 } from "./plc";

useTag(PLC1.Bench.Speed);                        // TagState<number>
useWriteTag(PLC1.Bench.SpeedSP)(1200);           // only compiles for writable tags (rules with writable = true)
<NumericInput name={PLC1.Bench.SpeedSP} />
<TagLamp name={PLC1.Bench.StatusWord.EStop} />   // bit names from rules
<TagLamp name={PLC1.Bench.StatusWord.bit(3)} />
<TagValue name={PLC1.Tank[2].Level} />
```

`tagTree<T>(plc, meta?)` is a Proxy that builds names from property access: `PLC1.Tank[2].Level` is
`"Tank[2].Level"` (or `"PLC1/Tank[2].Level"` when `plc` is given), `Grid[1][2]` is `"Grid[1,2]"`, and
`String(ref)` is the name. The types: `TagRef<V, W>` (value type V, writable W), `IntRef<W>` (with `.bit(n)`),
`TagArray<T, N>`, `DerivedRef<V>` and `TagName = string | TagRef`. Strings are accepted everywhere.

## Derived values

```tsx
const avgLevel = derive((get) => {
  const levels = [0, 1, 2, 3].map((i) => get(PLC1.Tank[i].Level) ?? 0);
  return levels.reduce((a, b) => a + b) / levels.length;
}, "avgLevel");

<TagValue name={avgLevel} decimals={1} unit="%" />
```

`derive(fn, name?)` runs `fn(get)` in the client whenever a tag it read changes; the tags it reads are tracked
again on every run, so conditional reads work. The result is pending while any input is, else it takes the
worst quality of its inputs (bad > missing > stale > good); a throw (or a cycle) gives `bad` with error kind
`derive`. Create it at module scope or in `useMemo`. It works in `useTag`, `useTags`, `TagValue` and `TagLamp`,
never reaches the server and is never writable.

## Without React

`TagStore` is the connection and value store the hooks use; it works anywhere with a global `WebSocket`
(browsers, Node 22+): `acquire(name)`, `get(name)`, `listen(name, cb)`, `tagInfo(name)`,
`write(name, value)`, `momentary(name)`, `connect()`, `close()`.

## License

MIT
