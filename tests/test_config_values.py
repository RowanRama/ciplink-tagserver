import pytest
from ciplink import Tag

from ciplink_tagserver.config import load_config, parse_config
from ciplink_tagserver.values import jsonable, quality, tag_list, to_value


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


def test_defaults():
    config = parse_config({"plc": {"A": {"path": "1.2.3.4"}}})
    assert (config.host, config.port, config.cycle_ms) == ("127.0.0.1", 8765, 250)
    assert config.tokens == {}


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


def test_quality_mapping():
    assert quality(None) == "good"
    assert quality("Tag doesn't exist - Foo") == "missing"
    assert quality("AOI local tag, not externally accessible") == "missing"
    assert quality("Service not supported") == "bad"
    assert to_value(Tag("x", 5, "DINT", None)) == (5, "good")
    assert to_value(Tag("x", 5, "DINT", "General failure")) == (None, "bad")


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
