"""Reads the TOML config file."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PlcConfig:
    name: str
    path: str
    tag_cache_file: str | None = None
    mock_tags: str | None = None
    rules: list[dict] = field(default_factory=list)  # [[plc.X.rule]] tables in file order, see rules.py
    groups: list[dict] = field(default_factory=list)  # [[plc.X.group]] tables: name, rate_ms, match


@dataclass
class TokenConfig:
    name: str
    secret: str
    write: list[str] = field(default_factory=list)  # rules.match patterns on qualified names


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    cycle_ms: int = 250
    audit_log: str = "audit.log"
    tier2_period_s: float = 3  # each tier-2 tag is read about this often
    grace_s: float = 10  # a tag leaving tier 1 stays at tier 2 this long
    pages_file: str | None = "pages.json"  # learned page -> tags, None to keep them in memory only
    planner: bool = True  # read structs whole when that saves reads
    whole_max_bytes: int = 1500  # largest struct the planner reads whole
    momentary_timeout_ms: int = 1000  # a pressed momentary tag with no hold this long is released
    plcs: dict[str, PlcConfig] = field(default_factory=dict)
    tokens: dict[str, TokenConfig] = field(default_factory=dict)

    def qualify(self, name: str) -> tuple[str, str]:
        """Splits ``<plc>/<tag>`` into (plc, tag); an unqualified name means the only PLC, if there is one."""
        if "/" in name:
            plc, tag = name.split("/", 1)
            if plc not in self.plcs:
                raise ValueError(f"unknown PLC {plc!r}")
        elif len(self.plcs) == 1:
            plc, tag = next(iter(self.plcs)), name
        else:
            raise ValueError(f"tag {name!r} must be qualified as <plc>/<tag>")
        if not tag:
            raise ValueError(f"empty tag name in {name!r}")
        return plc, tag


def load_config(path) -> Config:
    path = Path(path)
    with open(path, "rb") as file:
        return parse_config(tomllib.load(file), path.parent)


def parse_config(data: dict, base_dir=".") -> Config:
    """Builds a Config from the parsed TOML; relative file paths are relative to ``base_dir``."""
    base = Path(base_dir)

    def resolve(value):
        return str(base / value) if value else None

    server = data.get("server", {})
    config = Config(
        host=server.get("host", "127.0.0.1"),
        port=server.get("port", 8765),
        cycle_ms=server.get("cycle_ms", 250),
        audit_log=resolve(server.get("audit_log", "audit.log")),
        tier2_period_s=server.get("tier2_period_s", 3),
        grace_s=server.get("grace_s", 10),
        pages_file=resolve(server.get("pages_file", "pages.json")),
        planner=server.get("planner", True),
        whole_max_bytes=server.get("whole_max_bytes", 1500),
        momentary_timeout_ms=server.get("momentary_timeout_ms", 1000),
    )
    for name, plc in data.get("plc", {}).items():
        config.plcs[name] = PlcConfig(
            name=name,
            path=plc["path"],
            tag_cache_file=resolve(plc.get("tag_cache_file")),
            mock_tags=resolve(plc.get("mock_tags")),
            rules=list(plc.get("rule", [])),
            groups=list(plc.get("group", [])),
        )
        for group in config.plcs[name].groups:
            rate = group.get("rate_ms")
            if not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0 or "name" not in group:
                raise ValueError(f"plc.{name}.group needs a name and an integer rate_ms > 0: {group}")
    for name, token in data.get("token", {}).items():
        config.tokens[name] = TokenConfig(name=name, secret=token["secret"], write=list(token.get("write", [])))
    return config
