# apps/

This folder holds apps installed from the marketplace. Everything under it
(besides this file) is runtime state, not project source — that's why it's
gitignored.

You can install, update, or remove apps two ways:

- **Through the UI** — the Apps section of the console.
- **From a terminal inside the workspace**, using this workspace's own CLI:

  ```bash
  aw-workspace-cli marketplace install <app>
  aw-workspace-cli marketplace install <app> --update
  ```

See `skills/aw-workspace/SKILL.md` for the full CLI reference.
