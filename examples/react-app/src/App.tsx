import { useState, type FormEvent } from "react";
import { TagLamp, TagValue, useTagServer, useWriteTag } from "@ciplink/react";

const row = { display: "grid", gridTemplateColumns: "16ch auto", alignItems: "center", justifyItems: "start", gap: "1ch", margin: "4px 0" };

export function App() {
  const { state, plcs, canWrite } = useTagServer();
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: 16 }}>
      <h1 style={{ fontSize: 20 }}>Line 1</h1>
      <div style={row}>
        <span>Speed</span>
        <TagValue name="Line1.Station1.Speed" decimals={1} unit="m/min" />
      </div>
      <div style={row}>
        <span>Setpoint</span>
        <TagValue name="Line1.Station1.SpeedSP" decimals={1} unit="m/min" />
      </div>
      <div style={row}>
        <span>Part count</span>
        <TagValue name="PartCount" />
      </div>
      <div style={row}>
        <span>Running</span>
        <TagLamp name="Line1.Running" />
      </div>
      <div style={row}>
        <span>Door open</span>
        <TagLamp name="Program:Station1.DoorOpen" onColor="#ef6c00" />
      </div>
      <SetpointWriter disabled={!canWrite || state !== "connected"} />
      {/* variable-width status text goes last, so it never moves a control */}
      <p style={{ color: "#666" }}>
        Server: {state}; PLCs: {Object.entries(plcs).map(([name, s]) => `${name} ${s}`).join(", ") || "none"}
      </p>
    </main>
  );
}

function SetpointWriter({ disabled }: { disabled: boolean }) {
  const write = useWriteTag("Line1.Station1.SpeedSP");
  const [input, setInput] = useState("50");
  const [result, setResult] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    const { ok, error } = await write(Number(input));
    setResult(ok ? "written" : `failed: ${error}`);
  }
  return (
    <form onSubmit={submit} style={{ ...row, gridTemplateColumns: "16ch 10ch max-content 1fr" }}>
      <span>New setpoint</span>
      <input value={input} onChange={(e) => setInput(e.target.value)} inputMode="decimal" style={{ width: "10ch" }} />
      <button type="submit" disabled={disabled}>
        Write
      </button>
      <span style={{ color: "#666" }}>{result}</span>
    </form>
  );
}
