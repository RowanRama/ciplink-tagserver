import type { ReactNode } from "react";
import { MomentaryButton, NumericInput, TagBrowser, TagLamp, TagValue, useTag, useTagInfo } from "@ciplink/react";
import { atSpeed, avgLevel } from "./derived";
import { PLC1 } from "./plc";

const { Bench, Tank, Trend } = PLC1;
const Mode = PLC1["Program:Bench"].Mode;

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="row">
      <span className="label">{label}</span>
      <span>{children}</span>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export function Overview() {
  return (
    <div className="panels">
      <Panel title="Spindle">
        <Row label="Running">
          <TagLamp name={Bench.Running} />
        </Row>
        <Row label="At speed">
          <TagLamp name={atSpeed} />
        </Row>
        <Row label="Start / Stop">
          <MomentaryButton name={Bench.StartPB} width={8}>
            Start
          </MomentaryButton>
          <MomentaryButton name={Bench.StopPB} width={8}>
            Stop
          </MomentaryButton>
        </Row>
        <Row label="Start PB">
          <TagLamp name={Bench.StartPB} onColor="#1565c0" />
        </Row>
        <Row label="Setpoint">
          <NumericInput name={Bench.SpeedSP} decimals={1} />
        </Row>
        <Row label="Speed">
          <TagValue name={Bench.Speed} decimals={1} />
        </Row>
      </Panel>

      <Panel title="Status word (always polled)">
        <Row label="Word">
          <TagValue name={Bench.StatusWord} unit="" />
        </Row>
        <Row label="E-stop">
          <TagLamp name={Bench.StatusWord.EStop} onColor="#c62828" />
        </Row>
        <Row label="Door open">
          <TagLamp name={Bench.StatusWord.DoorOpen} onColor="#ef6c00" />
        </Row>
        <Row label="Fault">
          <TagLamp name={Bench.StatusWord.Fault} onColor="#c62828" />
        </Row>
        <Row label="Bit 3">
          <TagLamp name={Bench.StatusWord.bit(3)} />
        </Row>
      </Panel>

      <Panel title="Values">
        <Row label="Count">
          <TagValue name={Bench.Count} unit="" />
        </Row>
        <Row label="Message">
          <TagValue name={Bench.Message} width={14} unit="" />
        </Row>
        <Row label="Mode (program)">
          <TagValue name={Mode} unit="" />
        </Row>
        <Row label="No such tag">
          <TagValue name="Bench.NoSuchTag" unit="" />
        </Row>
        <Row label="Recipe">
          <TagValue name={Bench.Recipe.Name} width={14} unit="" />
        </Row>
        <Row label="Recipe (whole)">
          <TagValue name={Bench.Recipe} width={30} unit="" />
        </Row>
      </Panel>

      <Panel title="Tanks (Temp in the slow group)">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Level</th>
              <th>Temp</th>
              <th>Valve</th>
            </tr>
          </thead>
          <tbody>
            {[0, 1, 2, 3].map((i) => (
              <tr key={i}>
                <td>{i}</td>
                <td>
                  <TagValue name={Tank[i].Level} decimals={1} width={6} unitWidth={2} />
                </td>
                <td>
                  <TagValue name={Tank[i].Temp} decimals={1} width={6} unitWidth={3} />
                </td>
                <td>
                  <TagLamp name={Tank[i].Valve} />
                </td>
              </tr>
            ))}
            <tr>
              <td>avg</td>
              <td>
                <TagValue name={avgLevel} decimals={1} width={6} unit="%" />
              </td>
              <td colSpan={2} className="note">
                derived in the browser
              </td>
            </tr>
          </tbody>
        </table>
      </Panel>

      <Panel title="Trend (one ranged read)">
        <div className="trend">
          {Array.from({ length: Trend.length }, (_, i) => (
            <TagValue key={i} name={Trend[i]} decimals={1} width={6} unit="" />
          ))}
        </div>
      </Panel>
    </div>
  );
}

export function Motion() {
  const position = useTag(Bench.Axis.Position).value ?? 0;
  const info = useTagInfo(Bench.Axis.Position);
  return (
    <div className="panels">
      <Panel title="Axis (fast group, only while this page is open)">
        <Row label="Position">
          <TagValue name={Bench.Axis.Position} decimals={1} />
        </Row>
        <Row label="Velocity">
          <TagValue name={Bench.Axis.Velocity} decimals={1} />
        </Row>
        <div className="bar" title="position, 0 to 1000 mm">
          <div style={{ width: `${Math.min(100, Math.max(0, position / 10))}%` }} />
        </div>
        <p className="note">{info ? `polled every ${info.rate_ms} ms (group ${info.group ?? "none"})` : " "}</p>
      </Panel>
      <Panel title="Spindle">
        <Row label="Running">
          <TagLamp name={Bench.Running} />
        </Row>
        <Row label="Speed">
          <TagValue name={Bench.Speed} decimals={1} />
        </Row>
      </Panel>
    </div>
  );
}

export function Browser() {
  return (
    <section className="panel wide">
      <h2>Tag browser</h2>
      <TagBrowser />
    </section>
  );
}
