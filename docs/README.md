# Workspace docs

Generic documentation for this workspace — notes, decisions and references
that belong to the workspace itself rather than to any one repo under
`repos/`.

This directory is mapped into the workspace as the folder **`docs`**
(Workspace › Folders), which is what makes it reachable to apps that receive
`$AW_WORKSPACE_FOLDERS` — the Knowledge Base indexes every mapped folder, so
anything added here becomes searchable on the next
`aw-workspace-cli knowledge-base --map-all && … --build`.

Per-repo documentation stays in its own repo.
