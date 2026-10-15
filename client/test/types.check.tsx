/** Compile-time tests, checked by `tsc --noEmit` (never run): a read-only ref must not be writable. */
import {
  MomentaryButton,
  NumericInput,
  derive,
  tagTree,
  useMomentary,
  useTag,
  useWriteTag,
  type DerivedRef,
  type IntRef,
  type TagArray,
  type TagRef,
  type TagState,
} from "../src/index.js";

interface Tank {
  Level: TagRef<number, false>;
  Valve: TagRef<boolean, true>;
}
interface Tags {
  SpeedSP: TagRef<number, true>;
  Speed: TagRef<number, false>;
  Message: TagRef<string, true>;
  StatusWord: IntRef<false> & { EStop: TagRef<boolean, false> };
  Control: IntRef<true>;
  Tank: TagArray<Tank, 4>;
}
const PLC1 = tagTree<Tags>(null);

export function Checks() {
  // writable refs and plain strings compile
  const setSpeed = useWriteTag(PLC1.SpeedSP);
  void setSpeed(1200);
  // @ts-expect-error the value type follows the ref
  void setSpeed("fast");
  useWriteTag("Anything.Goes")("x");
  useWriteTag(PLC1.Tank[1].Valve);
  useMomentary(PLC1.Control.bit(3));
  useMomentary(PLC1.Tank[0].Valve);

  // read-only refs do not
  // @ts-expect-error not writable
  useWriteTag(PLC1.Speed);
  // @ts-expect-error not writable
  useWriteTag(PLC1.Tank[2].Level);
  // @ts-expect-error not writable
  useWriteTag(PLC1.StatusWord.bit(0));
  // @ts-expect-error not writable
  useMomentary(PLC1.StatusWord.EStop);
  // @ts-expect-error not a BOOL
  useMomentary(PLC1.SpeedSP);

  const avg: DerivedRef<number> = derive((get) => ((get(PLC1.Tank[0].Level) ?? 0) + (get(PLC1.Tank[1].Level) ?? 0)) / 2);
  // @ts-expect-error derived values are never writable
  useWriteTag(avg);

  // reads are typed by the ref
  const s: TagState<number> = useTag(PLC1.Speed);
  const b: boolean | null = useTag(PLC1.StatusWord.EStop).value;
  const a: number | null = useTag(avg).value;
  // @ts-expect-error a number ref's value is not a string
  const wrong: string | null = useTag(PLC1.Speed).value;
  const n: 4 = PLC1.Tank.length;
  void [s, b, a, wrong, n];

  return (
    <>
      <NumericInput name={PLC1.SpeedSP} />
      <NumericInput name="Bench.SpeedSP" />
      {/* @ts-expect-error not writable */}
      <NumericInput name={PLC1.Speed} />
      {/* @ts-expect-error not a number */}
      <NumericInput name={PLC1.Message} />
      {/* @ts-expect-error derived values are never writable */}
      <NumericInput name={avg} />
      <MomentaryButton name={PLC1.Control.bit(0)}>Start</MomentaryButton>
      {/* @ts-expect-error not writable */}
      <MomentaryButton name={PLC1.StatusWord.bit(0)}>Start</MomentaryButton>
    </>
  );
}
