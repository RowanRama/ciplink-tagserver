"""Decorators: [decorator.*] bundles attached by UDT member ([plc.P.udt.*]) or tag path ([plc.P.tags])."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import conftest
from ciplink_tagserver import cli
from ciplink_tagserver.config import load_config, parse_config
from ciplink_tagserver.rules import Resolver, Rules
from ciplink_tagserver.server import create_app

from conftest import hello, until, wait_for
from test_types import serve_tags

FIXTURES = Path(__file__).parent / "fixtures"
DECORATORS = {
    "temp": {"units": "C", "rate_ms": 1000},
    "fast": {"rate_ms": 250},
    "slow": {"rate_ms": 2000, "together": True},
    "sp": {"writable": True, "min": 0, "max": 100, "units": "rpm"},
    "cmd": {"writable": True},
    "alarm": {"always": True},
}
TAGS = [{"name": "Line", "type": "LineData"}, {"name": "Axes", "type": "Axis", "dims": [2]},
        {"name": "Zone", "type": "REAL", "dims": [8]}, {"name": "Program:Main.Axis", "type": "Axis"},
        {"name": "Word", "type": "DINT"}]
TYPES = {
    "LineData": {"members": {"Pump": {"type": "Pump"}, "Axes": {"type": "Axis", "dims": [2]}}},
    "Axis": {"members": {"Drive": {"type": "Drive"}, "Pos": {"type": "REAL"}, "Jog": {"type": "BOOL"}}},
    "Drive": {"members": {"Temp": {"type": "REAL"}, "Amps": {"type": "REAL"}, "Faults": {"type": "DINT", "dims": [4]}}},
    "Pump": {"members": {"SpeedSP": {"type": "REAL"}, "Speed": {"type": "REAL"}, "Alarm": {"type": "BOOL"}}},
}


def resolver(udt=None, tags=None, rules=(), groups=(), decorators=DECORATORS, types=True):
    r = Resolver(decorators, udt, tags, rules, groups)
    if types:
        r.set_types(TAGS, TYPES)
    return r


def test_entry_forms_list_order_and_inline_fields():
    r = resolver(tags={"A": "temp", "B": ["temp", "fast"], "C": ["fast", "temp"], "D": {"use": "sp", "max": 5},
                       "E": {"use": ["sp", "temp"], "units": "%", "rate_ms": 10}, "F": {"min": 1}})
    assert r.effective("A") == {"units": "C", "rate_ms": 1000, "group": "temp"}
    assert r.effective("B")["rate_ms"] == 250 and r.group("B") == {"name": "fast", "rate_ms": 250}  # later wins
    assert r.effective("C")["rate_ms"] == 1000
    assert r.effective("D") == {"writable": True, "min": 0, "max": 5, "units": "rpm"}  # inline wins over use
    assert r.effective("e")["units"] == "%" and r.group("E") == {"name": "tag", "rate_ms": 10}
    assert r.effective("F") == {"min": 1}


def test_precedence_at_a_node():
    r = resolver(udt={"Axis": {"Pos": {"units": "udt", "min": 1, "max": 9}}},
                 tags={"Axes.Pos": {"units": "stripped", "min": 2}, "Axes[1].Pos": {"units": "specific"}})
    assert r.effective("Axes[1].Pos") == {"units": "specific", "min": 2, "max": 9}
    assert r.effective("Axes[0].Pos") == {"units": "stripped", "min": 2, "max": 9}
    assert r.effective("Program:Main.Axis.Pos") == {"units": "udt", "min": 1, "max": 9}  # no [tags] key
    assert r.effective("AXES[01].pos")["units"] == "specific"  # case and index spelling


def test_inherited_fields_closest_wins():
    r = resolver(udt={"Axis": {"Drive": "fast"}, "Drive": {"Temp": "temp"}, "LineData": {"Pump": "slow"}},
                 tags={"Line": {"rate_ms": 50, "together": False}})
    assert r.effective("Axes[1].Drive.Amps") == {"rate_ms": 250, "group": "fast"}
    assert r.effective("Axes[1].Drive.Faults[2]") == {"rate_ms": 250, "group": "fast"}
    assert r.effective("Axes[0].Drive.Temp") == {"rate_ms": 1000, "group": "temp", "units": "C"}
    assert r.effective("Line.Pump.Speed") == {"rate_ms": 2000, "group": "slow", "together": True}
    assert r.effective("Line.Axes[0].Pos") == {"rate_ms": 50, "group": "tag", "together": False}
    assert r.effective("Line.Axes[0].Drive.Amps") == {"rate_ms": 250, "group": "fast", "together": False}


def test_value_fields_do_not_inherit_into_members():
    r = resolver(tags={"Line": ["sp", "alarm"], "Line.Pump": {"description": "pump", "bits": {"X": 1}}})
    assert r.effective("Line")["writable"] is True
    assert r.effective("Line.Pump") == {"description": "pump", "bits": {"X": 1}}
    assert r.effective("Line.Pump.SpeedSP") == {}


def test_array_elements_take_their_arrays_value_fields():
    r = resolver(tags={"Zone": "temp", "Zone[2]": {"units": "K"}, "Axes": "sp"})
    assert r.effective("Zone[3]") == {"units": "C", "rate_ms": 1000, "group": "temp"}
    assert r.effective("Zone[2]")["units"] == "K"
    assert r.effective("Axes[1]")["writable"] is True
    assert "writable" not in r.effective("Axes[1].Pos")


def test_nested_udts_and_program_scope():
    r = resolver(udt={"Drive": {"Temp": "temp"}, "Pump": {"SpeedSP": "sp"}},
                 tags={"Program:Main.Axis.Jog": "cmd"})
    for path in ("Axes[0].Drive.Temp", "Line.Axes[1].Drive.Temp", "Program:Main.Axis.Drive.Temp"):
        assert r.effective(path)["units"] == "C"
    assert r.effective("Line.Pump.SpeedSP")["writable"] is True
    assert r.effective("program:main.axis.jog") == {"writable": True}
    assert r.effective("Program:Main.Axis.Pos") == {}


def test_bits():
    r = resolver(tags={"Word": {"bits": {"EStop": 0}, "writable": True, "units": "x", "rate_ms": 30},
                       "Word.3": {"writable": False}})
    rules = Rules(parse_config({"decorator": DECORATORS, "plc": {"P": {"path": "x", "tags": {
        "Word": {"bits": {"EStop": 0}}, "Program:Main.Axis.Pos": {"bits": {"Home": 4}}}}}}))
    assert r.effective("Word.0") == {"writable": True, "rate_ms": 30, "group": "tag"}
    assert r.effective("Word.3") == {"writable": False, "rate_ms": 30, "group": "tag"}
    assert rules.resolve("P", "Word.estop") == "Word.0"
    assert rules.resolve("P", "Program:Main.Axis.Pos.Home") == "Program:Main.Axis.Pos.4"


def test_rules_are_the_fallback_and_groups_need_no_decorator_rate():
    rules = [{"match": "Axes[*].*", "units": "rule", "max": 7, "description": "r"}]
    groups = [{"name": "g", "rate_ms": 40, "match": "Axes[*].*"}]
    r = resolver(udt={"Axis": {"Pos": {"units": "m"}, "Drive": "fast"}}, rules=rules, groups=groups)
    assert r.effective("Axes[0].Pos") == {"units": "m", "max": 7, "description": "r"}
    assert r.group("Axes[0].Pos") == groups[0]
    assert r.group("Axes[0].Drive") == {"name": "fast", "rate_ms": 250}


def test_types_unknown_before_connect_and_memo_cleared():
    config = parse_config({"decorator": DECORATORS, "plc": {"P": {"path": "x", "udt": {"Axis": {"Pos": "sp"}},
                                                                    "tags": {"Zone": "temp"}}}})
    rules = Rules(config)
    assert rules.effective("P", "Axes[0].Pos") == {}  # UDT entries need the types
    assert rules.effective("P", "Zone[1]")["units"] == "C"  # [tags] entries do not
    rules.set_types("P", TAGS, TYPES)
    assert rules.effective("P", "Axes[0].Pos")["writable"] is True
    tags = [{**t, "type": "Axis2"} if t["name"] == "Axes" else t for t in TAGS]  # a download changed its type
    rules.set_types("P", tags, {**TYPES, "Axis2": TYPES["Axis"]})
    assert rules.effective("P", "Axes[0].Pos") == {}


def test_always_literal_keys_and_udt_instances():
    r = resolver(udt={"Pump": {"Alarm": "alarm"}, "Axis": {"Jog": "alarm"}},
                 tags={"Word": "alarm", "Line.Axes[1].Jog": {"always": False}},
                 rules=[{"match": "Zone[2]", "always": True}])
    assert r.always() == ["Zone[2]", "Word", "Line.Pump.Alarm", "Line.Axes[0].Jog", "Axes[0].Jog", "Axes[1].Jog",
                          "Program:Main.Axis.Jog"]  # not Line.Axes[1].Jog: its [tags] entry wins
    assert resolver(udt={"Pump": {"Alarm": "alarm"}}, types=False).always() == []


def test_always_index_less_key_takes_every_element():
    tags = {"Axes.Jog": "alarm", "Line.Axes.Drive.Faults": "alarm", "Zone": "alarm", "Axes[1].Pos": "alarm"}
    assert resolver(tags=tags).always() == ["Axes[0].Jog", "Axes[1].Jog", "Line.Axes[0].Drive.Faults",
                                            "Line.Axes[1].Drive.Faults", "Zone", "Axes[1].Pos"]
    assert resolver(tags={"Axes.Jog": "alarm"}, types=False).always() == ["Axes.Jog"]  # until the types are known


@pytest.mark.parametrize("plc,error", [
    ({"udt": {"Axis": {"Pos": "nope"}}}, "plc.P.udt.Axis.Pos: unknown decorator 'nope'"),
    ({"tags": {"Zone": {"use": ["temp", "nope"]}}}, "plc.P.tags.Zone: unknown decorator 'nope'"),
    ({"tags": {"Zone": 3}}, "plc.P.tags.Zone: expected a decorator name"),
    ({"tags": {"Zone": {"rate_ms": 0}}}, "plc.P.tags.Zone: rate_ms"),
])
def test_config_errors(plc, error):
    with pytest.raises(ValueError, match=error.replace(".", r"\.").replace("[", r"\[")):
        parse_config({"decorator": DECORATORS, "plc": {"P": {"path": "x", **plc}}})


def test_unknown_decorator_names_the_file_and_unknown_keys_warn(tmp_path, caplog):
    path = tmp_path / "t.toml"
    path.write_text('[decorator.d]\nunit = "x"\nmin = 1\n[plc.P]\npath = "x"\n[plc.P.tags]\nA = "d"\nB = "e"\n')
    with pytest.raises(ValueError, match=r"t\.toml: plc\.P\.tags\.B: unknown decorator 'e'"):
        load_config(path)
    assert "decorator.d: unknown key 'unit' ignored" in caplog.text
    path.write_text('[decorator.d]\nunit = "x"\nmin = 1\n[plc.P]\npath = "x"\n[plc.P.tags]\nA = "d"\n')
    assert Rules(load_config(path)).effective("P", "A") == {"min": 1}


def test_codegen_uses_the_same_resolution():
    body = {"plc": "PLC1", "version": 1, "tags": TAGS, "types": TYPES, "rules": [], "groups": [],
            "decorators": DECORATORS, "udt": {"Axis": {"Jog": "cmd"}},
            "tag_settings": {"Word": {"bits": {"EStop": 0}, "use": "cmd"}, "Word.0": {"writable": False}}}
    tags = cli.tree_module([body]).split("export interface PLC1Tags ")[1]
    assert "      Jog: TagRef<boolean, true>;\n" in tags
    assert "    Pos: TagRef<number, false>;\n" in tags
    assert "  Word: IntRef<true> & { EStop: TagRef<boolean, false>; };\n" in tags


# through the server

MOCK_TAGS = {"types": {"Pump": {"SpeedSP": "REAL", "Running": "BOOL", "Temp": "REAL", "Alarm": "BOOL"}},
             "tags": {"SpeedPumps": "Pump[2]", "SpeedLimit": "REAL"}}
EXTRA = """
[plc.PLC1.udt.Pump]
SpeedSP = { use = "sp", max = 50 }
Temp = "warm"
Alarm = "alarm"

