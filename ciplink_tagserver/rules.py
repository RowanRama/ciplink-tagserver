"""Tag patterns and the TOML rules and poll groups that match them."""

import logging
import re
from functools import lru_cache

log = logging.getLogger(__name__)

RULE_KEYS = ("writable", "min", "max", "units", "description", "deadband", "always", "bits", "together")
NOT_FOR_BITS = {"min", "max", "deadband", "units", "bits", "together"}
BIT = re.compile(r"^(.+)\.(\d+)$")  # X.<n>: Logix member names never start with a digit


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


class Rules:
    """The rules and poll groups of every PLC in a Config."""

    memo_max = 10000

    def __init__(self, config):
        self.config = config
        self._memo = {}

    def _merged(self, plc, tag) -> dict:
        result = {}
        for rule in self.config.plcs[plc].rules:
            if matches(rule, tag):
                for key in RULE_KEYS:
                    if key in rule and key not in result:
                        result[key] = rule[key]
        return result

    def effective(self, plc: str, tag: str) -> dict:
        """Merged rule fields for a tag path in the PLC: per field, the first rule (file order) that sets it."""
        key = (plc, tag.lower())
        if key not in self._memo:
            if len(self._memo) >= self.memo_max:
                self._memo.clear()
            result = self._merged(plc, tag)
            bit = BIT.match(tag)
            if bit:  # a bit takes only writable from its word
                result = {k: v for k, v in result.items() if k not in NOT_FOR_BITS}
                if "writable" not in result and "writable" in (word := self.effective(plc, bit[1])):
                    result["writable"] = word["writable"]
            self._memo[key] = result
        return self._memo[key]

    def resolve(self, plc: str, tag: str) -> str:
        """A bit name ``X.EStop`` as ``X.<n>`` from the bits of X's rules; anything else unchanged."""
        if "." not in tag or BIT.match(tag):
            return tag
        word, name = tag.rsplit(".", 1)
        bits = self.effective(plc, word).get("bits") or {}
        for bit_name, n in bits.items():
            if bit_name.lower() == name.lower():
                return f"{word}.{n}"
        return tag

    def group(self, plc: str, tag: str) -> dict | None:
        """The first poll group matching the tag, None if none does."""
        return next((g for g in self.config.plcs[plc].groups if matches(g, tag)), None)

    def groups(self, plc: str) -> list:
        return self.config.plcs[plc].groups

    def always(self, plc: str) -> list:
        """Tags read at tier 1 even with no subscribers: literal names of rules whose effective ``always`` is true."""
        result = []
        for rule in self.config.plcs[plc].rules:
            if not rule.get("always"):
                continue
            for name in patterns(rule):
                if "*" in name or "?" in name:
                    log.warning("%s: always ignored for the pattern %r, it takes tag names only", plc, name)
                elif self.effective(plc, name).get("always") and name not in result:
                    result.append(name)
        return result
