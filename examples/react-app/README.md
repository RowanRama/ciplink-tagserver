# React example

One page with a few mock tags, a setpoint write and the connection state, using `@ciplink/react` from
`../../client`.

From the repository root, start the tag server with fake values:

```
uv run ciplink-tagserver run --config examples/tagserver.toml --mock
```

Build the client package once (the example links to its `dist/`):

```
cd client
npm install && npm run build
```

Then in `examples/react-app`:

```
npm install && npm run dev
```

and open http://localhost:5173. The app uses the token `change-me` (`[token.hmi1]` in
`examples/tagserver.toml`), which may write `Line1.*.SpeedSP`. The mock stores the write, then keeps
random-walking the value from there.
