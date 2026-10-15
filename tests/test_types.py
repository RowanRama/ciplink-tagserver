import json
from pathlib import Path
import shutil
import subprocess

import pytest
from conftest import wait_for

from ciplink_tagserver import cli

AOI = {"tag_type": "struct", "data_type_name": "MyAOI",
       "data_type": {"name": "MyAOI", "aoi": {"whole_read": False, "readable": ["Open", "Cmd"],
                                             "refused": ["Local1"]}}}
REAL = {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"}
CLIENT = Path(__file__).resolve().parents[1] / "client"

# a /tags body with every shape codegen handles
BODY = {
    "plc": "PLC1", "state": "connected", "version": 1234,
    "tags": [
        {"name": "Bench", "type": "BenchData"},
        {"name": "Tank", "type": "Tank", "dims": [4]},
        {"name": "Trend", "type": "REAL", "dims": [16]},
        {"name": "Grid", "type": "DINT", "dims": [3, 4]},
        {"name": "Flags", "type": "BOOL", "dims": [32]},
        {"name": "Message", "type": "STRING"},
        {"name": "Valve1", "type": "MyAOI", "aoi_readable": ["Open", "Cmd"]},
        {"name": "Pump-2", "type": "REAL"},
        {"name": "Program:Bench.Mode", "type": "DINT"},
        {"name": "Program:Bench.Cell", "type": "Tank", "dims": [2]},
        {"name": "Old", "type": "OldAOI", "aoi_readable": ["Done"]},
    ],
    "types": {
        "BenchData": {"members": {
            "Running": {"type": "BOOL"}, "StartPB": {"type": "BOOL"}, "SpeedSP": {"type": "REAL"},
            "Speed": {"type": "REAL"}, "StatusWord": {"type": "DINT"}, "Count": {"type": "DINT"},
            "Message": {"type": "STRING"}, "Axis": {"type": "Axis"}, "Recipe": {"type": "Recipe"},
            "length": {"type": "DINT"}}},
        "Axis": {"members": {"Position": {"type": "REAL"}, "Velocity": {"type": "REAL"}}},
        "Recipe": {"members": {"Name": {"type": "STRING"}, "Setpoints": {"type": "REAL", "dims": [4]}}},
        "Tank": {"members": {"Level": {"type": "REAL"}, "Temp": {"type": "REAL"}, "Valve": {"type": "BOOL"},
                             "Status": {"type": "INT"}}},
        "MyAOI": {"members": {"Open": {"type": "BOOL"}, "Cmd": {"type": "DINT"}}, "aoi": True},
    },
    "rules": [
        {"match": "Bench.SpeedSP", "writable": True, "min": 0, "max": 1800},
        {"match": ["Bench.StartPB", "Tank[*].Valve"], "writable": True},
        {"match": "Bench.StatusWord.1", "writable": True},
        {"match": "Bench.StatusWord", "always": True, "bits": {"EStop": 0, "DoorOpen": 1}},
        {"match": "Tank[*].Status", "bits": {"Alarm": 3}},
        {"match": "Grid*", "writable": True},
    ],
    "groups": [],
}

CHECKS = """\
import { useMomentary, useTag, useTags, useWriteTag } from "@ciplink/react";
import { PLC1, type PLC1_Tank } from "./plc";

export function checks(tank: PLC1_Tank) {
  useWriteTag(PLC1.Bench.SpeedSP)(5);
  useWriteTag(PLC1.Tank[2].Valve)(true);
  useWriteTag(PLC1.Grid[1][2])(3);
  useMomentary(PLC1.Bench.StartPB);
  // @ts-expect-error a read-only leaf
  useWriteTag(PLC1.Bench.Speed);
  // @ts-expect-error read-only through a shared interface
  useWriteTag(PLC1.Bench.Axis.Position);
  // @ts-expect-error not writable
  useWriteTag(PLC1.Tank[1].Level);
  // @ts-expect-error not writable
  useMomentary(PLC1.Bench.Running);
  // @ts-expect-error a REAL takes a number
  useWriteTag(PLC1.Bench.SpeedSP)("fast");
  // @ts-expect-error the word is read-only
  useWriteTag(PLC1.Bench.StatusWord);
  useWriteTag(PLC1.Bench.StatusWord.DoorOpen)(true);
  // @ts-expect-error EStop is read-only
  useWriteTag(PLC1.Bench.StatusWord.EStop);
  const estop: boolean | null = useTag(PLC1.Bench.StatusWord.EStop).value;
  const bit: boolean | null = useTag(PLC1.Bench.Count.bit(3)).value;
  const alarm: boolean | null = useTag(PLC1.Tank[0].Status.Alarm).value;
  const grid: number | null = useTag(PLC1.Grid[1][2]).value;
  const n: 4 = PLC1.Tank.length;
  const m: 16 = PLC1.Trend.length;
  const mode: number | null = useTag(PLC1["Program:Bench"].Mode).value;
  const cell: number | null = useTag(PLC1["Program:Bench"].Cell[1].Level).value;
  const text: string | null = useTag(PLC1.Message).value;
  const name: string | null = useTag(PLC1.Bench.Recipe.Name).value;
  const sp: number | null = useTag(PLC1.Bench.Recipe.Setpoints[3]).value;
  const flag: boolean | null = useTag(PLC1.Flags[31]).value;
  const open: boolean | null = useTag(PLC1.Valve1.Open).value;
  const pump: number | null = useTag(PLC1["Pump-2"]).value;
  useTag(PLC1.Old.Done);
  useTags([PLC1.Bench.Recipe, PLC1.Trend[0], tank.Level]);
  // @ts-expect-error a bare array name is its first element, not the array
  useTag(PLC1.Trend);
  // @ts-expect-error AOI members are not readable whole
  useTag(PLC1.Valve1);
  return [estop, bit, alarm, grid, n, m, mode, cell, text, name, sp, flag, open, pump];
}
"""


def serve_tags(client, monkeypatch):
    """Routes the CLI's HTTP fetch to the in-process test client."""
    def fetch(url, token, plc):
        response = client.get("/tags", params={"plc": plc} if plc else None,
                              headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json()
    monkeypatch.setattr(cli, "fetch_tags", fetch)


def connected(client, *plcs):
    wait_for(lambda: all(client.get("/health").json()["plcs"][p]["state"] == "connected" for p in plcs))


def test_types_one_plc(make, tmp_path, monkeypatch):
    def setup(fakes):
        fakes["PLC1"].tags = {"Line1.Speed": REAL, "Valve1": AOI}
    client, _, _ = make(setup=setup)
    connected(client, "PLC1")
    serve_tags(client, monkeypatch)
    out = tmp_path / "tags.d.ts"
    cli.main(["types", "--token", "view", "--plc", "PLC1", "--format", "names", "--out", str(out)])
    text = out.read_text()
    assert 'export type TagName =\n  | "Line1.Speed"\n  | "Valve1.Open"\n  | "Valve1.Cmd";\n' in text
    assert "Local1" not in text


def test_types_qualified_with_several_plcs(make, tmp_path, monkeypatch):
    def setup(fakes):
        fakes["PLC1"].tags = {"Line1.Speed": REAL}
        fakes["PLC2"].tags = {"Program:Station1.DoorOpen": REAL, "Valve1": AOI}
    client, _, _ = make("PLC1", "PLC2", setup=setup)
    connected(client, "PLC1", "PLC2")
    serve_tags(client, monkeypatch)
    out = tmp_path / "tags.d.ts"
    cli.main(["types", "--token", "view", "--plc", "PLC1", "--plc", "PLC2", "--format", "names", "--out", str(out)])
    assert out.read_text().endswith(
        '  | "PLC1/Line1.Speed"\n  | "PLC2/Program:Station1.DoorOpen"\n'
        '  | "PLC2/Valve1.Open"\n  | "PLC2/Valve1.Cmd";\n')


def test_tree_shapes():
    text = cli.tree_module([BODY])
    assert text.startswith("// Generated by ciplink-tagserver types (PLC1 tag table version 1234). Do not edit.\n")
    assert 'import { tagTree, type TagRef, type IntRef, type TagArray } from "@ciplink/react";' in text
    assert "export interface PLC1_Axis extends TagRef<Record<string, unknown>, false> {\n" \
           "  Position: TagRef<number, false>;\n  Velocity: TagRef<number, false>;\n}" in text
    assert "export interface PLC1_MyAOI {\n  Open: TagRef<boolean, false>;\n  Cmd: IntRef<false>;\n}" in text
    assert "  Setpoints: TagArray<TagRef<number, false>, 4>;\n" in text
    assert "length" not in text.split("export interface PLC1_BenchData")[1].split("}")[0]  # a proxy name
    tags = text.split("export interface PLC1Tags ")[1]
    assert "    SpeedSP: TagRef<number, true>;\n    Speed: TagRef<number, false>;\n" in tags
    assert "    StartPB: TagRef<boolean, true>;\n" in tags
    assert "    StatusWord: IntRef<false> & { EStop: TagRef<boolean, false>; DoorOpen: TagRef<boolean, true>; };\n" in tags
    assert "    Axis: PLC1_Axis;\n    Recipe: PLC1_Recipe;\n" in tags  # nothing writable below: shared interfaces
    assert "  Tank: TagArray<TagRef<Record<string, unknown>, false> & {\n" in tags  # Valve writable: inline
    assert "    Valve: TagRef<boolean, true>;\n    Status: IntRef<false> & { Alarm: TagRef<boolean, false>; };\n" in tags
    assert "  Grid: TagArray<TagArray<IntRef<true>, 4>, 3>;\n" in tags
    assert "  Flags: TagArray<TagRef<boolean, false>, 32>;\n" in tags
    assert "  Message: TagRef<string, false>;\n  Valve1: PLC1_MyAOI;\n" in tags
    assert '  "Pump-2": TagRef<number, false>;\n  "Program:Bench": {\n    Mode: IntRef<false>;\n' in tags
    assert "  Old: {\n    Done: TagRef<unknown, false>;\n  };\n" in tags  # AOI type not listed
    meta = json.loads(text.split("tagTree<PLC1Tags>(null, ")[1].rsplit(");", 1)[0])
    assert meta == {
        "dims": {"Bench.Recipe.Setpoints": [4], "Tank": [4], "Trend": [16], "Grid": [3, 4], "Flags": [32],
                 "Program:Bench.Cell": [2]},
        "bits": {"Bench.StatusWord": {"EStop": 0, "DoorOpen": 1}, "Tank[].Status": {"Alarm": 3}}}


def test_tree_qualified_with_several_plcs():
    text = cli.tree_module([BODY, {**BODY, "plc": "Line 2", "version": 7}], qualified=True)
    assert "PLC1 tag table version 1234, Line 2 tag table version 7" in text.splitlines()[0]
    assert 'export const PLC1 = tagTree<PLC1Tags>("PLC1", {' in text
    assert 'export const Line_2 = tagTree<Line_2Tags>("Line 2", {' in text
    assert "export interface Line_2_Axis " in text


@pytest.mark.skipif(not shutil.which("node") or not (CLIENT / "node_modules" / "typescript").exists(),
                    reason="needs node and the client's node_modules")
def test_tree_compiles(tmp_path):
    (tmp_path / "plc.ts").write_text(cli.tree_module([BODY]), encoding="utf-8")
    (tmp_path / "checks.ts").write_text(CHECKS, encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"target": "ES2020", "module": "ESNext", "moduleResolution": "Bundler", "strict": True,
                            "jsx": "react-jsx", "lib": ["ES2020", "DOM", "DOM.Iterable"], "skipLibCheck": True,
                            "noEmit": True, "noUnusedLocals": True, "types": [],
                            "paths": {"@ciplink/react": [str(CLIENT / "src" / "index.ts")]}},
        "files": ["plc.ts", "checks.ts"]}), encoding="utf-8")
    tsc = CLIENT / "node_modules" / "typescript" / "bin" / "tsc"
    result = subprocess.run(["node", str(tsc), "-p", str(tmp_path / "tsconfig.json")], capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_types_tree_from_mock_server(make, tmp_path, monkeypatch):
    tags = tmp_path / "mock_tags.json"
    tags.write_text(json.dumps({"types": {"Tank": {"Level": "REAL", "Valve": "BOOL"}},
                                "tags": {"Tank": "Tank[4]", "Trend": "REAL[16]", "Program:Bench.Mode": "DINT"}}))
    extra = (f'mock_tags = "{tags.as_posix()}"\n[[plc.PLC1.rule]]\nmatch = "Tank[*].Valve"\nwritable = true\n')
    client, _, _ = make(mock=True, extra=extra)
    connected(client, "PLC1")
    serve_tags(client, monkeypatch)
    out = tmp_path / "plc.ts"
    cli.main(["types", "--token", "view", "--out", str(out)])
    text = out.read_text()
    assert "export interface PLC1_Tank extends TagRef<Record<string, unknown>, false> {" in text
    assert "    Valve: TagRef<boolean, true>;\n" in text
    assert "  Trend: TagArray<TagRef<number, false>, 16>;\n" in text
    assert '  "Program:Bench": {\n    Mode: IntRef<false>;\n  };\n' in text
    assert 'export const PLC1 = tagTree<PLC1Tags>(null, {"dims": {"Tank": [4], "Trend": [16]}});' in text