[decorator.sp]
writable = true
min = 0
max = 100
units = "rpm"

[decorator.warm]
units = "C"
rate_ms = 30

[decorator.alarm]
always = true
"""


def mock_server(make, tmp_path):
    path = tmp_path / "mock_tags.json"
    path.write_text(json.dumps(MOCK_TAGS))
    client, _, config = make(mock=True, extra=f'mock_tags = "{path.as_posix()}"\n{EXTRA}')
    wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")
    return client


def test_ws_writes_info_rate_and_always(make, tmp_path):
    client = mock_server(make, tmp_path)
    worker = client.app.state.hub.workers["PLC1"]
    wait_for(lambda: worker.tiers.get("SpeedPumps[1].Alarm") == 1)  # always via udt, added on connect
    assert worker.tiers.get("SpeedPumps[0].Alarm") == 1
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["SpeedPumps[1].SpeedSP", "SpeedPumps[0].Temp"]})
        info = until(ws, lambda m: m["op"] == "info")["d"]
        assert info["SpeedPumps[1].SpeedSP"] == {"type": "REAL", "writable": True, "rate_ms": 20, "group": None,
                                                 "min": 0, "max": 50, "units": "rpm"}
        assert info["SpeedPumps[0].Temp"] == {"type": "REAL", "writable": False, "rate_ms": 30, "group": "warm",
                                              "units": "C"}
        acks = []
        for i, (tag, value) in enumerate([("SpeedPumps[1].SpeedSP", 40.0), ("SpeedPumps[1].SpeedSP", 60.0),
                                          ("SpeedPumps[0].Running", True), ("SpeedLimit", 1.0)]):
            ws.send_json({"op": "write", "id": 10 + i, "tag": tag, "value": value})
            acks.append(until(ws, lambda m: m["op"] == "ack" and m["id"] == 10 + i))
        assert [(a["ok"], a["kind"]) for a in acks] == [(True, None), (False, "out_of_range"),
                                                         (False, "not_writable"), (False, "not_writable")]
        assert acks[0]["value"] == 40.0
        stats = lambda: client.get("/stats").json()["plcs"]["PLC1"]["groups"].get("warm")
        wait_for(lambda: stats() is not None and stats()["last_period_ms"] is not None)
        assert stats()["rate_ms"] == 30 and stats()["tags"] == 1


def test_tags_payload_and_codegen_from_the_server(make, tmp_path, monkeypatch):
    client = mock_server(make, tmp_path)
    body = client.get("/tags", headers={"Authorization": "Bearer view"}).json()
    assert body["udt"]["Pump"]["Temp"] == "warm" and body["decorators"]["warm"] == {"units": "C", "rate_ms": 30}
    assert body["tag_settings"] == {}
    serve_tags(client, monkeypatch)
    out = tmp_path / "plc.ts"
    cli.main(["types", "--token", "view", "--out", str(out)])
    text = out.read_text()
    assert "    SpeedSP: TagRef<number, true>;\n    Running: TagRef<boolean, false>;\n" in text


def test_reload_clears_the_memo_and_updates_always(make, tmp_path, monkeypatch):
    stored = {}

    def setup(fakes):
        fakes["PLC1"].tags = {"Tanks": conftest.tag_def(conftest.udt("Tank", 8, {"Level": "REAL"}), dim=1, dims=[2])}
        fakes["PLC1"].values["Tanks"] = [{"Level": 1.0}, {"Level": 2.0}]
    monkeypatch.setattr(conftest.FakeDriver, "data_types", property(lambda self: stored), raising=False)
    stored["Tank"] = conftest.udt("Tank", 8, {"Level": "REAL"})
    extra = '[decorator.watch]\nalways = true\nunits = "%"\n[plc.PLC1.udt.Tank]\nLevel = "watch"\n'
    client, fakes, _ = make(extra=extra, setup=setup)
    hub, worker = client.app.state.hub, client.app.state.hub.workers["PLC1"]
    wait_for(lambda: worker.tiers.get("Tanks[1].Level") == 1)
    assert hub.rules.effective("PLC1", "Tanks[0].Level")["units"] == "%"
    stored["Tank2"] = conftest.udt("Tank2", 8, {"Level": "REAL"})  # a download changed the type
    fakes["PLC1"].tags = {"Tanks": conftest.tag_def(stored["Tank2"], dim=1, dims=[2])}
    fakes["PLC1"].version = 2
    wait_for(lambda: "Tanks[1].Level" not in worker.tiers)
    assert hub.rules.effective("PLC1", "Tanks[0].Level") == {}


def test_udt_bit_alias_subscribed_before_connect_resolves_on_connect(make):
    def setup(fakes):
        fakes["PLC1"].fail = True  # the HMI is up before the PLC
        fakes["PLC1"].tags = {"Ax": conftest.tag_def(conftest.udt("Axis", 8, {"St": "DINT"}))}
        fakes["PLC1"].values["Ax"] = {"St": 8}
    extra = '[decorator.stbits]\nbits = { Ready = 3 }\n[plc.PLC1.udt.Axis]\nSt = "stbits"\n'
    client, fakes, _ = make(extra=extra, setup=setup)
    hub, worker = client.app.state.hub, client.app.state.hub.workers["PLC1"]
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Ax.St.Ready"]})
        assert until(ws, lambda m: m["op"] == "info")["d"]["Ax.St.Ready"]["type"] is None
        fakes["PLC1"].fail = False
        info = until(ws, lambda m: m["op"] == "info" and m["d"].get("Ax.St.Ready", {}).get("type"))
        assert info["d"]["Ax.St.Ready"]["type"] == "BOOL"
        assert until(ws, lambda m: m["op"] == "values" and "Ax.St.Ready" in m["d"])["d"]["Ax.St.Ready"][0] is True
        assert "Ax.St.Ready" not in worker.tiers and worker.tiers["Ax.St.3"] == 1
        assert set(hub.refs) == {"PLC1/Ax.St.3"}


# the example config, under generic names

def test_example_config_in_mock_mode(tmp_path, caplog):
    config = load_config(FIXTURES / "decorators_example.toml")
    config.audit_log, config.pages_file = str(tmp_path / "audit.log"), None
    with TestClient(create_app(config, mock=True)) as client:
        wait_for(lambda: client.get("/health").json()["plcs"]["PLC1"]["state"] == "connected")
        hub = client.app.state.hub
        eff = lambda tag: hub.rules.effective("PLC1", tag)
        rate = lambda tag: (hub.rules.group("PLC1", tag) or {"rate_ms": config.cycle_ms})["rate_ms"]
        assert eff("Spindle.RPM")["units"] == "rpm" and eff("Spindle.RPM")["deadband"] == 0.5
        assert rate("Spindle.RPM") == 100
        sp = eff("Spindle.SpeedSP")
        assert (sp["writable"], sp["min"], sp["max"], sp["units"]) == (True, 0, 3000, "rpm")
        assert sp["description"] == "Spindle speed setpoint"
        assert rate("Spindle.Running") == 250 and not eff("Spindle.Running").get("writable")
        assert eff("Axes[1].Drive.Motor.Temp")["units"] == "°C" and rate("Axes[1].Drive.Motor.Temp") == 1000
        assert eff("Axes[0].Drive.Motor.Current")["units"] == "A" and rate("Axes[0].Drive.Motor.Current") == 250
        assert rate("Axes[0].Drive.Faults[2]") == 250
        assert eff("Axes[1].JogCmd")["writable"] is True
        assert eff("ZoneTemp[3]")["units"] == "°C" and rate("ZoneTemp[3]") == 1000
        assert rate("RecipeName") == 2000 and hub.rules.group("PLC1", "RecipeName")["name"] == "tag"
        assert hub.rules.resolve("PLC1", "Program:Main.StatusWord.EStop") == "Program:Main.StatusWord.0"
        assert eff("Program:Main.CmdWord.0")["writable"] is True
        assert rate("Grid[3]") == 500 and eff("Grid[3]") == {}
        with client.websocket_connect("/ws") as ws:
            hello(ws, "hmi-secret")
            ws.send_json({"op": "sub", "id": 1, "tags": ["Spindle.SpeedSP", "Program:Main.StatusWord.EStop"]})
            info = until(ws, lambda m: m["op"] == "info")["d"]
            assert info["Spindle.SpeedSP"]["writable"] is True and info["Spindle.SpeedSP"]["group"] == "fast"
            assert info["Program:Main.StatusWord.EStop"]["type"] == "BOOL"
            ws.send_json({"op": "momentary", "id": 2, "tag": "Axes[1].JogCmd", "action": "press"})
            assert until(ws, lambda m: m["op"] == "ack" and m["id"] == 2)["ok"] is True
            ws.send_json({"op": "momentary", "id": 3, "tag": "Axes[1].JogCmd", "action": "release"})
            assert until(ws, lambda m: m["op"] == "ack" and m["id"] == 3)["ok"] is True
            ws.send_json({"op": "write", "id": 4, "tag": "Program:Main.CmdWord.0", "value": True})
            assert until(ws, lambda m: m["op"] == "ack" and m["id"] == 4)["ok"] is True
            ws.send_json({"op": "write", "id": 5, "tag": "Spindle.SpeedSP", "value": 3500.0})
            assert until(ws, lambda m: m["op"] == "ack" and m["id"] == 5)["kind"] == "out_of_range"
    assert "unknown key" not in caplog.text
