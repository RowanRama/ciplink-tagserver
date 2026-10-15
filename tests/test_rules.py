import pytest

from ciplink_tagserver.config import parse_config
from ciplink_tagserver.rules import Rules, match


@pytest.mark.parametrize("pattern,name,expected", [
    ("Bench.SpeedSP", "bench.speedsp", True),  # case-insensitive
    ("Tank[*].Level", "Tank[3].Level", True),  # brackets are literal
    ("Tank[*].Level", "Tank3.Level", False),
    ("Arr[[]3]", "Arr[3]", False),  # the 0.2 fnmatch escape no longer works
    ("Bench.*", "Bench.Axis.Position", True),  # * crosses dots
    ("PLC1/Speed*", "PLC1/SpeedSP", True),
    ("Speed?", "Speed1", True),
    ("Speed?", "Speed12", False),
    ("Speed", "SpeedSP", False),  # whole name
    ("a.b", "aXb", False),  # . is literal
    ("Program:Bench.*", "Program:Bench.Mode", True),
])
def test_match(pattern, name, expected):
    assert match(pattern, name) is expected


def rules(*tables, groups=()):
    return Rules(parse_config({"plc": {"P": {"path": "x", "rule": list(tables), "group": list(groups)}}}))


def test_effective_first_rule_per_field_wins():
    r = rules({"match": "Bench.SpeedSP", "writable": True, "max": 1800},
              {"match": ["Bench.*", "Other"], "units": "rpm", "max": 5, "writable": False, "nonsense": 1})
    assert r.effective("P", "bench.speedsp") == {"writable": True, "max": 1800, "units": "rpm"}
    assert r.effective("P", "Bench.Speed") == {"units": "rpm", "max": 5, "writable": False}
    assert r.effective("P", "Other") == {"units": "rpm", "max": 5, "writable": False}
    assert r.effective("P", "Nothing") == {}


def test_effective_of_a_bit_takes_only_writable_from_its_word():
    r = rules({"match": "W", "writable": True, "min": 0, "bits": {"EStop": 0}, "units": "x", "description": "d"},
              {"match": "V.*", "max": 3, "description": "bit"}, {"match": "V", "writable": True})
    assert r.effective("P", "W.3") == {"writable": True}
    assert r.effective("P", "V.0") == {"description": "bit", "writable": True}  # max never applies to a bit
    assert r.effective("P", "Z.0") == {}


def test_effective_memo_is_bounded(monkeypatch):
    r = rules({"match": "*", "units": "u"})
    monkeypatch.setattr(r, "memo_max", 3)
    for i in range(10):
        assert r.effective("P", f"T{i}") == {"units": "u"}
    assert len(r._memo) <= 3


def test_resolve_bit_names():
    r = rules({"match": "Bench.StatusWord", "bits": {"EStop": 0, "DoorOpen": 1}},
              {"match": "Tank[*].Status", "bits": {"Alarm": 7}})
    assert r.resolve("P", "Bench.StatusWord.estop") == "Bench.StatusWord.0"
    assert r.resolve("P", "Bench.StatusWord.DoorOpen") == "Bench.StatusWord.1"
    assert r.resolve("P", "Tank[2].Status.Alarm") == "Tank[2].Status.7"
    assert r.resolve("P", "Bench.StatusWord.5") == "Bench.StatusWord.5"
    assert r.resolve("P", "Bench.StatusWord.Nope") == "Bench.StatusWord.Nope"  # unknown: unchanged, reads missing
    assert r.resolve("P", "Speed") == "Speed"


def test_groups():
    fast = {"name": "fast", "rate_ms": 50, "match": ["Bench.Axis.*"]}
    slow = {"name": "slow", "rate_ms": 2000, "match": "Tank[*].Temp"}
    r = rules(groups=[fast, {"name": "also", "rate_ms": 10, "match": "Bench.Axis.Position"}, slow])
    assert r.group("P", "Bench.Axis.Position") is fast  # first matching group wins
    assert r.group("P", "tank[1].temp") is slow
    assert r.group("P", "Bench.Speed") is None
    assert [g["name"] for g in r.groups("P")] == ["fast", "also", "slow"]


def test_always_takes_literal_names(caplog):
    r = rules({"match": "Off", "always": False}, {"match": ["Bench.StatusWord", "Off", "Tank[*].Level"], "always": True},
              {"match": "Alarm", "always": True})
    assert r.always("P") == ["Bench.StatusWord", "Alarm"]
    assert "Tank[*].Level" in caplog.text
