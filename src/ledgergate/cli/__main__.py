"""Entry point for the ``ledgergate`` command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ledgergate import __version__


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="ledgergate",
        description="Prove an agent respects financial state machines before deployment.",
    )
    parser.add_argument("--version", action="version", version=f"ledgergate {__version__}")
    parser.set_defaults(handler=None)

    sub = parser.add_subparsers(dest="command", metavar="{run,verify,record,report}")
    for name, help_text in (
        ("run", "run an agent against the corpus and score it"),
        ("verify", "verify an existing trace against the corpus"),
        ("record", "record a cassette from a live agent run"),
        ("report", "render a result.json into markdown, junit or sarif"),
    ):
        sub.add_parser(name, help=help_text)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    print(f"'{args.command}' is not implemented yet (milestone M3).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
