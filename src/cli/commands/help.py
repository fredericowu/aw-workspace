"""aw-workspace help — list available commands."""
from __future__ import annotations

COMMAND = "help"
DESCRIPTION = "Show this help message"


def run(args: list[str]) -> int:
    import importlib
    import pkgutil

    import src.cli.commands as commands_pkg

    print("aw-workspace CLI\n")
    print("Usage: aw-workspace <command> [args...]\n")
    print("Commands:")
    for _, name, _ in sorted(pkgutil.iter_modules(commands_pkg.__path__)):
        module = importlib.import_module(f"src.cli.commands.{name}")
        command = getattr(module, "COMMAND", name)
        description = getattr(module, "DESCRIPTION", "")
        print(f"  {command:<14} {description}")
    return 0
