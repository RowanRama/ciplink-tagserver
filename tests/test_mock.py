import json
from types import SimpleNamespace

import pytest
from ciplink import CommError

from ciplink_tagserver import planner
from ciplink_tagserver.cli import main
from ciplink_tagserver.mock import MockDriver, make_factory

from conftest import hello, until

BENCH = {
    "types": {"BenchData": {"Running": "BOOL", "StartPB": "BOOL", "StopPB": "BOOL", "SpeedSP": "REAL",
                            "Speed": "REAL", "Axis": "Axis", "StatusWord": "DINT", "Count": "DINT",
                            "Message": "STRING", "Recipe": "Recipe"},
              "Axis": {"Position": "REAL", "Velocity": "REAL"},
              "Recipe": {"Name": "STRING", "Setpoints": "REAL[4]"},
              "Tank": {"Level": "REAL", "Temp": "REAL", "Valve": "BOOL"}},
    "tags": {"Tank": "Tank[4]", "Trend": "REAL[16]", "Bench": "BenchData", "Program:Bench.Mode": "DINT",
             "Small": "SINT", "Odd": "MyAOI"},
}


@pytest.fixture
def bench(tmp_path):
    path = tmp_path / "mock.json"
    path.write_text(json.dumps(BENCH))
    plc = MockDriver(SimpleNamespace(name="PLC1", mock_tags=str(path)))
    plc.state.clock = lambda: 1000.0
    return plc


def value(plc, name):
    result = plc.read(name)
    assert result.error is None, result
    return result.value


def test_flat_file(tmp_path):
    path = tmp_path / "mock.json"
    path.write_text(json.dumps({"Speed": "REAL", "Run": "BOOL", "Count": "DINT", "Recipe": "UDT_R",
                                "Line1.Level": "REAL"}))
    plc = MockDriver(SimpleNamespace(mock_tags=str(path)))
    now = [0.0]
    plc.state.clock = lambda: now[0]
    speeds, runs = set(), set()
    for i in range(40):
        now[0] = i * 0.7
        speeds.add(value(plc, "Speed"))
        runs.add(value(plc, "Run"))
        assert isinstance(value(plc, "Count"), int) and 0 <= value(plc, "Line1.Level") <= 100
    assert len(speeds) > 10 and runs == {True, False}
    missing = plc.read("Nope")
    assert missing.error.startswith("Tag doesn't exist") and missing.error_kind == "not_found"
    assert plc.read("Recipe").error_kind == "other"  # a type the file does not define
    assert plc.write("Count", 7).error is None and value(plc, "Count") == 7
    assert plc.tags["Speed"]["data_type_name"] == "REAL" and plc.data_types == {}


def test_tags_and_types_are_ciplink_shaped(bench):
    tank, bench_data, string = (bench.data_types[n] for n in ("Tank", "BenchData", "STRING"))
    assert bench.tags["Tank"]["dim"] == 1 and bench.tags["Tank"]["dimensions"] == [4, 0, 0]
    assert bench.tags["Tank"]["tag_type"] == "struct" and bench.tags["Tank"]["data_type"] is tank
    assert bench.tags["Trend"] == {"tag_name": "Trend", "dim": 1, "dimensions": [16, 0, 0], "alias": False,
                                   "tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"}
    assert tank["attributes"] == ["Level", "Temp", "Valve"] and tank["template"]["structure_size"] == 12
    assert bench_data["internal_tags"]["Recipe"]["data_type"]["internal_tags"]["Setpoints"]["array"] == 4
    assert string["string"] == 82 and string["template"]["structure_size"] == 88
    assert bench.tags["Program:Bench.Mode"]["data_type_name"] == "DINT" and bench.connection_size == 4000
    assert bench.get_tag_info("Tank[2].level")["data_type_name"] == "REAL"
    assert bench.get_tag_info("bench.recipe.setpoints[1]")["array"] == 4
    assert bench.get_tag_info("Program:Bench.Mode")["data_type_name"] == "DINT"
    for unknown in ("Nope", "Bench.Nope", "Bench.Count.3"):
        with pytest.raises(Exception):
            bench.get_tag_info(unknown)
    assert bench._tag_table_version == 1 and not hasattr(bench, "get_tag_descriptions")


