"""Read planner: one read of a struct instead of several reads of its members, which are sliced out of it.

A poll costs about 12 ms per packet plus 1.35 ms per read service, so fewer reads beat fewer bytes. For the
tags of one cycle, members of the same struct (a tag, an array element or a nested member) are read through
the smallest such struct that holds at least two of them and is at most ``whole_max_bytes``. A requested tag
that is a whole struct is read anyway, so the tags below it are sliced out of it at no cost.
"""

import re
from collections import defaultdict

from ciplink import Tag

_TOKEN = re.compile(r"(?P<name>\.?[A-Za-z_][A-Za-z0-9_]*)|\[(?P<index>\d+(?:,\d+)*)\]")


def tokenize(name: str):
    """[(text, key)] of a tag name: key is a member name, an element index (int) or a tuple for a
    multi-dimensional index. None for what is never grouped: bit access (``X.5``), ``X{3}``, odd syntax."""
    prefix, rest = "", name
    if name[:8].lower() == "program:":
        dot = name.find(".")
        if dot < 0:
            return None
        prefix, rest = name[: dot + 1], name[dot + 1:]
    tokens, pos = [], 0
    while pos < len(rest):
        m = _TOKEN.match(rest, pos)
        if m is None:
            return None
        text = m.group(0)
        if m.group("name") is not None:
            if (pos == 0) == text.startswith("."):  # the first name has no dot, the others need one
                return None
            key = text.lstrip(".")
        else:
            index = [int(i) for i in m.group("index").split(",")]
            key = index[0] if len(index) == 1 else tuple(index)
        tokens.append((text, key))
        pos = m.end()
    if not tokens or isinstance(tokens[0][1], (int, tuple)):
        return None
    tokens[0] = (prefix + tokens[0][0], prefix + tokens[0][1])
    return tokens


def _is_array(info) -> bool:
    return bool(info.get("dim") or info.get("array"))


def _whole_readable(data_type, memo) -> bool:
    """False if this type, or a struct inside it, is an AOI the controller refuses to read whole."""
    name = data_type.get("name")
    if name in memo:
        return memo[name]
    memo[name] = True  # guards recursive definitions
    aoi = data_type.get("aoi")
    ok = not (aoi and not aoi.get("whole_read", True))
    for member in (data_type.get("internal_tags") or {}).values():
        sub = member.get("data_type")
        if ok and member.get("tag_type") == "struct" and isinstance(sub, dict):
            ok = _whole_readable(sub, memo)
    memo[name] = ok
    return ok


def _struct_size(info, last_key, memo):
    """structure_size if the path is one struct that can be read whole, else None."""
    if info is None or info.get("tag_type") != "struct":
        return None
    if not isinstance(last_key, (int, tuple)) and _is_array(info):
        return None  # a whole array: a read without an index gets its first element only
    data_type = info.get("data_type")
    if not isinstance(data_type, dict) or data_type.get("string") or not _whole_readable(data_type, memo):
        return None
    return (data_type.get("template") or {}).get("structure_size")


