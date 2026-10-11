---
source: aw-workspace-cli kb-reference
type: cli-reference
checksum: sha256:8d75afed65a58e9b79dae7422ca9f19929b41cec14749e27e44cf19fb7ccce0c
captured: 2026-10-11
edited: false
---

# aw-workspace-cli kb-reference --help

**Como eu regenero a referência de CLI e skills na base de conhecimento?** / **How do I regenerate the CLI and skills reference in the knowledge base?**

Regenerate CLI --help and skills/ content under the knowledge-base tree

`aw-workspace-cli kb-reference`

```
``aw-workspace-cli kb-reference`` — (re)generate the ``cli_reference/`` and
``skills/`` knowledge-base content from ``src/libs/cli_reference.py``.

Run by the "Sync: CLI + Skills reference" scheduled task so a captured
``--help`` snapshot never outlives one tick; also runnable by hand right
after a CLI command or a skill changes. Writing the docs is all this does —
``aw-workspace-cli knowledge-base --build`` (pgvector) and
``aw-workspace-cli knowledgeable-ingest run`` (the graph) still have to run
afterward to actually ingest them.
```
