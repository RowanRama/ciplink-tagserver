# Changelog

## ciplink-tagserver 0.3.0 / @ciplink/react 0.3.0

Needs ciplink 0.2 (`ciplink>=0.2,<0.3`). The protocol stays v1: every addition is optional, so 0.2 clients keep
working.

### Breaking

- **Writes need a writable rule.** A token `write` pattern alone no longer allows a write: the tag's rule must also
  say `writable = true`, or the write is rejected with `kind: "not_writable"`. Add `[[plc.X.rule]]` tables for
  your setpoints before upgrading.
- **Token write patterns no longer use fnmatch.** All patterns use one matcher: case-insensitive, `*` any run of
  characters, `?` one, `[` and `]` literal. `PLC1/Tank[*].Level` matches `PLC1/Tank[3].Level`; the 0.2 trick
  `Arr[[]3]` no longer matches anything.
- `types` writes a typed tag tree by default; `--format names` gives the 0.2 name union.
- A whole array written by name, or a name with an element count (`X{2}`), is a `bad_request`: write `X[i]`.

### Server

- Rules (`[[plc.X.rule]]`): `writable`, `min`, `max`, `units`, `description`, `deadband`, `always`, `bits`,
  `together`. Bit names (`X.EStop`) work wherever a tag name does.
- Poll groups (`[[plc.X.group]]`, `rate_ms`), applied only while a tag is at tier 1; `/stats` shows them.
- Quality from ciplink's error kinds; `bad` keeps the last value; value entries carry `error`.
- Writes run between the packets of a cycle and are read back; acks carry `kind` and the read-back `value`.
  Checks in order: `bad_request`, `not_allowed`, `not_writable`, `out_of_range`, then ciplink's `bad_value`.
- Momentary buttons (`op: "momentary"`) with a server watchdog (`momentary_timeout_ms`), released on disconnect
  and shutdown, and written again until the PLC takes the release.
- `info` messages (type, writable, limits, units, bits, rate, group, description) before the first values.
- Coalesced session output: a slow client skips intermediate values and never grows memory.
- The audit log is written by a background thread; `audit_log = ""` turns it off.
- Planner: ranged reads of array runs (`Trend[3]{5}`) and `together` structs.
- `GET /tags` adds `types`, `rules` and `groups`, with CORS for GET. `/stats` adds `writes`, `write_rejects`,
  `momentary_timeouts` and per-PLC `groups`.
- `--mock` simulates UDTs, arrays, strings, bits and writes like ciplink (structured `mock_tags` files).
- `tests/conformance/` checks any server serving the Test Bench over a real WebSocket.

### @ciplink/react

- Tree refs from the generated `src/plc.ts` (`tagTree`, `TagRef<V, W>`): read-only tags don't compile in
  `useWriteTag`, `NumericInput` or `MomentaryButton`.
- Bits from words (`X.3`, `.bit(3)`) computed in the client; `derive` for values computed from other tags.
- `useTagInfo`, `useMomentary`, `<NumericInput>`, `<MomentaryButton>`, `<TagBrowser>`; `TagState.error`; local
  write pre-checks with the server's kinds. Every component is fixed size.

### Examples

- `examples/bench` (the Test Bench) replaces `examples/react-app`, with an end-to-end check (`npm run e2e`).
