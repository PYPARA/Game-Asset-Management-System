#!/usr/bin/env python3
"""Run the repeatable M6 v1 release acceptance rehearsal.

The default run is offline and uses a loopback OpenAI-compatible protocol
fixture.  It still exercises the real HTTP adapter and all response classes;
no provider key or network access is needed.  Pass ``--provider-url`` and
``--api-key`` (or ``GAME_ASSETS_M6_API_KEY``) to add a live provider contract
check.  The command never runs Git commands and never writes outside the
requested report path and disposable temporary fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from game_assets_api.m6_acceptance import run_m6_acceptance, write_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("m6-acceptance.json"),
        help="structured JSON receipt path (default: ./m6-acceptance.json)",
    )
    parser.add_argument(
        "--provider-url",
        help="optional real OpenAI-compatible provider base URL (for live contract check)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GAME_ASSETS_M6_API_KEY"),
        help="optional live provider key; prefer GAME_ASSETS_M6_API_KEY",
    )
    parser.add_argument(
        "--require-live-provider",
        action="store_true",
        help="fail the rehearsal when a live provider URL and key are not supplied",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = run_m6_acceptance(
        live_provider_url=arguments.provider_url,
        live_provider_api_key=arguments.api_key,
        require_live_provider=arguments.require_live_provider,
    )
    output = write_report(report, arguments.output)
    print(json.dumps({"output": str(output), **report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
