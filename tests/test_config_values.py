import pytest
from ciplink import ErrorKind, Tag, TagError

from ciplink_tagserver.config import load_config, parse_config
from ciplink_tagserver.values import error_of, jsonable, merge, quality, tag_list, to_value, type_map


def test_load_config(tmp_path):
    path = tmp_path / "tagserver.toml"
    path.write_text("""
[server]
host = "0.0.0.0"
port = 9000
cycle_ms = 100

[plc.PLC1]
path = "10.0.0.1"
tag_cache_file = "cache/PLC1.json"

[plc.Other]
path = "10.0.0.2/1/3"

[[plc.PLC1.rule]]
match = "Bench.SpeedSP"
writable = true
max = 1800

[[plc.PLC1.rule]]
match = ["Bench.Speed", "Bench.Rpm"]
deadband = 0.5

[[plc.PLC1.group]]
name = "fast"
rate_ms = 50
match = ["Bench.Axis.*"]

[token.hmi1]
secret = "change-me"
write = ["PLC1/Line1.*.SpeedSP"]

[token.viewer]
secret = "v"
""")
    config = load_config(path)
    assert (config.host, config.port, config.cycle_ms) == ("0.0.0.0", 9000, 100)
    assert config.audit_log == str(tmp_path / "audit.log")
    assert config.plcs["PLC1"].path == "10.0.0.1"
    assert config.plcs["PLC1"].tag_cache_file == str(tmp_path / "cache/PLC1.json")
    assert config.plcs["Other"].tag_cache_file is None
    assert config.tokens["hmi1"].write == ["PLC1/Line1.*.SpeedSP"]
    assert config.tokens["viewer"].write == []
    assert config.plcs["PLC1"].rules == [{"match": "Bench.SpeedSP", "writable": True, "max": 1800},
                                         {"match": ["Bench.Speed", "Bench.Rpm"], "deadband": 0.5}]
    assert config.plcs["PLC1"].groups == [{"name": "fast", "rate_ms": 50, "match": ["Bench.Axis.*"]}]
    assert config.plcs["Other"].rules == [] and config.plcs["Other"].groups == []
    assert config.momentary_timeout_ms == 1000


@pytest.mark.parametrize("group", [{"name": "g", "rate_ms": 0}, {"name": "g", "rate_ms": 1.5}, {"rate_ms": 10}])
def test_bad_group(group):
    with pytest.raises(ValueError, match="rate_ms"):
        parse_config({"plc": {"A": {"path": "x", "group": [group]}}})


def test_defaults():
    config = parse_config({"plc": {"A": {"path": "1.2.3.4"}}})
    assert (config.host, config.port, config.cycle_ms) == ("127.0.0.1", 8765, 250)
    assert config.tokens == {}
    assert parse_config({"server": {"momentary_timeout_ms": 300}}).momentary_timeout_ms == 300


def test_qualify_one_plc():
    config = parse_config({"plc": {"A": {"path": "x"}}})
    assert config.qualify("Speed") == ("A", "Speed")
    assert config.qualify("A/Program:Main.Speed") == ("A", "Program:Main.Speed")
    with pytest.raises(ValueError):
        config.qualify("B/Speed")


def test_qualify_several_plcs():
    config = parse_config({"plc": {"A": {"path": "x"}, "B": {"path": "y"}}})
    assert config.qualify("B/Speed") == ("B", "Speed")
    with pytest.raises(ValueError, match="qualified"):
        config.qualify("Speed")
    with pytest.raises(ValueError):
        config.qualify("A/")


def result(error=None, value=5):
    return Tag("x", value, "DINT", error)


@pytest.mark.parametrize("kind,expected", [
    (None, "good"), (ErrorKind.NOT_FOUND, "missing"), (ErrorKind.ACCESS, "missing"), (ErrorKind.BAD_VALUE, "bad"),
    (ErrorKind.REFUSED, "bad"), (ErrorKind.TABLE_CHANGED, "bad"), (ErrorKind.COMM, "bad"), (ErrorKind.OTHER, "bad"),
])
def test_quality_from_error_kinds(kind, expected):
    assert quality(result(None if kind is None else TagError("boom", kind))) == expected


def test_quality_never_reads_message_text():
    assert quality(result("Tag doesn't exist - Foo")) == "bad"  # a plain string error is kind other
    assert error_of(result("General failure")) == {"kind": "other", "message": "General failure"}


