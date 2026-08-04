"""aw-workspace-cli help — list available commands."""
from __future__ import annotations

COMMAND = "help"
DESCRIPTION = "Show this help message"


def run(args: list[str]) -> int:
    from src.cli.discovery import discover_commands

    entries = sorted(
        (command, getattr(module, "DESCRIPTION", ""))
        for command, module in discover_commands().items()
    )
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
