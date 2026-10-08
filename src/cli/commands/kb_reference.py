"""``aw-workspace-cli kb-reference`` — (re)generate the ``cli_reference/`` and
``skills/`` knowledge-base content from ``src/libs/cli_reference.py``.

Run by the "Sync: CLI + Skills reference" scheduled task so a captured
``--help`` snapshot never outlives one tick; also runnable by hand right
after a CLI command or a skill changes. Writing the docs is all this does —
``aw-workspace-cli knowledge-base --build`` (pgvector) and
``aw-workspace-cli knowledgeable-ingest run`` (the graph) still have to run
afterward to actually ingest them.
"""
from __future__ import annotations

from src.libs import cli_reference

COMMAND = "kb-reference"
DESCRIPTION = "Regenerate CLI --help and skills/ content under the knowledge-base tree"


def run(args: list[str]) -> int:
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    cli_result = cli_reference.sync_cli_reference()
    skills_result = cli_reference.sync_skills_reference()

    print(f"cli_reference/: {cli_result.written} written"
          + (f", {len(cli_result.failed)} failed ({', '.join(cli_result.failed)})"
             if cli_result.failed else ""))
    print(f"skills/: {skills_result.written} written"
          + (f", {len(skills_result.failed)} failed ({', '.join(skills_result.failed)})"
             if skills_result.failed else ""))

    return 1 if (cli_result.failed or skills_result.failed) else 0
