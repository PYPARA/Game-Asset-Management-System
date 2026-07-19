"""Command-line interface for the Emperor Simulator adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .adapter import dry_run, export_manifest, import_to
from .errors import AdapterError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emperor-adapter")
    subcommands = parser.add_subparsers(dest="command", required=True)

    preview = subcommands.add_parser("dry-run", help="inspect without writing")
    preview.add_argument("root", type=Path)
    preview.add_argument("--compact", action="store_true")

    importer = subcommands.add_parser("import", help="write .game-assets metadata")
    importer.add_argument("root", type=Path)
    importer.add_argument("destination", type=Path)

    exporter = subcommands.add_parser("export", help="render a compatible manifest")
    exporter.add_argument("source", type=Path)
    exporter.add_argument("-o", "--output", type=Path)
    exporter.add_argument("--format", choices=("json", "typescript", "ts"), default="json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "dry-run":
            result = dry_run(args.root)
            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=None if args.compact else 2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "import":
            print(json.dumps(import_to(args.root, args.destination), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        rendered = export_manifest(args.source, output=args.output, format=args.format)
        if args.output is None:
            sys.stdout.write(rendered)
        return 0
    except (AdapterError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

