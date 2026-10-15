"""A fake driver for --mock: serves the tags of the PLC's ``mock_tags`` JSON file with simulated values.

The file is the old flat ``{name: TYPE}`` or ``{"types": {UDT: {member: TYPE}}, "tags": {name: TYPE}}``, TYPE
being ``NAME`` or ``NAME[n]`` (1-D). Values are functions of the path and the clock, shaped by the name; written
values hold.
"""

import json
import math
import re
import struct
import time
import zlib

from ciplink import CommError, ErrorKind, RequestError, Tag, TagError

SIZES = {"BOOL": 1, "SINT": 1, "USINT": 1, "INT": 2, "UINT": 2, "DINT": 4, "UDINT": 4, "DWORD": 4, "REAL": 4,
         "LINT": 8, "ULINT": 8, "LREAL": 8}
RANGES = {"SINT": (-2**7, 2**7 - 1), "INT": (-2**15, 2**15 - 1), "DINT": (-2**31, 2**31 - 1),
          "LINT": (-2**63, 2**63 - 1), "USINT": (0, 2**8 - 1), "UINT": (0, 2**16 - 1), "UDINT": (0, 2**32 - 1),
          "ULINT": (0, 2**64 - 1), "DWORD": (0, 2**32 - 1)}
FLOATS = {"REAL", "LREAL"}
STRING_LEN = 82

