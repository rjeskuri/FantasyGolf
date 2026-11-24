"""The fantasygolf command line. Later tasks add one Command entry to COMMANDS each."""

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fantasygolf import __version__


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    handler: Callable[[argparse.Namespace], int]
    configure: Callable[[argparse.ArgumentParser], None] = lambda parser: None


COMMANDS: list[Command] = []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fantasygolf", description="Fantasy golf projections.")
    parser.add_argument("--version", action="version", version=f"fantasygolf {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    for command in COMMANDS:
        command.configure(sub.add_parser(command.name, help=command.help))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = next((c for c in COMMANDS if c.name == args.command), None)
    if command is None:
        parser.print_help()
        return 2
    return command.handler(args)