def plan(tags, get_info, whole_max_bytes, no_group=frozenset()):
    """The reads for ``tags``: [(read name, [(tag, path)])], path being the keys that slice the tag's value
    out of the read value, or None for the value as read. ``get_info(name)`` is the driver's tag definition,
    None if unknown. Names in ``no_group`` (lowercase) are not read whole."""
    infos = {}

    def info(name):
        key = name.lower()
        if key not in infos:
            try:
                infos[key] = get_info(name)
            except Exception:
                infos[key] = None
        return infos[key]

    memo = {}
    parsed = {}  # tag -> tokens
    ancestors = {}  # tag -> [key], deepest first, of structs above it that can be read whole
    depth = defaultdict(dict)  # tag -> key -> number of tokens of that ancestor
    names, sizes = {}, {}  # key -> read name, structure_size
    whole = {}  # key -> requested tag that is itself a whole-readable struct
    seen = set()
    for tag in tags:
        tokens = tokenize(tag)
        if tokens is None or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tinfo = info(tag)
        if tinfo is None or (_is_array(tinfo) and not isinstance(tokens[-1][1], (int, tuple))):
            continue  # unknown, or an array read without an index (its first element only)
        parsed[tag] = tokens
        found = []
        n = len(tokens)
        for k in range(n, 0, -1):
            if k < n:  # the slice gains token k: a name, or an index of a list (not BOOL/DWORD bits)
                key_k = tokens[k][1]
                if isinstance(key_k, tuple):
                    break
                if isinstance(key_k, int):
                    array = info("".join(t for t, _ in tokens[:k]))
                    if array is None or array.get("data_type_name") in ("BOOL", "DWORD"):
                        break
            prefix = "".join(t for t, _ in tokens[:k])
            key = prefix.lower()
            if key in no_group:
                continue
            size = _struct_size(info(prefix), tokens[k - 1][1], memo)
            if size is None:
                continue
            depth[tag][key] = k
            names.setdefault(key, prefix)
            if k == n:
                whole[key] = tag
            else:
                found.append(key)
                sizes[key] = size
        ancestors[tag] = found

    # tags under a requested whole struct come free with it; the others are the units to group
    covered = {tag: next((k for k in found if k in whole), None) for tag, found in ancestors.items()}
    under = defaultdict(int)
    for tag, found in ancestors.items():
        if covered[tag] is None:
            for key in found:
                under[key] += 1
    assign = {}
    for tag, found in ancestors.items():
        if covered[tag] is not None:
            assign[tag] = covered[tag]
        else:
            assign[tag] = next((k for k in found if sizes[k] <= whole_max_bytes and under[k] >= 2), None)
            if assign[tag] is None and tag.lower() in whole:
                assign[tag] = tag.lower()

    def final(tag):
        key = assign[tag]
        while key is not None and key in whole and whole[key] != tag and assign[whole[key]] != key:
            tag, key = whole[key], assign[whole[key]]
        return key

    groups = defaultdict(list)
    for tag in ancestors:
        key = final(tag)
        if key is not None:
            groups[key].append(tag)
    result, grouped = [], set()
    for key, members in groups.items():
        if len(members) == 1 and whole.get(key) != members[0]:
            continue  # a group of one is just a bigger read
        entries = []
        for tag in members:
            k = depth[tag][key]
            entries.append((tag, None if k == len(parsed[tag]) else [key_ for _, key_ in parsed[tag][k:]]))
            grouped.add(tag)
        result.append((names[key], entries))
    result += [(tag, [(tag, None)]) for tag in tags if tag not in grouped]
    return result


def dig(value, path):
    """The part of a parsed struct value at ``path`` (member names, list indexes)."""
    for key in path:
        if isinstance(key, int):
            if not isinstance(value, list):
                raise TypeError("not a list")
            value = value[key]
        else:
            if not isinstance(value, dict):
                raise TypeError("not a struct")
            if key not in value:
                key = next(k for k in value if k.lower() == key.lower())
            value = value[key]
    return value


def _as_list(results):
    return results if isinstance(results, list) else [results]


def execute(plc, reads):
    """Runs a plan: ({tag: Tag}, names of the group reads that failed, number of read services).
    The members of a failed group read are read one by one in the same call, so no value is ever wrong."""
    names = [name for name, _ in reads]
    results = _as_list(plc.read(*names)) if names else []
    out, retry, failed = {}, [], []
    for (name, members), result in zip(reads, results):
        if len(members) == 1 and members[0][1] is None:
            out[members[0][0]] = result
            continue
        if result.error is not None:
            failed.append(name)
            retry += [tag for tag, _ in members]
            continue
        for tag, path in members:
            if path is None:
                out[tag] = result
                continue
            try:
                out[tag] = Tag(tag, dig(result.value, path), None, None)
            except (KeyError, IndexError, TypeError, StopIteration):
                if name not in failed:
                    failed.append(name)
                retry.append(tag)
    if retry:
        out.update(zip(retry, _as_list(plc.read(*retry))))
    return out, failed, len(names) + len(retry)
