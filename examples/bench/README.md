# Test Bench example

A small mock-driven bench that exercises every signal of ciplink-tagserver 0.3 and `@ciplink/react`
(linked from `../../client`). Three pages: Overview, Motion and a tag browser.

| tag | type | shows |
|---|---|---|
| Bench.Running | BOOL | lamp |
| Bench.StartPB / Bench.StopPB | BOOL | momentary buttons (server watchdog) |
| Bench.SpeedSP | REAL | writable, 0 to 1800 rpm, `NumericInput` |
| Bench.Speed | REAL | deadband 0.5, follows the setpoint while running |
| Bench.Axis.Position / Velocity | REAL | fast group (50 ms), only while Motion is open |
| Bench.StatusWord | DINT | `always`, bit names EStop/DoorOpen/Fault, bit 3 from the word |
| Bench.Count, Bench.Message | DINT, STRING | counter, string |
| Bench.Recipe | RecipeData {Name, Setpoints[4]} | `together`, whole-struct value |
| Tank[4] | TankData {Level, Temp, Valve} | array of UDT, slow group on Temp, derived average level |
| Trend | REAL[16] | 16 elements read as one ranged read |
| Program:Bench.Mode | DINT | program scope |
| Bench.NoSuchTag | | missing quality |

The rules and groups are in [tagserver.toml](tagserver.toml), the simulated tags in
[mock_tags.json](mock_tags.json), derived values in [src/derived.ts](src/derived.ts).

## Run

From the repository root, start the tag server with simulated values:

```
uv run ciplink-tagserver run --config examples/bench/tagserver.toml --mock
```

Then here: `npm install && npm run dev`, and open http://localhost:5173. `npm run dev` first builds
`../../client`. The app uses the token `bench-hmi`, which may write `PLC1/Bench.*`; the rules make only the
setpoint and the two buttons writable.

- **Tiers.** Routes are hash paths passed to `<TagServerProvider page>`; the Overview and Motion links are
  `<TagLink>`s. Once Motion has been opened, the link on Overview keeps its tags read at tier 2.
- **Poll groups.** `GET http://127.0.0.1:8765/stats`: `plcs.PLC1.groups.fast.tags` is 2 while Motion is open
  and 0 otherwise.
- **Momentary.** Hold Start: `Bench.StartPB` stays true while held (the client sends `hold` every 300 ms),
  `Running` turns on and the speed follows the setpoint. Release, close the tab, or stop the holds, and the
  server writes false within `momentary_timeout_ms`.

## Regenerate the tag tree

`src/plc.ts` is generated, never edited. With the mock server running, from the repository root:

```
uv run ciplink-tagserver types --url http://127.0.0.1:8765 --token bench-hmi --out examples/bench/src/plc.ts
```

(`npm run codegen` does the same with `ciplink-tagserver` on the PATH.)

## End-to-end check

```
npm run e2e
```

starts the mock server and Vite, drives the app in the installed Chrome (`playwright-core`, no browser
download; set `CHROME` to another binary) and checks: values arrive good and the unknown tag is missing,
the fast group runs only on Motion, an out-of-range entry is refused, a write is read back, Start
press/hold/release, a held button released when its page closes, the watchdog releasing a press with no
holds, the audit log, the tag browser, stale values after the server stops, and that no button, input or
link moves by a pixel over the whole run. It saves [screenshot.png](screenshot.png). `TAGSERVER` sets the
command that runs the server (default `uv run ciplink-tagserver`, run from the repository root).
