"""Project skill launcher. Resolve the runtime from the installed skill symlink."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from factory.__main__ import main

main()
