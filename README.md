# ciplink-tagserver

One server per site that owns all the PLC connections and serves tag values to React (or any) apps
over a WebSocket. Apps never talk to the PLCs: each PLC gets one connection, one thread and one poll
loop, however many screens are open.

It is built on [ciplink](https://pypi.org/project/ciplink/), a fork of pycomm3 that reads by instance
ID and reloads its tag table by itself after online edits.

## Install

```
pip install ciplink-tagserver
```

or from a checkout with [uv](https://docs.astral.sh/uv/): `uv sync`, then `uv run ciplink-tagserver ...`.

## Run

```
ciplink-tagserver run --config tagserver.toml          # real PLCs
ciplink-tagserver run --config tagserver.toml --mock   # fake values, no PLC needed
ciplink-tagserver dump-tags --config tagserver.toml --plc PLC1 --out PLC1_tags.json
ciplink-tagserver types --url http://127.0.0.1:8765 --token T --plc PLC1 --out src/tags.d.ts
```

`types` asks a running server for its tag list (`GET /tags`) and writes a TypeScript union,
`export type TagName = "Line1.Speed" | ...;`. AOIs appear as their readable members (`Valve1.Open`),
never their local tags. Pass `--plc` once per PLC; with more than one, names are qualified (`PLC1/Line1.Speed`).

Add `--log-file tagserver.log` to `run` to log to a file (rotated at 10 MB, 5 kept) instead of the console.

## Run at Windows startup

From an administrator terminal (right-click Terminal, "Run as administrator"):

```
ciplink-tagserver service install --config C:\TagServer\tagserver.toml
ciplink-tagserver service status
ciplink-tagserver service remove
```

`install` registers a Task Scheduler task that starts at every boot as SYSTEM, without anyone logging in,
and starts it now. The task runs `ciplink-tagserver service run`, which starts the server again 5 s after
it stops for any reason; Task Scheduler also restarts the task itself if it fails. Logs go to
`tagserver.log` next to the config (`--log-file` to change it). Use full paths in the config, or paths
relative to it; the task runs in the config's folder. `--name` sets the task name (default
`ciplink-tagserver`), so several servers can run on one PC with different configs and ports.

## Config

```toml
[server]
host = "127.0.0.1"      # localhost only by default
port = 8765
cycle_ms = 250          # poll period per PLC
audit_log = "audit.log" # optional, JSON lines of every write attempt

[plc.PLC1]
path = "192.168.1.10"                    # a ciplink/pycomm3 path, e.g. "10.0.0.5/1/3"
tag_cache_file = "cache/PLC1.json"   # optional: fast restarts
mock_tags = "PLC1_tags.json"         # optional: used only with --mock

[token.viewer]
secret = "change-me-too"                # no write patterns: read-only

[token.hmi1]
secret = "change-me"
write = ["PLC1/Line1.*.SpeedSP", "PLC1/Program:Station1.Door*"]
```

Relative file paths are relative to the config file. A working example is in `examples/`.

**Tag names** are qualified as `<plc>/<tag>`, e.g. `PLC1/Program:Main.Speed` (a slash cannot appear
in a Logix tag name). With exactly one PLC configured, unqualified names mean that PLC.

**Write patterns** are `fnmatch` patterns matched against the qualified name, ignoring case. `*` also
matches dots, and `[...]` is a character class, so to allow `Arr[3]` write `Arr[[]3]` or `Arr?3?`.

## Protocol (v1)

JSON messages over `ws://host:port/ws`.

Client to server:

| message | meaning |
|---|---|
| `{"op":"hello","v":1,"token":"<secret>"}` | must be the first message |
| `{"op":"sub","id":N,"tags":["..."],"tier":1}` | subscribe; reusing an id replaces that subscription |
| `{"op":"unsub","id":N}` | drop subscription N |
| `{"op":"write","id":N,"tag":"...","value":...}` | write one tag |

Server to client:

| message | when |
|---|---|
| `{"op":"welcome","v":1,"plcs":{"PLC1":"connected"},"write":true}` | after a good hello; `write` says whether the token has any write patterns |
| `{"op":"values","d":{"<tag>":[value, quality, t]}}` | changed values only; right after a `sub`, the values already cached |
| `{"op":"ack","id":N,"ok":true,"error":null}` | for `sub`, `unsub` and `write` |
| `{"op":"status","plcs":{"PLC1":"disconnected"}}` | a PLC changed state |
| `{"op":"meta","plc":"PLC1"}` | the PLC's tag table was reloaded (online edit, download): fetch `/tags` again |
| `{"op":"error","id":N,"error":"..."}` | bad request (`id` is null when unknown) |

- A bad token, a first message that is not `hello`, or another protocol version: the socket is closed with code **4401**.
- Tags in `values` are keyed by the name as the client subscribed it.
- `quality` is `good`, `bad` (the read failed), `missing` (no such tag, or an AOI local tag that cannot
  be read from outside) or `stale` (PLC disconnected: the last value is kept). `value` is null for bad and missing.
- `t` is the time of the change in ms since the epoch.
- Values: structs and AOIs are objects, arrays are lists, bytes are hex strings, NaN and infinities are
  the strings `"NaN"`, `"Infinity"`, `"-Infinity"`.
- PLC states: `connecting`, `connected`, `disconnected`. Reconnects back off 1, 2, 5, then every 10 s.
- `tier` is stored but not used yet: every subscribed tag is read every cycle.
- Writes go to the PLC's thread, run after that cycle's read, and the ack carries the driver's result.
  They are never retried; a write while the PLC is not connected fails at once.

## HTTP

- `GET /health`: per PLC `state`, `last_cycle_ms`, `last_ok_ts`, `reconnects`, `reloads`.
- `GET /stats`: `sessions`, and per PLC `reads_per_cycle`, `last_cycle_ms`, `cycle_ms`, `overruns`, `subscriptions`.
- `GET /tags?plc=NAME` with `Authorization: Bearer <secret>`: the PLC's tags with data type and, for
  AOIs, `aoi_readable` (the members that can be read). `plc` may be left out with one PLC.

## React

[`@ciplink/react`](client/) (in `client/`) is the React side: one provider that owns the WebSocket, and
hooks and components that subscribe while mounted.

```tsx
import { TagServerProvider, TagValue, TagLamp, useWriteTag, useTagServer } from "@ciplink/react";

<TagServerProvider url="ws://127.0.0.1:8765/ws" token="change-me">
  <TagValue name="Line1.Station1.Speed" decimals={1} unit="m/min" />
  <TagLamp name="Line1.Running" />
</TagServerProvider>
```

- `useTag(name)` gives `{value, quality, t}` and re-renders only when that tag changes; subscriptions are
  counted across components and batched.
- `useTags(names)`, `useWriteTag(name)` (resolves with the write's ack), `useTagServer()` (connection
  state, PLC states, `canWrite`).
- `<TagValue>` and `<TagLamp>` are fixed width with a quality marker, so nothing moves as values change.
- `<TagLink to as?>` prefetches the tags of a page registered with `registerPage(path, tags)`.
- Reconnects after 1, 2, 5, then every 10 s, re-subscribes, and shows values as `stale` meanwhile.

A runnable app is in [`examples/react-app/`](examples/react-app/): start the server with
`uv run ciplink-tagserver run --config examples/tagserver.toml --mock`, build `client/` once
(`npm install && npm run build`), then `npm install && npm run dev` in the example.

## Mock mode

`--mock` replaces every PLC driver with a fake serving the tags in that PLC's `mock_tags` file:

```json
{"Line1.Station1.Speed": "REAL", "Line1.Running": "BOOL", "PartCount": "DINT"}
```

Numbers random-walk, BOOLs toggle now and then, writes are stored, unknown names are `missing`. Other
types (UDTs, AOIs) read as `bad`; list their atomic members by full name to simulate them.
`dump-tags` writes this file from a real PLC (it only uploads the tag list), so a mock can mirror it.

## Safety

- Writes are off unless a token has `write` patterns, and only names matching them can be written.
- Every write attempt, allowed or not, is appended to the audit log.
- The server binds to `127.0.0.1` by default. Put it behind a TLS proxy before opening it to a network:
  tokens travel in clear text over plain `ws://` and `http://`.
- Change the example secrets.
