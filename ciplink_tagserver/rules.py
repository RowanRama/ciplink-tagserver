"""Tag settings: decorators attached by UDT member or tag path, then the match-pattern rules and poll groups."""

import itertools
import logging
import re
from functools import lru_cache

log = logging.getLogger(__name__)

RULE_KEYS = ("writable", "min", "max", "units", "description", "deadband", "always", "bits", "together")
DECORATOR_KEYS = RULE_KEYS + ("rate_ms",)
INHERITED = ("rate_ms", "together")  # the only fields a struct or array passes down to its members
NOT_FOR_BITS = {"min", "max", "deadband", "units", "bits", "together"}
BIT = re.compile(r"^(.+)\.(\d+)$")  # X.<n>: Logix member names never start with a digit
INDEX = re.compile(r"\[([^\]]*)\]")
_PART = re.compile(r"\[[^\]]*\]|[^.\[\]]+")
_warned = set()  # (where, key): the config is read more than once


def canonical(match):
    """[02] and [ 2] address element 2 in ciplink and the mock (int()), so settings must see [2]."""
    try:
        return "[" + ",".join(str(int(i)) for i in match[1].split(",")) + "]"
    except ValueError:
        return match[0]


def normalize(path: str) -> str:
    return INDEX.sub(canonical, path).lower()


@lru_cache(maxsize=None)
def _regex(pattern: str):
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern),
                      re.IGNORECASE | re.DOTALL)


def match(pattern: str, name: str) -> bool:
    """Case-insensitive; ``*`` any run of characters (``.`` and ``[]`` included), ``?`` one, the rest literal."""
    return _regex(pattern).fullmatch(name) is not None


def patterns(table: dict) -> list:
    """The ``match`` of a rule or group table as a list."""
    m = table.get("match", [])
    return [m] if isinstance(m, str) else list(m)


def matches(table: dict, name: str) -> bool:
    return any(match(p, name) for p in patterns(table))


def fields(table: dict, where: str) -> dict:
    """The decorator fields of a table; unknown keys are logged and dropped, a bad rate_ms is an error."""
    for key in table:
        if key not in DECORATOR_KEYS and (where, key) not in _warned:
            _warned.add((where, key))
            log.warning("%s: unknown key %r ignored", where, key)
    rate = table.get("rate_ms")
    if "rate_ms" in table and (not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0):
        raise ValueError(f"{where}: rate_ms must be an integer > 0")
    return {k: v for k, v in table.items() if k in DECORATOR_KEYS}


def entry(value, decorators: dict, where: str) -> dict:
    """The fields of a [udt] or [tags] entry: "name", ["name", ...] or {use = ..., <fields>}. Its decorators in
    list order (later wins), then its own fields; ``rate_ms`` comes with ``group``, the name that set it."""
    own = {}
    if isinstance(value, dict):
        use = value.get("use", [])
        own = fields({k: v for k, v in value.items() if k != "use"}, where)
        value = use
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(f"{where}: expected a decorator name, a list of names or a table")
    result = {}
    for name in names:
        if name not in decorators:
            raise ValueError(f"{where}: unknown decorator {name!r}")
        result.update(decorators[name])
        if "rate_ms" in decorators[name]:
            result["group"] = name
    result.update(own)
    if "rate_ms" in own:
        result["group"] = "tag"
    return result


