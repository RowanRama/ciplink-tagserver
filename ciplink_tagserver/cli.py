"""ciplink-tagserver run | dump-tags | types | service"""

import argparse
import json
import logging
import logging.handlers
from pathlib import Path
import re
import urllib.parse
import urllib.request

from . import rules
from .config import load_config
from .values import type_name


def run(args):
    import uvicorn

    from .server import create_app

    config = load_config(args.config)
    log_config = None if args.log_file else uvicorn.config.LOGGING_CONFIG  # with a log file, uvicorn logs go there too
    uvicorn.run(create_app(config, mock=args.mock), host=config.host, port=config.port, log_config=log_config)


def service(args):
    from . import service as svc

    config = Path(args.config).resolve()
    log_file = Path(args.log_file).resolve() if args.log_file else config.with_name("tagserver.log")
    if args.action == "install":
        return svc.install(args.name, config, log_file)
    if args.action == "remove":
        return svc.remove(args.name)
    if args.action == "status":
        return svc.status(args.name)
    args.mock, args.config, args.log_file = False, str(config), str(log_file)
    svc.supervise(lambda: run(args))  # "run": what the installed task executes


def setup_logging(log_file=None):
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers = [logging.handlers.RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def dump_tags(args):
    """Uploads the tag list of one PLC (reads only) and writes it in the mock_tags format."""
    from .server import driver_factory

    config = load_config(args.config)
    with driver_factory(config.plcs[args.plc]) as plc:
        tags = {name: type_name(info) for name, info in plc.tags.items()}
    with open(args.out, "w", encoding="utf-8") as file:
        json.dump(tags, file, indent=2)
    print(f"wrote {len(tags)} tags to {args.out}")


def fetch_tags(url, token, plc):
    """GET <url>/tags?plc=NAME; returns the whole body (plc, version, tags, types, rules, groups)."""
    query = f"/tags?plc={urllib.parse.quote(plc)}" if plc else "/tags"
    request = urllib.request.Request(url.rstrip("/") + query, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def tag_names(tags):
    """Readable names: each tag, but for an AOI its readable members (Inst.Member) instead of the instance."""
    names = []
    for tag in tags:
        if "aoi_readable" in tag:
            names += [f"{tag['name']}.{member}" for member in tag["aoi_readable"]]
        else:
            names.append(tag["name"])
    return names


INTS = {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT", "BYTE", "WORD", "LWORD"}
NUMBERS = INTS | {"REAL", "LREAL"}
RESERVED = {"bit", "length", "then", "toString", "toJSON"}  # the tree proxy answers these itself (amendment 31)
IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def ident(name):
    """A TS identifier from a PLC or UDT name: other characters become _."""
    name = re.sub(r"[^A-Za-z0-9_$]", "_", name)
    return name if IDENT.match(name) else "_" + name


def obj(fields, indent=None):
    """A TS object type; on one line when indent is None."""
    fields = [(k if IDENT.match(k) else json.dumps(k), v) for k, v in fields]
    if indent is None:
        return "{ " + " ".join(f"{k}: {v};" for k, v in fields) + " }" if fields else "{}"
    pad = " " * (indent + 2)
    return "{\n" + "".join(f"{pad}{k}: {v};\n" for k, v in fields) + " " * indent + "}"


def ts_bool(b):
    return "true" if b else "false"


class Scope(dict):
    """Tags under a dotted prefix (``Program:X``)."""


class TreeGen:
    """TS types for one PLC's /tags body. Paths go two ways: ``path`` with each index as ``[0]`` (what rules are
    matched against, amendment 36; None inside a shared interface) and ``meta`` with each index group as ``[]``."""

    def __init__(self, body):
        self.body, self.types, self.rules = body, body.get("types") or {}, body.get("rules") or []
        self.prefix = ident(body["plc"])
        self.dims, self.bits = {}, {}

    def rule(self, path, field):
        """The field of the first rule (file order) matching the path that sets it, else None."""
        if path is None:
            return None
        return next((r[field] for r in self.rules if field in r and rules.matches(r, path)), None)

    def leaf(self, type_, path, meta):
        """(TS type, path specific) of an atomic, STRING or unknown leaf."""
        w = bool(self.rule(path, "writable"))
        if type_ not in INTS and type_ != "DWORD":
            value = {"BOOL": "boolean", "STRING": "string"}.get(type_, "number" if type_ in NUMBERS else "unknown")
            return f"TagRef<{value}, {ts_bool(w)}>", w
        text = f"IntRef<{ts_bool(w)}>" if type_ != "DWORD" else \
            f"TagRef<boolean[], {ts_bool(w)}> & {{ bit(n: number): TagRef<boolean, {ts_bool(w)}> }}"
        bits = self.rule(path, "bits")
        if not bits:
            return text, w
        if meta is not None:
            self.bits[meta] = dict(bits)
        aliases = []
        for name, n in bits.items():  # a bit's own rules first, else its word's writable
            bw = self.rule(f"{path}.{n}", "writable")
            aliases.append((name, f"TagRef<boolean, {ts_bool(w if bw is None else bw)}>"))
        return f"{text} & {obj(aliases)}", True

    def node(self, type_, dims, path, meta, indent):
        """(TS type, path specific) of a tag or member. Path specific (a writable path or bit names somewhere
        below) means it is typed inline; otherwise a UDT uses its shared interface."""
        if dims:
            if meta is not None:
                self.dims[meta] = list(dims)
            sub = None if path is None else f"{path}[{','.join('0' * len(dims))}]"
            inner, special = self.node(type_, [], sub, None if meta is None else meta + "[]", indent)
            for n in reversed(dims[1:]):
                inner = f"TagArray<{inner}, {n}>"
            # not a ref: a bare array name reads and writes its first element only (ciplink and the mock)
            return f"TagArray<{inner}, {dims[0]}>", special
        if type_ not in self.types:
            return self.leaf(type_, path, meta)
        udt, fields, special = self.types[type_], [], False
        for name, member in udt["members"].items():
            if name not in RESERVED:
                text, s = self.node(member.get("type", ""), member.get("dims"),
                                    None if path is None else f"{path}.{name}",
                                    None if meta is None else f"{meta}.{name}", indent + 2)
                fields.append((name, text))
                special |= s
        w = bool(self.rule(path, "writable"))
        if not (special or w):
            return f"{self.prefix}_{ident(type_)}", False
        head = "" if udt.get("aoi") else f"TagRef<Record<string, unknown>, {ts_bool(w)}> & "  # an AOI is never read whole
        return head + obj(fields, indent), True

    def interfaces(self):
        out = []
        for name, udt in self.types.items():
            fields = [(m, self.node(info.get("type", ""), info.get("dims"), None, None, 0)[0])
                      for m, info in udt["members"].items() if m not in RESERVED]
            base = "" if udt.get("aoi") else " extends TagRef<Record<string, unknown>, false>"
            out.append(f"export interface {self.prefix}_{ident(name)}{base} {obj(fields, 0)}\n")
        return out

    def tags(self):
        """The ``<PLC>Tags`` interface; a dotted tag name (``Program:X.Tag``) nests under its prefix."""
        root = Scope()
        for tag in self.body["tags"]:
            *scopes, last = tag["name"].split(".")
            level = root
            for scope in scopes:
                level = level.setdefault(scope, Scope())
                if not isinstance(level, Scope):
                    break
            else:
                level.setdefault(last, tag)

        def emit(level, prefix, indent):
            fields = []
            for name, item in level.items():
                path = prefix + name
                if name in RESERVED:
                    continue
                if isinstance(item, Scope):
                    fields.append((name, emit(item, path + ".", indent + 2)))
                elif "aoi_readable" in item and item.get("type") not in self.types:  # AOI type not listed
                    fields.append((name, obj([(m, self.leaf("", f"{path}.{m}", None)[0])
                                              for m in item["aoi_readable"]], indent + 2)))
                else:
                    fields.append((name, self.node(item.get("type", ""), item.get("dims"), path, path, indent + 2)[0]))
            return obj(fields, indent)

        return f"export interface {self.prefix}Tags {emit(root, '', 0)}\n"

    def meta(self):
        """tagTree meta: dims of every array path, bit names of integer paths. Call after tags()."""
        meta = {k: v for k, v in (("dims", self.dims), ("bits", self.bits)) if v}
        return json.dumps(meta) if meta else "{}"


def tree_module(bodies, qualified=False):
    """The generated module: UDT interfaces, a tags interface and a tagTree const per PLC."""
    parts, consts = [], []
    for body in bodies:
        gen = TreeGen(body)
        parts += gen.interfaces()
        parts.append(gen.tags())
        plc = json.dumps(body["plc"]) if qualified else "null"
        consts.append(f"export const {gen.prefix} = tagTree<{gen.prefix}Tags>({plc}, {gen.meta()});\n")
    code = "\n".join(parts) + "\n" + "".join(consts)
    used = [t for t in ("TagRef", "IntRef", "TagArray") if re.search(rf"\b{t}<", code)]
    versions = ", ".join(f"{b['plc']} tag table version {b.get('version')}" for b in bodies)
    imports = ", ".join(["tagTree"] + [f"type {t}" for t in used])
    return (f"// Generated by ciplink-tagserver types ({versions}). Do not edit.\n"
            f'import {{ {imports} }} from "@ciplink/react";\n\n{code}')


def types(args):
    """Writes a TypeScript module typing the tags of a running tag server, or (--format names) a name union."""
    plcs = args.plc or [None]
    bodies = [fetch_tags(args.url, args.token, plc) for plc in plcs]
    if args.format == "tree":
        text = tree_module(bodies, qualified=len(plcs) > 1)
        summary = f"wrote the tag tree of {', '.join(b['plc'] for b in bodies)}"
    else:
        names = []
        for plc, body in zip(plcs, bodies):
            prefix = f"{plc}/" if len(plcs) > 1 else ""
            names += [prefix + name for name in tag_names(body["tags"])]
        names = list(dict.fromkeys(names))
        lines = "".join(f"\n  | {json.dumps(name)}" for name in names) or " never"
        text = f"// Generated by ciplink-tagserver types. Do not edit.\nexport type TagName ={lines};\n"
        summary = f"wrote {len(names)} tag names"
    with open(args.out, "w", encoding="utf-8") as file:
        file.write(text)
    print(f"{summary} to {args.out}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ciplink-tagserver")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("run", help="serve tag values over a WebSocket")
    p.add_argument("--config", default="tagserver.toml")
    p.add_argument("--mock", action="store_true", help="serve fake values from each PLC's mock_tags file")
    p.add_argument("--log-file", help="write logs to this file (rotated at 10 MB, 5 kept) instead of the console")
    p.set_defaults(func=run)

    p = commands.add_parser("service", help="run at Windows startup (install/remove need an administrator terminal)")
    p.add_argument("action", choices=["install", "remove", "status", "run"])
    p.add_argument("--config", default="tagserver.toml")
    p.add_argument("--name", default="ciplink-tagserver", help="Task Scheduler task name")
    p.add_argument("--log-file", help="default: tagserver.log next to the config")
    p.set_defaults(func=service)

    p = commands.add_parser("dump-tags", help="write a PLC's tag list as a mock_tags file")
    p.add_argument("--config", default="tagserver.toml")
    p.add_argument("--plc", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=dump_tags)

    p = commands.add_parser("types", help="write the typed tag tree of a running server as a TypeScript module")
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--token", required=True)
    p.add_argument("--plc", action="append", help="repeat for several PLCs; names are then qualified as PLC/tag")
    p.add_argument("--format", choices=["tree", "names"], default="tree",
                   help="tree: interfaces and a tagTree const per PLC; names: the 0.2 union of tag names")
    p.add_argument("--out", required=True)
    p.set_defaults(func=types)

    args = parser.parse_args(argv)
    setup_logging(getattr(args, "log_file", None))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
