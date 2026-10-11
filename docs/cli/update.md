---
source: aw-workspace-cli update
type: cli-reference
checksum: sha256:7b1dcde339c78aa82a3d38f8cf7bf33db2eb50d968cedfde3d2d52a7dd93306e
captured: 2026-10-11
edited: false
---

# aw-workspace-cli update --help

**Como eu atualizo a aw-workspace rodando?** / **How do I update a running workspace?**

Update the workspace code or its remote host (aw-console Manage > Update)

`aw-workspace-cli update`

```
usage: aw-workspace-cli update [-h] [--token TOKEN] {remote-host,workspace}

Update the workspace code or its remote host (aw-console Manage > Update)

positional arguments:
  {remote-host,workspace}
                        What to update

options:
  -h, --help            show this help message and exit
  --token TOKEN         Central-identity bearer token (defaults to
                        $AW_ID_TOKEN)

restart vs. update: `restart core` reloads the process on code already on disk
— what you want after a plain `git push`, since /opt/aw-workspace is a host
bind mount and the commit is already there. `update workspace` instead pulls
the :latest container image and syncs its repo copy over the host dir — reach
for it only when you actually need a new image.
```