class Resolver:
    """Effective settings of tag paths in one PLC, from the decorators, its [udt] and [tags] entries, its rules
    and groups, and the controller's types (the /tags ``tags`` and ``types``; UDT entries need them)."""

    def __init__(self, decorators=None, udt=None, tags=None, rules=(), groups=(), where="plc"):
        decorators = {n: fields(d, f"decorator.{n}") for n, d in (decorators or {}).items()}
        self.udt = {u.lower(): {m.lower(): entry(e, decorators, f"{where}.udt.{u}.{m}") for m, e in members.items()}
                    for u, members in (udt or {}).items()}
        self.tags = {normalize(k): entry(e, decorators, f"{where}.tags.{k}") for k, e in (tags or {}).items()}
        self.names = {normalize(k): INDEX.sub(canonical, k) for k in (tags or {})}
        self.rules, self.groups = list(rules), list(groups)
        self.set_types(None, None)

    @classmethod
    def from_body(cls, body):
        """From a GET /tags body, as codegen sees it."""
        resolver = cls(body.get("decorators"), body.get("udt"), body.get("tag_settings"), body.get("rules") or [],
                       body.get("groups") or [], body.get("plc", "plc"))
        resolver.set_types(body.get("tags"), body.get("types"))
        return resolver

    def set_types(self, tag_list, types):
        """The controller structure (None: unknown, so only [tags] entries and rules apply)."""
        self.tag_types = {t["name"].lower(): (t["name"], (t.get("type") or "").lower(), t.get("dims"))
                          for t in tag_list or []}
        self.types = {u.lower(): {m.lower(): (m, (i.get("type") or "").lower(), i.get("dims"))
                                  for m, i in t.get("members", {}).items()} for u, t in (types or {}).items()}

    def _nodes(self, tag):
        """[(kind, fields set at that node)] from the base tag to the leaf; kind: tag, member, element or bit."""
        prefix, rest = "", tag
        if tag[:8].lower() == "program:":
            dot = tag.find(".")
            prefix, rest = (tag + ".", "") if dot < 0 else (tag[:dot + 1], tag[dot + 1:])
        parts = _PART.findall(rest)
        if not parts:
            return [("tag", self.tags.get(normalize(tag), {}))]
        path = prefix + parts[0]
        type_ = self.tag_types.get(path.lower(), (None, None))[1]
        result = [("tag", self.tags.get(normalize(path), {}))]
        for i, part in enumerate(parts[1:], 2):
            udt = None
            if part.startswith("["):
                kind, path = "element", path + part
            elif part.isdigit() and i == len(parts):
                kind, path, type_ = "bit", f"{path}.{part}", None
            else:
                kind, path, udt = "member", f"{path}.{part}", type_
                type_ = self.types.get(udt, {}).get(part.lower(), (None, None))[1]
            key = normalize(path)
            found = dict(self.udt.get(udt, {}).get(part.lower(), {})) if udt else {}
            stripped = INDEX.sub("", key)
            if stripped != key:
                found.update(self.tags.get(stripped, {}))
            found.update(self.tags.get(key, {}))
            result.append((kind, found))
        return result

    def effective(self, tag: str) -> dict:
        """Settings of a tag path: rate_ms and together from the closest node that sets them, the other fields
        from the path itself (an element also from its array); then the rules, per field the first rule (file
        order) that sets it. A bit keeps no min, max, units, deadband, bits or together; writable falls back to
        its word's."""
        nodes = self._nodes(tag)
        result = {}
        for _, found in nodes:
            for key in INHERITED:
                if key in found:
                    result[key] = found[key]
                    if key == "rate_ms":
                        result["group"] = found["group"]
        chain, i = [nodes[-1]], len(nodes) - 1
        while nodes[i][0] == "element":
            i -= 1
            chain.insert(0, nodes[i])
        for _, found in chain:
            result.update({k: v for k, v in found.items() if k not in INHERITED and k != "group"})
        for rule in self.rules:
            if matches(rule, tag):
                for key in RULE_KEYS:
                    if key in rule and key not in result:
                        result[key] = rule[key]
        bit = BIT.match(tag)
        if bit:
            result = {k: v for k, v in result.items() if k not in NOT_FOR_BITS}
            if "writable" not in result and "writable" in (word := self.effective(bit[1])):
                result["writable"] = word["writable"]
        return result

    def group(self, tag, settings=None) -> dict | None:
        """{name, rate_ms} of the tag's poll rate: from a decorator (named after it, "tag" when set inline), else
        the first matching poll group; None if neither sets one."""
        settings = self.effective(tag) if settings is None else settings
        if "rate_ms" in settings:
            return {"name": settings["group"], "rate_ms": settings["rate_ms"]}
        return next((g for g in self.groups if matches(g, tag)), None)

    def always(self) -> list:
        """Tags read at tier 1 even with no subscribers: literal rule names and [tags] keys, and every instance
        of a UDT member whose entry says always (once the types are known)."""
        result = []
        for rule in self.rules:
            if rule.get("always"):
                for name in patterns(rule):
                    if "*" in name or "?" in name:
                        log.warning("always ignored for the pattern %r, it takes tag names only", name)
                    else:
                        result.append(name)
        result += [p for k, found in self.tags.items() if found.get("always") for p in self._expand(self.names[k])]
        result += self._instances({(u, m) for u, members in self.udt.items()
                                   for m, found in members.items() if found.get("always")})
        return [name for name in dict.fromkeys(result) if self.effective(name).get("always")]

    def _expand(self, key) -> list:
        """The paths an index-less [tags] key stands for: every element of each array it passes through (by the
        types; unknown, the key itself). A key with indices, or one ending at an array, is the path itself."""
        prefix, rest = "", key
        if key[:8].lower() == "program:":
            dot = key.find(".")
            prefix, rest = (key + ".", "") if dot < 0 else (key[:dot + 1], key[dot + 1:])
        parts = _PART.findall(rest)
        found = self.tag_types.get((prefix + parts[0]).lower()) if parts else None
        if "[" in key or found is None:
            return [key]
        paths, (_, type_, dims) = [found[0]], found
        for part in parts[1:]:
            if dims:
                paths = [f"{p}[{','.join(map(str, i))}]" for p in paths
                         for i in itertools.product(*(range(n) for n in dims))]
            name, type_, dims = self.types.get(type_, {}).get(part.lower(), (part, None, None))
            paths = [f"{p}.{name}" for p in paths]
        return paths

    def _instances(self, targets) -> list:
        """Paths of every instance of the (udt, member) targets in the controller, array elements expanded."""
        reach = {}

        def reaches(type_):
            if type_ not in reach:
                reach[type_] = False  # guards recursive types
                reach[type_] = any((type_, m) in targets or reaches(t)
                                   for m, (_, t, _) in self.types.get(type_, {}).items())
            return reach[type_]

        found = []

        def walk(path, type_, dims):
            if dims:
                for index in itertools.product(*(range(n) for n in dims)):
                    walk(f"{path}[{','.join(map(str, index))}]", type_, None)
                return
            for m, (name, t, member_dims) in self.types.get(type_, {}).items():
                if (type_, m) in targets:
                    found.append(f"{path}.{name}")
                if reaches(t):
                    walk(f"{path}.{name}", t, member_dims)

        if targets:
            for name, type_, dims in self.tag_types.values():
                if reaches(type_):
                    walk(name, type_, dims)
        return found