def test_reads(bench):
    whole = value(bench, "Bench")
    assert whole["Axis"] == {"Position": value(bench, "Bench.Axis.Position"), "Velocity": value(bench, "bench.axis.velocity")}
    assert whole["Recipe"]["Setpoints"] == value(bench, "Bench.Recipe.Setpoints[0]{4}")
    assert whole["Recipe"]["Setpoints"][2] == value(bench, "Bench.Recipe.Setpoints[2]")
    assert isinstance(whole["Message"], str) and whole["Message"] == value(bench, "Bench.Message")
    trend = [value(bench, f"Trend[{i}]") for i in range(16)]
    assert value(bench, "Trend[3]{5}") == trend[3:8] and value(bench, "Trend{16}") == trend
    assert bench.read("Trend[3]{5}").type == "REAL[5]" and value(bench, "Trend") == trend[0]  # no index: first
    tanks = value(bench, "Tank{4}")
    assert [t["Level"] for t in tanks] == [value(bench, f"Tank[{i}].Level") for i in range(4)]
    assert all(0 <= t["Level"] <= 100 and 20 <= t["Temp"] <= 90 for t in tanks)
    word = value(bench, "Bench.StatusWord")
    assert [value(bench, f"Bench.StatusWord.{i}") for i in range(4)] == [bool(word >> i & 1) for i in range(4)]
    assert bench.read("Bench.StatusWord.0").type == "BOOL" and isinstance(value(bench, "Program:Bench.Mode"), int)
    assert -128 <= value(bench, "Small") <= 127
    for name, kind in [("Tank[4].Level", "not_found"), ("Trend[14]{3}", "not_found"), ("Bench.Nope", "not_found"),
                       ("Bench.Speed.1", "bad_request"), ("Bench.Count.32", "not_found"), ("Bench..X", "bad_request"),
                       ("Bench.Axis{2}", "bad_request"), ("Odd", "other")]:
        assert bench.read(name).error_kind == kind, name
    results = bench.read("Trend[1]", "Nope")
    assert [r.error_kind for r in results] == [None, "not_found"]


def test_values_follow_the_names_and_the_clock(bench):
    now = [1000.0]
    bench.state.clock = lambda: now[0]
    assert value(bench, "Bench.Running") is False and value(bench, "Bench.Speed") == 0
    count, position = value(bench, "Bench.Count"), value(bench, "Bench.Axis.Position")
    now[0] += 2
    assert value(bench, "Bench.Count") == count + 2 and value(bench, "Bench.Axis.Position") == pytest.approx(position + 50)
    assert bench.write("Bench.SpeedSP", 1200.0).error is None
    assert bench.write("Bench.StartPB", True).error is None
    assert value(bench, "Bench.Running") is True and value(bench, "Bench.Speed") == pytest.approx(1200, abs=0.2)
    assert bench.write("Bench.StartPB", False).error is None and value(bench, "Bench.Running") is True
    bench.write("Bench.StopPB", 1)
    assert value(bench, "Bench.Running") is False and value(bench, "Bench.Speed") == 0
    # deterministic: another driver of the same file at the same time reads the same
    other = MockDriver(SimpleNamespace(mock_tags=None), bench.state)
    assert other.read("Nope").error_kind == "not_found"
    assert [value(bench, "Tank[1].Temp")] * 2 == [value(bench, "Tank[1].Temp"), value(bench, "tank[1].temp")]


def test_writes_are_type_checked(bench):
    for name, bad in [("Bench.Running", "x"), ("Bench.Running", 2), ("Bench.Count", 1.5), ("Bench.Count", True),
                      ("Small", 300), ("Bench.SpeedSP", "a"), ("Bench.SpeedSP", True), ("Bench.Message", "x" * 83),
                      ("Bench.Message", 5), ("Trend[0]{2}", [1.0]), ("Bench.Recipe", {"Nope": 1}),
                      ("Bench.Recipe", {"Name": "ok", "Setpoints": [1.0, 2.0, 3.0, "x"]}), ("Bench.Axis", 3), ("Odd", 1),
                      ("Bench.Recipe", {"Name": "ok"}), ("Bench.Recipe", {"Name": "ok", "Setpoints": [1.0, 2.0]}),
                      ("Trend", [1.0, 2.0]), ("Bench.Recipe", {"name": "ok", "setpoints": [1.0, 2.0, 3.0, 4.0]}),
                      ("Trend[0]", 1e40)]:  # as ciplink: every member (case-sensitive), all of a member array, one
        # value by name, a REAL that fits a float32
        result = bench.write(name, bad)
        assert result.error_kind == "bad_value" and result.error, (name, bad)
    assert bench.state.written == {}  # nothing of a rejected write is kept
    assert str(bench.write("Bench.Running", 2).error) == "Invalid value for BOOL - 2"  # the value as sent
    assert bench.write("Nope", 1).error_kind == "not_found"
    assert bench.write("Bench.Running", 1).error is None and value(bench, "Bench.Running") is True
    assert bench.write("Bench.Message", "x" * 82).error is None and value(bench, "Bench.Message") == "x" * 82
    assert bench.write("Small", -5).error is None and value(bench, "Small") == -5
    assert bench.write("Bench.Recipe", {"Name": "Brass", "Setpoints": [1, 2.5, 3, 4]}).error is None
    assert value(bench, "Bench.Recipe")["Name"] == "Brass" and value(bench, "Bench.Recipe.Setpoints[0]{2}") == [1.0, 2.5]
    assert bench.write("Trend[2]{3}", [7.0, 8.0, 9.0]).error is None and value(bench, "Trend[1]{4}")[1:] == [7.0, 8.0, 9.0]


