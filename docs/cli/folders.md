---
source: aw-workspace-cli folders
type: cli-reference
checksum: sha256:3645bac05633ce0f86a322a04a10ed7563e6a8be6a3b5dcbd624ee4c21e2179a
captured: 2026-10-11
edited: false
---

# aw-workspace-cli folders --help

**Como eu mapeio uma pasta na workspace?** / **How do I map a folder into the workspace?**

Map any folder into the workspace by name (no git repo needed)

`aw-workspace-cli folders`

```
usage: aw-workspace-cli folders [-h] {list,add,rm,browse} ...

Map any folder into the workspace by name (no git repo needed)

positional arguments:
  {list,add,rm,browse}
    list                List every mapped folder
    add                 Map a folder (re-mapping a name updates it)
    rm                  Unmap a folder by name
    browse              List subdirectories, to find a path to map

options:
  -h, --help            show this help message and exit
```
