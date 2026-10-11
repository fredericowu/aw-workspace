---
source: aw-workspace-cli test
type: cli-reference
checksum: sha256:4057a80b8222636bd92bc30c9ec5dbcbbbeb3d0bbc2950aa1f87a0f48e5b5dd0
captured: 2026-10-11
edited: false
---

# aw-workspace-cli test --help

**Como eu rodo os testes da workspace?** / **How do I run the workspace's test suite?**

Run this workspace's pytest suite (extra args go to pytest)

`aw-workspace-cli test`

```
``aw-workspace-cli test`` — port of the monolith's ``./aw test``.

Runs this repo's own pytest suite from the workspace root, so an agent or a
terminal session doesn't have to know where the tests live or which
interpreter to use. Extra arguments are passed straight through to pytest
(``aw-workspace-cli test -k folders -x``).
```
