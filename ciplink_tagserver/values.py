"""Turns driver results into JSON-ready cache entries and the controller structure into /tags data."""

import math

from ciplink import ErrorKind

MISSING = {ErrorKind.NOT_FOUND, ErrorKind.ACCESS}


def quality(tag) -> str:
    """good, missing (no such tag or not accessible) or bad, from ``tag.error_kind``."""
    kind = tag.error_kind
    if kind is None:
        return "good"
    return "missing" if kind in MISSING else "bad"


def error_of(tag) -> dict | None:
    kind = tag.error_kind
    return None if kind is None else {"kind": kind.value, "message": str(tag.error)}


def jsonable(value):
    """Dicts for structs/AOIs, lists for arrays, bytes as hex, non-finite floats as strings."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value).replace("inf", "Infinity").replace("nan", "NaN")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def to_value(tag) -> tuple:
    """(value, quality, error) for a driver Tag result; the value is None unless the read succeeded."""
    q = quality(tag)
    return (jsonable(tag.value) if q == "good" else None), q, error_of(tag)


def merge(old, tag, t) -> list:
    """The cache entry ``[value, quality, t, error]`` for a read result; ``bad`` keeps the value of ``old``
    (the previous entry or None), so a screen shows the last value marked bad."""
    value, q, error = to_value(tag)
    if q == "bad" and old is not None:
        value = old[0]
    return [value, q, t, error]


def is_string(data_type) -> bool:
    return isinstance(data_type, dict) and bool(data_type.get("string"))


def type_name(info: dict) -> str:
    """Data type name of an entry of ``plc.tags`` or a struct member; string types are STRING."""
    data_type = info.get("data_type")
    if is_string(data_type):
        return "STRING"
    if info.get("data_type_name"):
        return info["data_type_name"]
    return data_type if isinstance(data_type, str) else (data_type or {}).get("name", "")


def bool_array(info: dict) -> bool:
    """ciplink describes a BOOL[n] tag or member as a DWORD array whose dims count 32-bit words."""
    return type_name(info) == "DWORD" and bool(info.get("dim") or info.get("array"))


def tag_list(plc) -> list[dict]:
    """Tag names with data type, dims of arrays and, for AOIs, the readable members, from ``plc.tags``."""
    result = []
    for name, info in plc.tags.items():
        bools = bool_array(info)
        entry = {"name": name, "type": "BOOL" if bools else type_name(info)}
        dim = info.get("dim") or 0
        if dim:
            entry["dims"] = list(info.get("dimensions") or [0] * dim)[:dim]
            if bools:
                entry["dims"][-1] *= 32
        data_type = info.get("data_type")
        if info.get("tag_type") == "struct" and isinstance(data_type, dict) and data_type.get("aoi"):
            entry["aoi_readable"] = list(data_type["aoi"].get("readable", []))
        result.append(entry)
    return result


def type_map(plc) -> dict:
    """UDT and AOI name -> {"members": {m: {type, dims?}}, "aoi"?: true}, from ``plc.data_types``.
    Private members are hidden, string types are left out (their tags and members show as STRING)."""
    result = {}
    for name, data_type in (getattr(plc, "data_types", None) or {}).items():
        if is_string(data_type):
            continue
        internal = data_type.get("internal_tags", {})
        aoi = data_type.get("aoi")
        if aoi:
            names = aoi.get("readable", [])
        elif "attributes" in data_type:
            names = data_type["attributes"]
        else:
            names = [m for m in internal if not m.startswith(("ZZZZZZZZZZ", "__"))]
        members = {}
        for member in names:
            info = internal.get(member, {})
            bools = bool_array(info)
            members[member] = {"type": "BOOL" if bools else type_name(info)}
            if info.get("array"):
                members[member]["dims"] = [info["array"] * (32 if bools else 1)]
        result[name] = {"members": members, **({"aoi": True} if aoi else {})}
    return result
