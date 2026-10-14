"""Read planner: one read of a struct instead of several reads of its members, which are sliced out of it.

A poll costs about 12 ms per packet plus 1.35 ms per read service, so fewer reads matter, but an extra
packet costs far more than a few reads. For the tags of one cycle, a struct (a tag, an array element or a
nested member, at most ``whole_max_bytes``) replaces the reads of the tags under it when that pays: groups are
taken best first by extra reply bytes per read saved, each only if it costs at most ``BYTES_PER_READ`` extra
reply bytes per read saved and the cycle's estimated packet count does not grow over plain reads.
"""

import math
import re
from collections import defaultdict

from ciplink import Tag

try:
    from ciplink.packets.logix import TagTableVersionRequestPacket as _Version

    VERSION_REQUEST, VERSION_REPLY = _Version.request_size, _Version.reply_size
except (ImportError, AttributeError):  # an older ciplink: no tag table version check per packet
    VERSION_REQUEST = VERSION_REPLY = 0

BYTES_PER_READ = 450  # one read service (~1.35 ms) costs about as much packet time as this many reply bytes
PACKET_BYTES = 4000  # request and reply budget of a packet when the driver has no connection_size
ATOMIC_SIZES = {"BOOL": 1, "SINT": 1, "USINT": 1, "BYTE": 1, "INT": 2, "UINT": 2, "WORD": 2, "DINT": 4,
                "UDINT": 4, "DWORD": 4, "REAL": 4, "LINT": 8, "ULINT": 8, "LWORD": 8, "LREAL": 8}

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


def request_size(name) -> int:
    """Estimated size of a Read Tag service for ``name``, the base tag addressed by its instance ID."""
    tokens = tokenize(name)
    if tokens is None:
        return 10 + len(name) + len(name) % 2
    size = 4 + 6  # service, path size, element count; class and instance segments
    if tokens[0][0].lower().startswith("program:"):
        program = tokens[0][0].split(".")[0]
        size += 2 + len(program) + len(program) % 2
    for _, key in tokens[1:]:
        if isinstance(key, str):
            size += 2 + len(key) + len(key) % 2
        else:
            size += sum(2 if i < 256 else 4 for i in (key if isinstance(key, tuple) else (key,)))
    return size


def reply_size(info) -> int:
    """Estimated reply size of a read of one element of the tag ``info`` defines (12 when unknown)."""
    if info is None:
        return 12
    if info.get("tag_type") == "struct":
        data_type = info.get("data_type")
        size = (data_type.get("template") or {}).get("structure_size") if isinstance(data_type, dict) else None
        return (size or 4) + 10
    return ATOMIC_SIZES.get(info.get("data_type_name") or info.get("data_type"), 4) + 8


def packets(sizes, budget=PACKET_BYTES) -> int:
    """Packets for reads of these (request, reply) sizes, packed in order as ciplink does."""
    empty_request, empty_reply = 10 + VERSION_REQUEST, 8 + VERSION_REPLY
    count, request, reply = 0, empty_request, empty_reply
    for req, rep_ in sizes:
        if empty_request + req > budget or empty_reply + rep_ > budget:  # fragmented: packets of its own
            count += math.ceil(rep_ / (budget - empty_reply))
            continue
        if request + req > budget or reply + rep_ > budget:
            count += 1
            request, reply = empty_request, empty_reply
        request += req
        reply += rep_
    return count + (1 if request > empty_request else 0)


def plan(tags, get_info, whole_max_bytes, no_group=frozenset(), budget=PACKET_BYTES):
    """The reads for ``tags``: [(read name, [(tag, path)])], path being the keys that slice the tag's value
    out of the read value, or None for the value as read. ``get_info(name)`` is the driver's tag definition,
    None if unknown. Names in ``no_group`` (lowercase) are not read whole. ``budget``: bytes per packet."""
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
            sizes[key] = size
            if k == n:
                whole[key] = tag
            else:
                found.append(key)
        ancestors[tag] = found

    under = defaultdict(set)  # key -> the tags it can serve
    for tag, found in ancestors.items():
        for key in found:
            under[key].add(tag)
    for key, tag in whole.items():
        under[key].add(tag)
    candidates = {key for key, served in under.items()
                  if len(served) >= 2 and (key in whole or sizes[key] <= whole_max_bytes)}

    # reads: [key or None, members, (request, reply)], in the order they are sent; start with plain reads
    reads = [[None, [tag], (request_size(tag), reply_size(info(tag) if tag in parsed else None))] for tag in tags]
    limit = packets([r[2] for r in reads], budget)
    while candidates:
        best = None
        for key in candidates:
            covered = [i for i, r in enumerate(reads) if all(t in under[key] for t in r[1])]
            if len(covered) < 2:
                continue
            saved = len(covered) - 1
            group = (request_size(names[key]), sizes[key] + 10)
            extra = group[1] - sum(reads[i][2][1] for i in covered)
            if extra > BYTES_PER_READ * saved:
                continue
            members = [t for i in covered for t in reads[i][1]]
            new = [r for i, r in enumerate(reads) if i not in covered[1:]]
            new[covered[0]] = [key, members, group]
            if packets([r[2] for r in new], budget) > limit:
                continue
            score = (extra / saved, -saved, key)
            if best is None or score < best[0]:
                best = (score, key, new)
        if best is None:
            break
        _, key, reads = best
        candidates.discard(key)

    result = []
    for key, members, _ in reads:
        if key is None:
            result.append((members[0], [(members[0], None)]))
            continue
        entries = []
        for tag in members:
            k = depth[tag][key]
            entries.append((tag, None if k == len(parsed[tag]) else [key_ for _, key_ in parsed[tag][k:]]))
        result.append((names[key], entries))
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
