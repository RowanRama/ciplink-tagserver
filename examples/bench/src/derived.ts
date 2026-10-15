// Values computed in the browser from other tags. They never reach the server.
import { derive } from "@ciplink/react";
import { PLC1 } from "./plc";

const { Bench, Tank } = PLC1;

/** The average level of the four tanks. */
export const avgLevel = derive((get) => {
  let sum = 0;
  for (let i = 0; i < Tank.length; i++) sum += get(Tank[i].Level) ?? 0;
  return sum / Tank.length;
}, "avgLevel");

/** Running and within 1 rpm of the setpoint. */
export const atSpeed = derive(
  (get) => !!get(Bench.Running) && Math.abs((get(Bench.Speed) ?? 0) - (get(Bench.SpeedSP) ?? 0)) < 1,
  "atSpeed",
);
