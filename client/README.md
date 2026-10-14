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
- `useTag(name, {tier}?)` returns `{value, quality, t}`. Tier 1 (default) is read every cycle, tier 2 every
  few seconds, tier 3 not at all. Quality is `pending` until the first value, then
  `good`, `bad`, `missing` or `stale`. Subscriptions are counted per tag across components: the first
  user subscribes, the last one to unmount unsubscribes, and changes made in the same tick go out together.
- `useTags(names)` returns `{[name]: {value, quality, t}}`.
- `useWriteTag(name)` returns `async (value) => ({ok, error})`, resolved from the server's ack;
  `{ok: false, error: "not connected"}` when there is no connection.
- `useTagServer()` returns `{state, plcs, canWrite}`: `state` is `connecting`, `connected`,
  `disconnected` or `unauthorized`; `plcs` maps PLC names to their states; `canWrite` says whether the
  token has any write patterns.
- Tag names are as the server takes them: `PLC1/Line1.Speed`, or `Line1.Speed` with one PLC. Values
  come back under the name you used.

## Displays that never move

`<TagValue name decimals? unit? width?>` and `<TagLamp name onColor? offColor?>` have a fixed width:
tabular numbers, a value box `width` characters wide (default 8, longer text is cut with an ellipsis),
reserved space for the unit and for a quality marker. Nothing shifts when a value gains digits or its
quality changes. The marker is empty for good, `…` pending, `~` stale, `!` bad, `?` missing, with the
meaning in its title. Both carry `data-quality` and the classes `ciplink-value` / `ciplink-lamp` for
your own CSS.

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

## Typed tag names

`ciplink-tagserver types --url http://127.0.0.1:8765 --token T --plc PLC1 --out src/tags.d.ts` writes
`export type TagName = "..." | ...;` from the server's tag list.

## Without React

`TagStore` is the connection and value store the hooks use; it works anywhere with a global `WebSocket`
(browsers, Node 22+): `acquire(name)`, `get(name)`, `listen(name, cb)`, `write(name, value)`,
`connect()`, `close()`.

## License

MIT
