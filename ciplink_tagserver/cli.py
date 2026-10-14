"""ciplink-tagserver run | dump-tags"""

import argparse
import json
import logging

from .config import load_config
from .values import type_name


def run(args):
    import uvicorn

    from .server import create_app

    config = load_config(args.config)
    uvicorn.run(create_app(config, mock=args.mock), host=config.host, port=config.port)


def dump_tags(args):
    """Uploads the tag list of one PLC (reads only) and writes it in the mock_tags format."""
    from .server import driver_factory

    config = load_config(args.config)
    with driver_factory(config.plcs[args.plc]) as plc:
        tags = {name: type_name(info) for name, info in plc.tags.items()}
    with open(args.out, "w", encoding="utf-8") as file:
        json.dump(tags, file, indent=2)
    print(f"wrote {len(tags)} tags to {args.out}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ciplink-tagserver")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("run", help="serve tag values over a WebSocket")
    p.add_argument("--config", default="tagserver.toml")
    p.add_argument("--mock", action="store_true", help="serve fake values from each PLC's mock_tags file")
    p.set_defaults(func=run)

    p = commands.add_parser("dump-tags", help="write a PLC's tag list as a mock_tags file")
    p.add_argument("--config", default="tagserver.toml")
    p.add_argument("--plc", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=dump_tags)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
