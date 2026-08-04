"""Discovers aw-workspace-cli commands from two places:

1. Built-in: ``src/cli/commands/*.py`` (this repo).
2. App-contributed: ``<apps_root()>/<slug>/commands/*.py`` (one per
   installed app). Each app owns and ships its own CLI surface in its own
   repo — nothing app-specific lives in this workspace's code, and a
   command shows up the moment the app is installed/updated, with no
   change here.

Both locations use the identical file contract: a plain module exposing
``COMMAND`` (str), ``DESCRIPTION`` (str), and ``run(args: list[str]) -> int``.
Shared here so both ``aw-workspace-cli`` (the entrypoint) and the ``help``
command list the exact same set.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil

import src.cli.commands as _builtin_commands_pkg
from src.apps.fetch import apps_root


def _load_app_command(slug: str, path: str):
    mod_name = f"_aw_app_command_{slug}_{os.path.splitext(os.path.basename(path))[0]}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — one broken app command must not break the CLI
        print(f"aw-workspace-cli: failed loading {slug}'s command "
              f"{os.path.basename(path)}: {exc}")
        return None
    return module


def discover_commands() -> dict:
    found = {}

    for _, name, _ in pkgutil.iter_modules(_builtin_commands_pkg.__path__):
        module = importlib.import_module(f"src.cli.commands.{name}")
        found[getattr(module, "COMMAND", name)] = module

    root = apps_root()
    if os.path.isdir(root):
        for slug in sorted(os.listdir(root)):
            cmd_dir = os.path.join(root, slug, "commands")
            if not os.path.isdir(cmd_dir):
                continue
            for fname in sorted(os.listdir(cmd_dir)):
                if not fname.endswith(".py") or fname.startswith("_"):
                    continue
                module = _load_app_command(slug, os.path.join(cmd_dir, fname))
                if module is None:
                    continue
                command = getattr(module, "COMMAND", None)
                if command:
                    found[command] = module

    return found
