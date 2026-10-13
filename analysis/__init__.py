"""Offline analysis using local workspace APIs without installing model environments."""

import sys
from pathlib import Path

_workspace = Path(__file__).resolve().parent.parent
_sources = sorted([*_workspace.glob("packages/*/src"), *_workspace.glob("apps/*/src")])
for _source in reversed(_sources):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))
