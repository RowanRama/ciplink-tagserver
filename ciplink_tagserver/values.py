"""Turns driver results into JSON-ready cache entries."""

import math

AOI_LOCAL = "AOI local tag, not externally accessible"


def quality(error) -> str:
    if error is None:
        return "good"
    if str(error).startswith("Tag doesn't exist") or AOI_LOCAL in str(error):
        return "missing"
    return "bad"


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
    """(value, quality) for a driver Tag result; the value is None unless the read succeeded."""
    q = quality(tag.error)
    return (jsonable(tag.value) if q == "good" else None), q


def type_name(info: dict) -> str:
    """Data type name of an entry of ``plc.tags``."""
    if info.get("data_type_name"):
        return info["data_type_name"]
    data_type = info.get("data_type")
    return data_type if isinstance(data_type, str) else (data_type or {}).get("name", "")


def tag_list(plc) -> list[dict]:
    """Tag names with data type and, for AOIs, the readable members, from ``plc.tags``."""
    result = []
    for name, info in plc.tags.items():
        entry = {"name": name, "type": type_name(info)}
        data_type = info.get("data_type")
        if info.get("tag_type") == "struct" and isinstance(data_type, dict) and data_type.get("aoi"):
            entry["aoi_readable"] = list(data_type["aoi"].get("readable", []))
        result.append(entry)
    return result