class Rules:
    """The resolvers of every PLC in a Config, memoized; set_types clears the memo."""

    memo_max = 10000

    def __init__(self, config):
        self.config = config
        self.resolvers = {name: Resolver(config.decorators, plc.udt, plc.tags, plc.rules, plc.groups, f"plc.{name}")
                          for name, plc in config.plcs.items()}
        self._memo = {}

    def set_types(self, plc: str, tag_list, types):
        """The PLC's controller structure, after a connect or a tag table reload."""
        self.resolvers[plc].set_types(tag_list, types)
        self._memo = {}

    def effective(self, plc: str, tag: str) -> dict:
        """See Resolver.effective."""
        key = (plc, tag.lower())
        memo = self._memo
        if key not in memo:
            if len(memo) >= self.memo_max:
                memo.clear()
            memo[key] = self.resolvers[plc].effective(tag)
        return memo[key]

    def resolve(self, plc: str, tag: str) -> str:
        """A bit name ``X.EStop`` as ``X.<n>`` from the bits of X; anything else unchanged."""
        if "." not in tag or BIT.match(tag):
            return tag
        word, name = tag.rsplit(".", 1)
        bits = self.effective(plc, word).get("bits") or {}
        for bit_name, n in bits.items():
            if bit_name.lower() == name.lower():
                return f"{word}.{n}"
        return tag

    def group(self, plc: str, tag: str) -> dict | None:
        return self.resolvers[plc].group(tag, self.effective(plc, tag))

    def groups(self, plc: str) -> list:
        return self.config.plcs[plc].groups

    def always(self, plc: str) -> list:
        return self.resolvers[plc].always()