def test_to_value_and_merge():
    missing = result(TagError("Tag doesn't exist - x", ErrorKind.NOT_FOUND))
    bad = result(TagError("Service not supported", ErrorKind.DEVICE))
    assert to_value(result()) == (5, "good", None)
    assert to_value(bad) == (None, "bad", {"kind": "device", "message": "Service not supported"})
    assert merge(None, result(value=7), 10) == [7, "good", 10, None]
    assert merge([7, "good", 10, None], bad, 11) == [7, "bad", 11, {"kind": "device", "message": "Service not supported"}]
    assert merge(None, bad, 11)[:2] == [None, "bad"]
    assert merge([7, "good", 10, None], missing, 12) == [None, "missing", 12,
                                                          {"kind": "not_found", "message": "Tag doesn't exist - x"}]


def test_jsonable():
    value = {"A": (1, 2), "B": b"\x01\xff", "C": {"D": float("nan")}, "E": float("-inf"), "F": True}
    assert jsonable(value) == {"A": [1, 2], "B": "01ff", "C": {"D": "NaN"}, "E": "-Infinity", "F": True}


def test_tag_list_with_aoi():
    class Plc:
        tags = {
            "Speed": {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL"},
            "Valve1": {"tag_type": "struct", "data_type_name": "MyAOI",
                       "data_type": {"name": "MyAOI", "aoi": {"whole_read": False, "readable": ["Open", "Cmd"],
                                                             "refused": ["Local1"]}}},
            "Recipe": {"tag_type": "struct", "data_type_name": "UDT_R", "data_type": {"name": "UDT_R"}},
        }

    assert tag_list(Plc()) == [
        {"name": "Speed", "type": "REAL"},
        {"name": "Valve1", "type": "MyAOI", "aoi_readable": ["Open", "Cmd"]},
        {"name": "Recipe", "type": "UDT_R"},
    ]


STRING = {"name": "STRING", "string": 82, "attributes": ["LEN", "DATA"],
          "internal_tags": {"LEN": {"data_type_name": "DINT"}, "DATA": {"data_type_name": "SINT", "array": 82}}}
TANK = {"name": "Tank", "attributes": ["Level", "Name", "Hist"], "internal_tags": {
    "ZZZZZZZZZZTank0": {"data_type_name": "SINT"},
    "Level": {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL", "array": 0},
    "Name": {"tag_type": "struct", "data_type": STRING, "data_type_name": "STRING", "array": 0},
    "Hist": {"tag_type": "atomic", "data_type": "REAL", "data_type_name": "REAL", "array": 8}}}
OLD = {"name": "Old", "internal_tags": {"__pad": {"data_type_name": "SINT"}, "A": {"data_type_name": "DINT"}}}
AOI = {"name": "Valve_AOI", "internal_tags": {"Open": {"data_type_name": "BOOL"}, "Cmd": {"data_type_name": "BOOL"}},
       "attributes": ["Open", "Cmd"], "aoi": {"readable": ["Open"], "refused": ["Cmd"]}}


def test_tag_list_dims_and_strings_and_type_map():
    class Plc:
        tags = {
            "Tanks": {"tag_type": "struct", "data_type": TANK, "data_type_name": "Tank", "dim": 1,
                      "dimensions": [4, 0, 0]},
            "Grid": {"tag_type": "atomic", "data_type": "DINT", "data_type_name": "DINT", "dim": 2,
                     "dimensions": [3, 5, 0]},
            "Msg": {"tag_type": "struct", "data_type": dict(STRING, name="STR20"), "data_type_name": "STR20", "dim": 0},
        }
        data_types = {"STRING": STRING, "Tank": TANK, "Old": OLD, "Valve_AOI": AOI}

    assert tag_list(Plc()) == [{"name": "Tanks", "type": "Tank", "dims": [4]},
                               {"name": "Grid", "type": "DINT", "dims": [3, 5]},
                               {"name": "Msg", "type": "STRING"}]
    assert type_map(Plc()) == {
        "Tank": {"members": {"Level": {"type": "REAL"}, "Name": {"type": "STRING"},
                             "Hist": {"type": "REAL", "dims": [8]}}},
        "Old": {"members": {"A": {"type": "DINT"}}},
        "Valve_AOI": {"members": {"Open": {"type": "BOOL"}}, "aoi": True},
    }
