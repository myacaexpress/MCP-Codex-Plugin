import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "medcare-resources"
sys.path.insert(0, str(PLUGIN))
os.environ.setdefault("MEDCARE_CACHE_DIR", str(ROOT / ".test-cache"))
