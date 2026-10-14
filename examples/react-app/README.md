# React example

Two pages (Line 1 and Station 1) with a few mock tags, a setpoint write and the connection state, using
`@ciplink/react` from `../../client`.

From the repository root, start the tag server with fake values:

```
uv run ciplink-tagserver run --config examples/tagserver.toml --mock
```

Then in `examples/react-app`:

```
npm install && npm run dev
```

and open http://localhost:5173. `npm run dev` (and `npm run build`) first installs and builds
`../../client`, which the example links to.

The app uses the token `change-me` (`[token.hmi1]` in `examples/tagserver.toml`), which may write
`Line1.*.SpeedSP`. The mock stores the write, then keeps random-walking the value from there.

**Tiers.** Routes are hash paths (`#/station1`) passed to `<TagServerProvider page>`, and the links are
`<TagLink>`s. Once you have opened Station 1, the server knows its tags (`examples/pages.json`), and
while you are on Line 1 the link to Station 1 keeps them read at tier 2, so going back shows values at
once. `GET http://127.0.0.1:8765/stats` shows `tier1`, `tier2` and the reads per cycle.
