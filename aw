#!/usr/bin/env python3
"""
aw — aw-workspace CLI. Loosely inspired by the agentic-workspace ``./aw``
launcher (auto-discovered commands under a ``commands/`` package), but this
one is scoped to a single BYOD workspace instead of a whole sandbox: no
service lifecycle (that's the container runtime's job), no bootstrap venv
(the image ships its deps baked in) — just workspace-shaped actions.

Commands are auto-discovered from ``src/cli/commands/*.py``. Drop a new file
there with ``COMMAND``, ``DESCRIPTION``, and ``run(args) -> int`` and it
becomes available immediately.

Usage:
    ./aw <command> [args...]
    ./aw help
"""
import importlib
import os
import pkgutil
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import src.cli.commands as commands_pkg  # noqa: E402


def _discover_commands() -> dict:
    found = {}
    for _, name, _ in pkgutil.iter_modules(commands_pkg.__path__):
        module = importlib.import_module(f"src.cli.commands.{name}")
        command = getattr(module, "COMMAND", name)
        found[command] = module
    return found


def main() -> int:
    commands = _discover_commands()

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.argv = ["help"] + sys.argv[2:]

    name, rest = sys.argv[1], sys.argv[2:]
    module = commands.get(name)
    if module is None:
        print(f"./aw: unknown command '{name}'")
        print("Run './aw help' to list available commands.")
        return 1

    return module.run(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