def test_bit_writes_read_modify_write(bench):
    bench.write("Bench.StatusWord", 0b1000)
    assert bench.write("Bench.StatusWord.1", True).error is None
    assert bench.write("Bench.StatusWord.31", 1).error is None
    assert value(bench, "Bench.StatusWord") == 0b1010 - 2**31
    assert bench.write("Bench.StatusWord.3", False).error is None and value(bench, "Bench.StatusWord") == 0b10 - 2**31
    assert bench.write("Bench.StatusWord.2", 5).error_kind == "bad_value"


def test_factory_shares_state_and_simulates_disconnects(tmp_path):
    path = tmp_path / "mock.json"
    path.write_text(json.dumps(BENCH))
    factory = make_factory()
    one, two = (factory(SimpleNamespace(name="PLC1", mock_tags=str(path))) for _ in range(2))
    other = factory(SimpleNamespace(name="PLC2", mock_tags=str(path)))
    one.write("Bench.Count", 5)
    assert value(two, "Bench.Count") == 5 and value(other, "Bench.Count") != 5
    factory.states["PLC1"].fail = True
    for call in (two.open, lambda: two.read("Bench.Count"), lambda: two.write("Bench.Count", 1)):
        with pytest.raises(CommError):
            call()
    assert other.open() and value(other, "Bench.Count") is not None
    factory.states["PLC1"].fail = False
    assert two.open() and value(two, "Bench.Count") == 5


def test_planner_runs_on_the_mock(bench):
    tags = ["Bench.Speed", "Bench.SpeedSP", "Bench.Running", "Bench.Axis.Position", "Bench.Axis.Velocity",
            "Trend[2]", "Trend[3]", "Trend[9]", "Tank[1].Level", "Tank[1].Temp", "Tank[2].Level",
            "Bench.Recipe.Setpoints[1]", "Bench.Recipe.Name", "Program:Bench.Mode", "Bench.StatusWord.2", "Nope"]
    reads = planner.plan(tags, bench.get_tag_info, 1500, budget=bench.connection_size)
    names = [name for name, _ in reads]
    assert "Trend[2]{8}" in names and "Bench" in names and len(reads) < len(tags) - 5
    out, failed, _ = planner.execute(bench, planner.chunks(reads, bench.get_tag_info, bench.connection_size))
    assert failed == []
    assert {t: (r.value, r.error) for t, r in out.items()} == {t: (r.value, r.error) for t, r in zip(tags, bench.read(*tags))}


def test_mock_mode_values_change(make, tmp_path):
    (tmp_path / "mock.json").write_text(json.dumps({"Speed": "REAL"}))
    client, _, _ = make(mock=True, extra='mock_tags = "mock.json"\n[[plc.PLC1.rule]]\nmatch = "Speed"\nwritable = true')
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"op": "sub", "id": 1, "tags": ["Speed"]})
        seen = set()
        while len(seen) < 3:
            seen.add(until(ws, lambda m: m["op"] == "values")["d"]["Speed"][0])
        ws.send_json({"op": "write", "id": 2, "tag": "Speed", "value": 99.0})
        assert until(ws, lambda m: m["op"] == "ack" and m["id"] == 2)["ok"]
    tags = client.get("/tags", headers={"Authorization": "Bearer view"}).json()["tags"]
    assert tags == [{"name": "Speed", "type": "REAL"}]


def test_dump_tags(tmp_path, monkeypatch):
    class Driver:
        tags = {"Speed": {"data_type_name": "REAL"}, "Valve1": {"data_type": {"name": "MyAOI"}}}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr("ciplink_tagserver.server.driver_factory", lambda cfg: Driver())
    config = tmp_path / "t.toml"
    config.write_text('[plc.A]\npath = "192.0.2.1"\n')
    out = tmp_path / "tags.json"
    main(["dump-tags", "--config", str(config), "--plc", "A", "--out", str(out)])
    assert json.loads(out.read_text()) == {"Speed": "REAL", "Valve1": "MyAOI"}
