"""aw-workspace-cli help — list available commands."""
from __future__ import annotations

COMMAND = "help"
DESCRIPTION = "Show this help message"


def run(args: list[str]) -> int:
    import importlib
    import pkgutil

    import src.cli.commands as commands_pkg

    entries = []
    for _, name, _ in sorted(pkgutil.iter_modules(commands_pkg.__path__)):
        module = importlib.import_module(f"src.cli.commands.{name}")
        command = getattr(module, "COMMAND", name)
        description = getattr(module, "DESCRIPTION", "")
        entries.append((command, description))
    width = max((len(c) for c, _ in entries), default=0)

    print("aw-workspace-cli")
    print("This workspace's own CLI — not to be confused with the monolith's ./aw.\n")
    print("Usage:")
    print("  aw-workspace-cli <command> [args...]")
    print("  aw-workspace-cli help")
    print("  aw-workspace-cli <command> --help   (per-command usage and flags)\n")
    print("Commands:")
    for command, description in entries:
        print(f"  {command:<{width}}   {description}")
    print("\nRun 'aw-workspace-cli <command> --help' for a command's full usage.")
    return 0
