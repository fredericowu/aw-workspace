"""Generate the ``cli_reference/`` and ``skills/`` knowledge-base content.

Card ``quality:procedural-genre-absent-from-both-knowledge-indexes``: both
the ``kb`` pgvector store and the aw-knowledgeable graph measured ~83%
auto-generated code maps plus ~17% Notion cards — no procedural "how do I"
genre at all, so three ordinary operational questions scored 0/3 on both
sides even though the answers were two ``--help`` calls away. This module
produces that missing genre from two sources that already exist on disk and
cost no LLM tokens to capture:

* ``aw-workspace-cli <command> [<subcommand>] --help`` — one document per
  command, walking the SAME dynamic tree ``src/cli/discovery.py`` builds
  (built-in ``src/cli/commands/*.py`` plus every installed app's own
  ``commands/*.py``), so a new command shows up here with no change to this
  file.
* ``skills/<name>/SKILL.md`` — the materialized skill tree (AGENTS.md:
  ``skills/`` is generated, the sources are ``native-skills/<name>/`` and
  each app's ``contributes.skills``) is procedural prose written for exactly
  this purpose, and was previously indexed only via the separate
  ``search_skills`` surface — never as content an ordinary knowledge search
  would ever surface.

A CLI command's ``--help`` capture is split by owner, read off
``discover_commands()``'s ``__aw_app_id__`` stamp (``src/cli/discovery.py``):
a built-in lands in this repo's own committed ``docs/cli/`` (checksum-gated
writes, so a regeneration with no real ``--help`` change produces no git
diff), an app-contributed command lands in
``<AW_WORKSPACE_HOME>/knowledge_base/apps/<app-id>/cli_reference/`` — the
exact tree ``kb_app.kb_ops._build`` walks for the pgvector store (any ``.md``
anywhere under ``KB_DIR``, see ``src/apps/runtime.py``'s ``$AW_KB_DIR`` mount
comment), so a plain ``aw-workspace-cli knowledge-base --build`` after this
picks them up with no further change on that side. Both halves delete a
capture whose command disappeared — an uninstalled app's old ``--help`` page
must not outlive the app. ``skills/`` (below) keeps writing straight into
``<AW_WORKSPACE_HOME>/knowledge_base/skills/*.md`` as before. The
aw-knowledgeable graph side additionally needs its own bucket declared
(``BUCKET_ORDER`` in aw-app-knowledgeable's ``bulk_ingest.py``), since that
driver only scans a fixed allowlist of top-level subtrees — that edit lives
in that app's own repo, not here.

Wired to a scheduled task (see ``aw-workspace-cli kb-reference``) rather than
into ``agent_sync.sync_all()`` / the boot path: capturing ``--help`` for
every discovered command shells out once per command/subcommand, which is
cheap in isolation but would add a user-visible delay to every boot and to
every manual ``agent sync`` — a periodic tick (this content changes only
when a command's flags change) is the better fit than "redo it on every
server start."
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.apps import paths
from src.cli.discovery import discover_commands
from src.libs import skills_sync

log = logging.getLogger(__name__)

CLI_BIN = "aw-workspace-cli"
_HELP_TIMEOUT = 30


def kb_dir() -> Path:
    """``<AW_WORKSPACE_HOME>/knowledge_base`` — the same tree ``kb_app.kb_ops``
    indexes and aw-knowledgeable's bulk-ingest driver reads (its own
    ``kb_root()``)."""
    return Path(paths.workspace_home()) / "knowledge_base"


def docs_cli_dir() -> Path:
    """``<workspace root>/docs/cli`` — committed, core-owned half of the
    ``cli_reference`` split (the app-owned half is ``kb_dir() / "apps" /
    <id> / "cli_reference"``, written by ``_write_doc`` below)."""
    return Path(paths.workspace_root()) / "docs" / "cli"


@dataclass
class ReferenceSyncResult:
    written: int = 0
    failed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"written": self.written, "failed": self.failed}


def _write_doc(rel_path: Path, source_label: str, doc_type: str, body: str, captured: str) -> None:
    checksum = hashlib.sha256(body.encode()).hexdigest()
    out_path = kb_dir() / rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"source: {source_label}\n"
        f"type: {doc_type}\n"
        f"checksum: sha256:{checksum}\n"
        f"captured: {captured}\n"
        "edited: false\n"
        "---\n\n"
    )
    out_path.write_text(frontmatter + body, encoding="utf-8")


# --- CLI --help -----------------------------------------------------------

#: Card quality:procedural-genre-absent-from-both-knowledge-indexes,
#: measured follow-up: indexing the raw ``--help`` closed the CONTENT gap
#: but not the RETRIEVAL gap — asked in English naming the command,
#: `cli_reference/apps.md` scored 0.847 (the best in the whole experiment);
#: asked as "Como eu listo os apps instalados de uma workspace?" the same
#: doc wasn't in the top 5, because nothing in the embedding space bridges
#: terse English argparse output to a conversational Portuguese question.
#: This table gives the built-in top-level commands a real bilingual
#: question (not a mechanical translation — no LLM call does the
#: translating, a human phrased these once) to put above the verbatim
#: ``--help``. It deliberately only covers this repo's own top-level
#: commands: a subcommand or an app-contributed command falls back to the
#: mechanical phrasing in ``_intent_questions`` below, which is less fluent
#: but still bilingual and still names the command verbatim.
_PT_QUESTIONS: dict[str, str] = {
    "": "Como eu uso o `aw-workspace-cli`?",
    "agent": "Como eu sincronizo os agentes (skills, AGENTS.md e MCP) na workspace?",
    "apps": "Como eu listo os apps instalados de uma workspace?",
    "doctor": "Como eu verifico se tem algo degradado silenciosamente na workspace?",
    "folders": "Como eu mapeio uma pasta na workspace?",
    "help": "Como eu vejo a ajuda do aw-workspace-cli?",
    "kb-reference": "Como eu regenero a referência de CLI e skills na base de conhecimento?",
    "logs": "Como eu vejo os logs de um componente da workspace?",
    "marketplace": "Como eu instalo ou atualizo um app pelo marketplace?",
    "restart": "Como eu restarto a aw-workspace rodando?",
    "sideload": "Como eu instalo um app a partir de uma pasta local, pra testar?",
    "start": "Como eu inicio um componente da workspace?",
    "status": "Como eu vejo o status da workspace?",
    "stop": "Como eu paro um componente da workspace?",
    "test": "Como eu rodo os testes da workspace?",
    "update": "Como eu atualizo a aw-workspace rodando?",
}
_EN_QUESTIONS: dict[str, str] = {
    "": "How do I use `aw-workspace-cli`?",
    "agent": "How do I sync agents (skills, AGENTS.md, MCP) in the workspace?",
    "apps": "How do I list the apps installed on a workspace?",
    "doctor": "How do I check whether anything is silently degraded in the workspace?",
    "folders": "How do I map a folder into the workspace?",
    "help": "How do I see aw-workspace-cli's help?",
    "kb-reference": "How do I regenerate the CLI and skills reference in the knowledge base?",
    "logs": "How do I see a workspace component's logs?",
    "marketplace": "How do I install or update an app from the marketplace?",
    "restart": "How do I restart a running workspace?",
    "sideload": "How do I install an app from a local directory, to test it?",
    "start": "How do I start a workspace component?",
    "status": "How do I see the workspace's status?",
    "stop": "How do I stop a workspace component?",
    "test": "How do I run the workspace's test suite?",
    "update": "How do I update a running workspace?",
}


def _intent_questions(command_path: list[str]) -> tuple[str, str]:
    """(pt_question, en_question) for the bilingual intent surface. A
    top-level command in the tables above gets the hand-phrased pair; a
    subcommand or an app-contributed command (not in either table) falls
    back to a mechanical phrasing that still names the full command
    verbatim, so every doc gets a bilingual surface either way."""
    key = command_path[0] if command_path else ""
    if len(command_path) <= 1 and key in _PT_QUESTIONS:
        return _PT_QUESTIONS[key], _EN_QUESTIONS[key]
    full = (CLI_BIN + " " + " ".join(command_path)).strip()
    return f"Como eu uso o comando `{full}`?", f"How do I use `{full}`?"


def _subcommands(module) -> list[tuple[str, str]]:
    """(name, description) pairs for a command's own subcommands, if it
    declares any via argparse subparsers. Only modules exposing a
    module-level ``_build_parser()`` are introspectable this way
    (``agent``, ``marketplace``, ``folders``, ...); a command whose
    argparse logic lives in an imported app package (e.g. ``secrets``) is
    walked as a single top-level entry only — still correct, just not
    recursed into.

    The name list comes from ``action.choices`` (populated for every
    subcommand regardless of how it was declared); the description comes
    from ``action._choices_actions``, which argparse only populates for a
    subcommand whose own ``add_parser(..., help=...)`` call supplied one —
    a subcommand without one just gets an empty description, not a crash.
    """
    build_parser = getattr(module, "_build_parser", None)
    if build_parser is None:
        return []
    try:
        parser = build_parser()
    except Exception:  # noqa: BLE001 — one odd command must not break the walk
        return []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            help_by_name = {p.dest: p.help or "" for p in action._choices_actions}
            return [(name, help_by_name.get(name, "")) for name in sorted(action.choices)]
    return []


def _capture_help(command_path: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            [CLI_BIN, *command_path, "--help"],
            capture_output=True, text=True, timeout=_HELP_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cli_reference: %s --help failed: %s", " ".join(command_path), exc)
        return None
    return proc.stdout or proc.stderr


def _read_checksum(path: Path) -> str | None:
    """The ``checksum: sha256:...`` frontmatter line of an existing doc, or
    ``None`` if it has none / doesn't exist — the checksum-gate's read side."""
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            for _ in range(10):  # frontmatter is always near the top
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("checksum:"):
                    return line[len("checksum:"):].strip()
    except OSError:
        return None
    return None


def _write_core_doc(filename: str, source_label: str, body: str, captured: str) -> bool:
    """Checksum-gated write into the committed ``docs/cli/`` tree: a
    regeneration whose ``--help`` text is byte-identical to what's already
    committed must not touch the file (no spurious diff/commit), and a real
    interface change must still land. Returns whether a write happened."""
    checksum = f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"
    out_path = docs_cli_dir() / filename
    if _read_checksum(out_path) == checksum:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"source: {source_label}\n"
        "type: cli-reference\n"
        f"checksum: {checksum}\n"
        f"captured: {captured}\n"
        "edited: false\n"
        "---\n\n"
    )
    out_path.write_text(frontmatter + body, encoding="utf-8")
    return True


def _prune_stale(dir_path: Path, expected: set[str]) -> None:
    """Exact-mirror delete pass: a ``.md`` in ``dir_path`` whose command no
    longer exists (removed, or — for an app's own dir — the app got
    uninstalled) does not get to outlive it. The missing half of the
    generator before this split."""
    if not dir_path.is_dir():
        return
    for existing in sorted(dir_path.iterdir()):
        if existing.is_file() and existing.suffix == ".md" and existing.name not in expected:
            existing.unlink()


def _prune_app_stale(expected: dict[str, set[str]]) -> None:
    """Same delete pass as ``_prune_stale``, one level up: an app with no
    discovered commands this run (typically: uninstalled) loses its whole
    ``cli_reference/`` subdir. ``apps/<id>/docs/`` — the separate
    ``contributes.knowledge`` mirror — lives beside it and is never touched
    here; that tree belongs to a different registry."""
    apps_dir = kb_dir() / "apps"
    if not apps_dir.is_dir():
        return
    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir():
            continue
        cli_dir = app_dir / "cli_reference"
        if not cli_dir.is_dir():
            continue
        if app_dir.name not in expected:
            shutil.rmtree(cli_dir)
            continue
        _prune_stale(cli_dir, expected[app_dir.name])


def sync_cli_reference() -> ReferenceSyncResult:
    """Capture ``--help`` for the root command, every discovered command,
    and every subcommand it declares, and write one document per capture —
    split by owner (``discover_commands()``'s ``__aw_app_id__`` stamp): a
    built-in goes to the committed ``docs/cli/``, an app-contributed command
    goes to ``kb_dir()/apps/<id>/cli_reference/``. Both targets are
    exact-mirrored (stale captures for a removed command/uninstalled app are
    deleted), which this generator didn't do before this split."""
    result = ReferenceSyncResult()
    captured = date.today().isoformat()

    # (command_path, one-sentence description, owning app id or None for a
    # built-in) — the description is this repo's own cost-free source for
    # the intent surface's prose line: a top-level command's own
    # ``DESCRIPTION``, or a subcommand's ``add_parser(..., help=...)`` text
    # when it declared one.
    entries: list[tuple[list[str], str, str | None]] = [
        ([], "List and run every aw-workspace-cli command.", None)
    ]
    for name, module in sorted(discover_commands().items()):
        owner = getattr(module, "__aw_app_id__", None)
        entries.append(([name], getattr(module, "DESCRIPTION", "") or "", owner))
        for sub, sub_description in _subcommands(module):
            entries.append(([name, sub], sub_description, owner))

    core_expected: set[str] = set()
    app_expected: dict[str, set[str]] = {}

    for command_path, description, owner in entries:
        label = (CLI_BIN + " " + " ".join(command_path)).strip()
        text = _capture_help(command_path)
        if text is None:
            result.failed.append(label)
            continue
        slug = "-".join(command_path) or "root"
        filename = f"{slug}.md"
        pt_question, en_question = _intent_questions(command_path)
        intent = f"**{pt_question}** / **{en_question}**\n\n"
        if description:
            intent += f"{description}\n\n"
        body = f"# {label} --help\n\n{intent}`{label}`\n\n```\n{text.rstrip()}\n```\n"

        if owner is None:
            core_expected.add(filename)
            _write_core_doc(filename, label, body, captured)
        else:
            app_expected.setdefault(owner, set()).add(filename)
            _write_doc(Path("apps") / owner / "cli_reference" / filename, label, "cli-reference", body, captured)
        result.written += 1

    _prune_stale(docs_cli_dir(), core_expected)
    _prune_app_stale(app_expected)

    return result


# --- skills/*/SKILL.md as content ------------------------------------------


def sync_skills_reference() -> ReferenceSyncResult:
    """Index every materialized ``skills/<name>/SKILL.md`` as content under
    ``skills/``, recording which app owns it (``.aw-app-id``) or that it's
    native (``native-skills/``) per AGENTS.md's ownership rule."""
    result = ReferenceSyncResult()
    captured = date.today().isoformat()

    src_dir = skills_sync.source_dir()
    if not src_dir.is_dir():
        return result

    for skill_dir in sorted(p for p in src_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        label = f"skills/{skill_dir.name}/SKILL.md"
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cli_reference: could not read %s: %s", skill_md, exc)
            result.failed.append(label)
            continue

        owner_marker = skill_dir / ".aw-app-id"
        if owner_marker.is_file():
            owner = f"app: {owner_marker.read_text(encoding='utf-8').strip()}"
        else:
            owner = "native-skills/ (this repo)"

        body = f"# skill: {skill_dir.name}\n\nOwner: {owner}\n\n{content}\n"
        _write_doc(Path("skills") / f"{skill_dir.name}.md", label, "skill-content", body, captured)
        result.written += 1

    return result
