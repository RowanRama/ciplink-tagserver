"""Read planner: one read of a struct instead of several reads of its members, which are sliced out of it.

A poll costs about 12 ms per packet plus 1.35 ms per read service, so fewer reads matter, but an extra
packet costs far more than a few reads. For the tags of one cycle, a struct (a tag, an array element or a
nested member, at most ``whole_max_bytes``) or a run of elements of one atomic array (``A[3]{5}``) replaces
the reads of the tags under it when that pays: groups are taken best first by extra reply bytes per read
saved, each only if it costs at most ``BYTES_PER_READ`` extra reply bytes per read saved and the cycle's
estimated packet count does not grow over plain reads. A ``together`` struct is always read whole.
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

_RANGE = re.compile(r"(.+)\{(\d+)\}")
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


def request_size(name, get_info=None) -> int:
    """Estimated size of a Read Tag service for ``name``, the base tag addressed by its instance ID (sized
    from ``get_info(base)``; a 16-bit ID when unknown, as on most real controllers)."""
    tokens = tokenize(name)
    if tokens is None:
        return 10 + len(name) + len(name) % 2
    base = (get_info(tokens[0][0]) or {}) if get_info else {}
    instance = base.get("instance_id")
    size = 4 + 4 + (2 if isinstance(instance, int) and instance < 256 else 4 if (instance or 0) < 65536 else 6)
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




def _pack(sizes, budget):
    """([[index]] of the packets reads of these (request, reply) sizes go in, packet count), packed in order
    as ciplink does: the other reads first, then each fragmented read alone, taking several packets. With
    version checks, a read sent alone also gets a standalone check before and after it (the worker sends
    each in its own plc.read, so both are counted)."""
    empty_request, empty_reply = 10 + VERSION_REQUEST, 8 + VERSION_REPLY
    groups, fragmented_groups, count, current, request, reply = [], [], 0, [], empty_request, empty_reply
    for i, (req, rep_) in enumerate(sizes):
        if empty_request + req > budget or empty_reply + rep_ > budget:
            fragmented_groups.append([i])
            count += max(1, math.ceil(rep_ / (budget - empty_reply))) + (2 if VERSION_REQUEST else 0)
            continue
        if current and (request + req > budget or reply + rep_ > budget):
            groups.append(current)
            count += 1
            current, request, reply = [], empty_request, empty_reply
        current.append(i)
        request += req
        reply += rep_
    if current:
        groups.append(current)
        count += 1
    return groups + fragmented_groups, count


def packets(sizes, budget=PACKET_BYTES) -> int:
    """Packets for reads of these (request, reply) sizes, packed in order as ciplink does."""
    return _pack(sizes, budget)[1]


def _info_getter(get_info):
    """get_info memoized per call, None for what it doesn't know or raises on."""
    infos = {}

    def info(name):
        key = name.lower()
        if key not in infos:
            try:
                infos[key] = get_info(name)
            except Exception:
                infos[key] = None
        return infos[key]

    return info


def _element_size(info):
    """Size of one element of an atomic 1-D array whose elements can be read as a run, else None."""
    if not info or info.get("tag_type") != "atomic" or not (info.get("dim") == 1 or (info.get("array") or 0) > 0):
        return None
    name = info.get("data_type_name") or info.get("data_type")
    return None if name in ("BOOL", "DWORD") else ATOMIC_SIZES.get(name)  # BOOL/DWORD elements are bits


def read_size(name, get_info):
    """Estimated (request, reply) sizes of a read of ``name`` as ``plan`` writes it, ``A[3]{5}`` included."""
    info = _info_getter(get_info)
    m = _RANGE.fullmatch(name)
    if not m:
        return request_size(name, info), reply_size(info(name))
    array, count = info(m.group(1)), int(m.group(2))
    if array and (array.get("data_type_name") or array.get("data_type")) == "DWORD":  # BOOLs, 32 per word
        return request_size(m.group(1), info), math.ceil(count / 32) * 4 + 8
    overhead = 10 if array and array.get("tag_type") == "struct" else 8
    return request_size(m.group(1), info), count * (reply_size(array) - overhead) + overhead