_TYPE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[\s*(\d+)\s*\])?\s*")
_PART = re.compile(r"(\.?)([A-Za-z_][A-Za-z0-9_]*)|\[(\d+(?:,\d+)*)\]|\.(\d+)|\{(\d+)\}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _Error(Exception):
    """Carries the TagError of a failed request."""


def _error(message, kind):
    return _Error(TagError(message, kind))


def _key(d, name):
    """The key of ``d`` that is ``name`` ignoring case, else None."""
    if name in d:
        return name
    return next((k for k in d if k.lower() == name.lower()), None)


def _parse_type(text):
    m = _TYPE.fullmatch(str(text))
    if m is None:
        raise ValueError(f"mock_tags: bad type {text!r}")
    return m.group(1), int(m.group(2) or 0)


def _size(data_type):
    return data_type["template"]["structure_size"] if isinstance(data_type, dict) else SIZES.get(data_type, 4)


def _length(info):
    return info["dimensions"][0] if "dim" in info else info.get("array") or 0


class MockState:
    """What the drivers of one PLC share: written values, ``fail`` (open, read and write raise CommError),
    ``clock`` (seconds, replaceable by tests) and the tag table ``version``."""

    def __init__(self):
        self.written = {}  # canonical leaf path, lowercase -> value
        self.fail = False
        self.clock = time.time
        self.version = 1


def make_factory():
    """A driver factory for ``create_app``; its drivers share a MockState per PLC name, in ``factory.states``."""
    states = {}

    def factory(cfg):
        return MockDriver(cfg, states.setdefault(cfg.name, MockState()))

    factory.states = states
    return factory


class MockDriver:
    """The LogixDriver calls the server uses, with ciplink-shaped ``tags``, ``data_types`` and results."""

    connection_size = 4000

    def __init__(self, cfg, state=None):
        self.state = state or MockState()
        data = {}
        if cfg.mock_tags:
            with open(cfg.mock_tags, encoding="utf-8") as file:
                data = json.load(file)
        structured = isinstance(data.get("tags"), dict)
        self._udts = (data.get("types") or {}) if structured else {}
        self.data_types = {}
        self.tags = {}
        for name, text in (data["tags"] if structured else data).items():
            type_name, count = _parse_type(text)
            data_type = self._data_type(type_name)
            self.tags[name] = {"tag_name": name, "dim": 1 if count else 0, "dimensions": [count, 0, 0],
                               "alias": False, **self._definition(data_type)}

    @property
    def _tag_table_version(self):
        return self.state.version

    @staticmethod
    def _definition(data_type):
        struct = isinstance(data_type, dict)
        return {"tag_type": "struct" if struct else "atomic", "data_type": data_type,
                "data_type_name": data_type["name"] if struct else data_type}

    def _data_type(self, name, stack=()):
        """An atomic type name, a ciplink-like UDT dict (in ``data_types``) or, unknown, the name itself."""
        if name.upper() in SIZES:
            return name.upper()
        key = "STRING" if name.upper() == "STRING" else _key(self._udts, name)
        if key is None:
            return name
        if key in self.data_types:
            return self.data_types[key]
        if key in stack:
            raise ValueError(f"mock_tags: {key} contains itself")
        if key == "STRING":
            members, extra = {"LEN": "DINT", "DATA": f"SINT[{STRING_LEN}]"}, {"string": STRING_LEN}
        else:
            members, extra = self._udts[key], {}
        internal, offset = {}, 0
        for member, text in members.items():
            type_name, count = _parse_type(text)
            data_type = self._data_type(type_name, stack + (key,))
            internal[member] = {"offset": offset, "array": count, **self._definition(data_type)}
            offset += _size(data_type) * max(count, 1)
        self.data_types[key] = {"name": key, "internal_tags": internal, "attributes": list(internal),
                                "template": {"structure_size": max(4, -(-offset // 4) * 4)}, **extra}
        return self.data_types[key]

    def open(self):
        if self.state.fail:
            raise CommError("mock: connection refused")
        return True

    def close(self):
        pass

    def get_tag_info(self, name):
        """As ciplink: the definition of a tag or member path, indexes ignored; raises for unknown names."""
        key = _key(self.tags, name)  # a flat file may list dotted names
        if key is not None:
            return self.tags[key]
        prefix, rest = "", name
        if name[:8].lower() == "program:" and "." in name:
            prefix, rest = name.split(".", 1)
            prefix += "."
        base, *attrs = re.sub(r"\[[^\]]*\]|\{\d+\}", "", rest).split(".")
        info = self.tags.get(_key(self.tags, prefix + base))
        for attr in attrs:
            internal = info["data_type"]["internal_tags"] if info and isinstance(info["data_type"], dict) else {}
            info = internal.get(_key(internal, attr))
        if info is None:
            raise RequestError(TagError(f"Tag doesn't exist - {name}", ErrorKind.NOT_FOUND))
        return info

    def _resolve(self, name):
        """(canonical path, definition, whole array?, bit, element count, (array path, start) of a range) of a
        request name. Raises _Error."""
        missing = _error(f"Tag doesn't exist - {name}", ErrorKind.NOT_FOUND)
        key = _key(self.tags, name)
        if key is not None:
            return key, self.tags[key], bool(self.tags[key]["dim"]), None, None, (key, 0)
        prefix, rest = "", name
        if name[:8].lower() == "program:":
            if "." not in name:
                raise missing
            prefix, rest = name.split(".", 1)
            prefix += "."
        parts, pos = [], 0
        while pos < len(rest):
            m = _PART.match(rest, pos)
            if m is None or (m.group(2) and bool(m.group(1)) != (pos > 0)):
                raise _error(f"Invalid tag name - {name}", ErrorKind.BAD_REQUEST)
            parts.append(m)
            pos = m.end()
        if not parts or not parts[0].group(2) or (key := _key(self.tags, prefix + parts[0].group(2))) is None:
            raise missing
        path, info, array, bit, count = key, self.tags[key], bool(self.tags[key]["dim"]), None, None
        run, indexed = (path, 0), False
        for m in parts[1:]:
            if bit is not None or count is not None:
                raise _error(f"Invalid tag name - {name}", ErrorKind.BAD_REQUEST)
            if m.group(2):  # .member
                data_type = info["data_type"]
                if array or not isinstance(data_type, dict) or data_type.get("string"):
                    raise missing
                member = _key(data_type["internal_tags"], m.group(2))
                if member is None:
                    raise missing
                path, info = f"{path}.{member}", data_type["internal_tags"][member]
                array, indexed = bool(info.get("array")), False
            elif m.group(3):  # [index]
                index = [int(i) for i in m.group(3).split(",")]
                if not array or len(index) != 1 or index[0] >= _length(info):
                    raise missing
                run, path, array, indexed = (path, index[0]), f"{path}[{index[0]}]", False, True
            elif m.group(4):  # .bit
                size = SIZES.get(info["data_type_name"])
                if array or info["data_type_name"] not in RANGES:
                    raise _error(f"No bits in {info['data_type_name']} - {name}", ErrorKind.BAD_REQUEST)
                bit = int(m.group(4))
                if bit >= size * 8:
                    raise missing
            else:  # {count}
                count = int(m.group(5))
                if not (array or indexed) or count < 1:
                    raise _error(f"Invalid element count - {name}", ErrorKind.BAD_REQUEST)
                if array:
                    run = (path, 0)
                if run[1] + count > _length(info):
                    raise missing
        return path, info, array, bit, count, run

    def read(self, *tags):
        if self.state.fail:
            raise CommError("mock: connection lost")
        now = self.state.clock()
        results = [self._read(name, now) for name in tags]
        return results[0] if len(results) == 1 else results

    def _read(self, name, now):
        try:
            path, info, array, bit, count, (array_path, start) = self._resolve(name)
            string = isinstance(info["data_type"], dict) and info["data_type"].get("string")
            type_name = "STRING" if string else info["data_type_name"]
            if count is not None:
                value = [self._value(f"{array_path}[{i}]", info, False, now) for i in range(start, start + count)]
                return Tag(name, value, f"{type_name}[{count}]", None)
            if array:  # an array without an index reads its first element
                return Tag(name, self._value(f"{path}[0]", info, False, now), type_name, None)
            value = self._value(path, info, False, now)
            if bit is not None:
                return Tag(name, bool(value >> bit & 1), "BOOL", None)
            return Tag(name, value, type_name, None)
        except _Error as error:
            return Tag(name, None, None, error.args[0])

    def _value(self, path, info, array, now):
        """The value at ``path``: a list for a whole array, a dict for a struct, else the simulated leaf."""
        if array:
            return [self._value(f"{path}[{i}]", info, False, now) for i in range(_length(info))]
        data_type = info["data_type"]
        if isinstance(data_type, dict) and not data_type.get("string"):
            return {m: self._value(f"{path}.{m}", data_type["internal_tags"][m],
                                   bool(data_type["internal_tags"][m].get("array")), now)
                    for m in data_type["attributes"]}
        return self._leaf(path, "STRING" if isinstance(data_type, dict) else data_type, now)

    def _leaf(self, path, type_name, now):
        """A written value, else a deterministic function of the path and time, shaped by the member name."""
        written = self.state.written.get(path.lower())
        if written is not None:
            return written
        if type_name not in SIZES and type_name != "STRING":
            raise _error(f"mock does not simulate {type_name}", ErrorKind.OTHER)
        seed = zlib.crc32(path.lower().encode())
        wave = math.sin(2 * math.pi * now / (10 + seed % 20) + seed % 628 / 100)
        name = _NAME.findall(path)[-1].lower()
        parent = path[:path.rfind(".") + 1]
        if type_name == "STRING":
            return f"{_NAME.findall(path)[-1]} {int(now // 5) % 100}"
        if type_name == "BOOL":
            return False if name.endswith("pb") or name == "running" else wave > 0
        if name.endswith("sp") or "setpoint" in name:
            value = seed % 10 * 100
        elif "temp" in name:
            value = 55 + 35 * wave
        elif "level" in name:
            value = 50 + 50 * wave
        elif "speed" in name or "rpm" in name:
            value = self._follow(parent, _NAME.findall(path)[-1], now)
            value = 50 + 50 * wave if value is None else value
        elif "position" in name:
            value = (now * 25 + seed) % 1000
        elif "count" in name:
            value = int(now) % 100000 + seed % 1000
        elif type_name in RANGES and any(w in name for w in ("word", "status", "flags", "bits")):
            value = sum((int(now / (2 + i)) + (seed >> i)) % 2 << i for i in range(4))  # a few toggling bits
        else:
            value = 50 + 25 * wave
        if type_name in FLOATS:
            return float(round(value, 3))
        low, high = RANGES[type_name]
        return low + (round(value) - low) % (high - low + 1)

    def _follow(self, parent, member, now):
        """Its setpoint ``<member>SP`` while the sibling ``Running`` is true, 0 while it is false; None if there
        are no such siblings."""
        try:
            sp, sp_info, *_ = self._resolve(f"{parent}{member}SP")
            running, _, *_ = self._resolve(f"{parent}Running")
        except _Error:
            return None
        if not self._leaf(running, "BOOL", now):
            return 0
        target = self._leaf(sp, sp_info["data_type_name"], now)
        return target + 0.2 * math.sin(now) if target else target  # a ripple under a typical deadband

    def write(self, name, value):
        """Type-checked as ciplink 0.2 does (kind ``bad_value``, nothing written); a bit is read-modify-write."""
        if self.state.fail:
            raise CommError("mock: connection lost")
        now = self.state.clock()
        try:
            path, info, array, bit, count, (array_path, start) = self._resolve(name)
            if bit is not None:
                word = self._value(path, info, False, now)
                low, high = RANGES[info["data_type_name"]]
                word = word | 1 << bit if _check("BOOL", value) else word & ~(1 << bit)
                writes = [(path, low + (word - low) % (high - low + 1))]
            elif count is not None:
                if not isinstance(value, (list, tuple)) or len(value) != count:
                    raise _error(f"Expected {count} values - {value!r}", ErrorKind.BAD_VALUE)
                writes = [w for i, v in zip(range(start, start + count), value)
                          for w in _writes(f"{array_path}[{i}]", info, False, v)]
            else:
                writes = _writes(path, info, array, value)
        except _Error as error:
            return Tag(name, value, None, error.args[0])
        for leaf, leaf_value in writes:
            self.state.written[leaf.lower()] = leaf_value
            member = _NAME.findall(leaf)[-1].lower()
            if leaf_value is True and member.endswith(("startpb", "stoppb")):
                try:
                    running = self._resolve(leaf[:leaf.rfind(".") + 1] + "Running")[0]
                    self.state.written[running.lower()] = member.endswith("startpb")
                except _Error:
                    pass
        return Tag(name, value, info["data_type_name"] if bit is None else "BOOL", None)


def _check(type_name, value):
    """``value`` as stored for a leaf of ``type_name``; raises _Error (``bad_value``) if it does not fit."""
    ok = False
    if type_name == "BOOL":
        ok = isinstance(value, bool) or (type(value) is int and value in (0, 1))
        value = bool(value) if ok else value
    elif type_name in RANGES:
        ok = type(value) is int and RANGES[type_name][0] <= value <= RANGES[type_name][1]
    elif type_name in FLOATS:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if ok and type_name == "REAL":  # ciplink packs '<f': OverflowError past float32
            try:
                struct.pack("<f", value)
            except OverflowError:
                ok = False
        value = float(value) if ok else value
    elif type_name == "STRING":
        ok = isinstance(value, str) and len(value) <= STRING_LEN
    if not ok:
        raise _error(f"Invalid value for {type_name} - {value!r}", ErrorKind.BAD_VALUE)
    return value


def _writes(path, info, array, value, member=False):
    """[(leaf path, value)] for writing ``value`` at ``path``; raises _Error (``bad_value``) if it does not fit.
    As ciplink: an array written by name takes one value (element 0), an array member of a written struct takes
    all its elements, and a struct takes all its members."""
    if array:
        if member:
            if not isinstance(value, (list, tuple)) or len(value) != _length(info):
                raise _error(f"Expected {_length(info)} values - {value!r}", ErrorKind.BAD_VALUE)
            values = value
        else:
            values = value if isinstance(value, (list, tuple)) else [value]
            if len(values) != 1:
                raise _error("Too many values for requested elements, expected 1", ErrorKind.BAD_VALUE)
        return [w for i, v in enumerate(values) for w in _writes(f"{path}[{i}]", info, False, v)]
    data_type = info["data_type"]
    if isinstance(data_type, dict) and not data_type.get("string"):
        if not isinstance(value, dict):
            raise _error(f"Invalid value for {data_type['name']} - {value!r}", ErrorKind.BAD_VALUE)
        writes, given = [], set()
        for name, v in value.items():
            key = name if name in data_type["internal_tags"] else None  # member names are case-sensitive here
            if key is None:
                raise _error(f"No member {name} in {data_type['name']}", ErrorKind.BAD_VALUE)
            given.add(key)
            sub = data_type["internal_tags"][key]
            writes += _writes(f"{path}.{key}", sub, bool(sub.get("array")), v, True)
        missing = [m for m in data_type["attributes"] if m not in given]
        if missing:
            raise _error(f"Missing members {', '.join(missing)} of {data_type['name']}", ErrorKind.BAD_VALUE)
        return writes
    return [(path, _check("STRING" if isinstance(data_type, dict) else data_type, value))]
