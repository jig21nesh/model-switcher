import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for subdir in ("hooks", "statusline", "scripts"):
    sys.path.insert(0, str(REPO_ROOT / subdir))