def plan(tags, get_info, whole_max_bytes, no_group=frozenset(), budget=PACKET_BYTES, together=None):
    """The reads for ``tags`` in packet order: [(read name, [(tag, path)])], path being the keys that slice the
    tag's value out of the read value, or None for the value as read. ``get_info(name)`` is the driver's tag
    definition, None if unknown. Names in ``no_group`` (lowercase) are not read whole. ``budget``: bytes per
    packet. ``together(struct path)``: true if the requested tags under that struct are always read in one read."""
    info = _info_getter(get_info)
    memo = {}
    parsed = {}  # tag -> tokens
    ancestors = {}  # tag -> [key], deepest first, of structs above it that can be read whole
    depth = defaultdict(dict)  # tag -> key -> number of tokens of that ancestor
    names, sizes, levels = {}, {}, {}  # struct key -> read name, structure_size, number of tokens
    whole = {}  # key -> requested tag that is itself a whole-readable struct
    elements = defaultdict(list)  # array key -> [(index, tag)] of requested elements of an atomic array
    arrays = {}  # array key -> (array name, element size)
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
        if len(tokens) > 1 and type(tokens[-1][1]) is int:
            array = "".join(t for t, _ in tokens[:-1])
            size = _element_size(info(array))
            if size and array.lower() not in no_group:
                elements[array.lower()].append((tokens[-1][1], tag))
                arrays[array.lower()] = (array, size)
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
            levels[key] = k
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

    # array runs: the requested elements, split where bridging the gap alone would break the cost rule
    runs = {}  # run key -> (array name, element size, {tag: index})
    for akey, items in elements.items():
        items.sort()
        array, size = arrays[akey]
        clusters = [[items[0]]]
        for index, tag in items[1:]:
            if (index - clusters[-1][-1][0] - 1) * size > BYTES_PER_READ:
                clusters.append([])
            clusters[-1].append((index, tag))
        for c, cluster in enumerate(clusters):
            if len(cluster) >= 2:
                key = f"{akey}{{{c}"  # a "{" is never in a struct key
                runs[key] = (array, size, {tag: index for index, tag in cluster})
                under[key] = {tag for _, tag in cluster}
                candidates.add(key)

    def group(key, members):
        """(read name, (request, reply)) of the read of ``key`` that serves ``members``."""
        if key not in runs:
            return names[key], (request_size(names[key], info), sizes[key] + 10)
        array, size, index = runs[key]
        start = min(index[t] for t in members)
        count = max(index[t] for t in members) - start + 1
        return f"{array}[{start}]{{{count}}}", (request_size(f"{array}[{start}]", info), count * size + 8)

    def merge(key):
        """(reads with the ones all under ``key`` merged at the first one's place, reads saved, extra reply
        bytes, read name), None if fewer than two reads are under it."""
        covered = [i for i, r in enumerate(reads) if all(t in under[key] for t in r[1])]
        if len(covered) < 2:
            return None
        members = [t for i in covered for t in reads[i][1]]
        name, group_sizes = group(key, members)
        new = [r for i, r in enumerate(reads) if i not in covered[1:]]
        new[covered[0]] = [key, members, group_sizes, name]
        return new, len(covered) - 1, group_sizes[1] - sum(reads[i][2][1] for i in covered), name

    # reads: [key or None, members, (request, reply), read name], in the order they are sent; start plain
    reads = [[None, [tag], read_size(tag, info), tag] for tag in tags]  # sized as chunks() sizes them
    if together:  # outermost first; ignores whole_max_bytes and the cost rule, but must fit one packet
        fits = budget - 8 - VERSION_REPLY
        for key in sorted((k for k in sizes if len(under[k]) >= 2 and sizes[k] + 10 <= fits), key=levels.get):
            if together(names[key]):
                merged = merge(key)
                if merged:
                    reads = merged[0]
                candidates.discard(key)
    limit = packets([r[2] for r in reads], budget)
    while candidates:
        best = None
        for key in candidates:
            merged = merge(key)
            if merged is None:
                continue
            new, saved, extra, name = merged
            if extra > BYTES_PER_READ * saved or name.lower() in no_group:
                continue
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
    for key, members, _, name in reads:
        if key is None:
            result.append((name, [(name, None)]))
        elif key in runs:
            index = runs[key][2]
            start = min(index[t] for t in members)
            result.append((name, [(tag, [index[tag] - start]) for tag in members]))
        else:
            entries = []
            for tag in members:
                k = depth[tag][key]
                entries.append((tag, None if k == len(parsed[tag]) else [key_ for _, key_ in parsed[tag][k:]]))
            result.append((name, entries))
    return result


def chunks(reads, get_info, budget=PACKET_BYTES):
    """``reads`` (a plan) split into lists of about one packet each, packed as ``packets`` counts them."""
    groups, _ = _pack([read_size(name, get_info) for name, _ in reads], budget)
    return [[reads[i] for i in g] for g in groups]


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


def execute(plc, chunks, between=None):
    """Runs a plan split by ``chunks``: ({tag: Tag}, names of the group reads that failed, read services sent).
    One ``plc.read`` per chunk; after each chunk but the last, ``between({tag: Tag})`` gets that chunk's results,
    which are then left out of the returned ones. The members of failed group reads are read one by one after
    the last chunk, so no value is ever wrong. A plan that is not split is one chunk."""
    if chunks and isinstance(chunks[0], tuple):
        chunks = [chunks]
    out, retry, failed, services = {}, [], [], 0
    for n, chunk in enumerate(chunks):
        names = [name for name, _ in chunk]
        results = _as_list(plc.read(*names)) if names else []
        services += len(names)
        part = {}
        for (name, members), result in zip(chunk, results):
            if len(members) == 1 and members[0][1] is None:
                part[members[0][0]] = result
                continue
            if result.error is not None:
                failed.append(name)
                retry += [tag for tag, _ in members]
                continue
            for tag, path in members:
                if path is None:
                    part[tag] = result
                    continue
                try:
                    part[tag] = Tag(tag, dig(result.value, path), None, None)
                except (KeyError, IndexError, TypeError, StopIteration):
                    if name not in failed:
                        failed.append(name)
                    retry.append(tag)
        if between is not None and n < len(chunks) - 1:
            between(part)
        else:
            out.update(part)
    if retry:
        out.update(zip(retry, _as_list(plc.read(*retry))))
    return out, failed, services + len(retry)
