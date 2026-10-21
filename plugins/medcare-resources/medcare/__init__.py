"""MedCare Resources: public MedCare Partners Health Plan of Texas resources and
public Medicare plan research for Codex.

Independent team tool. Not affiliated with MedCare Partners, CMS or Medicare.gov.
No credentials, beneficiary records or enrollment systems are accessed.
"""

__version__ = "1.2.0"

import sys as _sys
from pathlib import Path as _Path

ROOT = _Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
_VENDOR = ROOT / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in _sys.path:
    # Vendored pure-Python pypdf (BSD-3) so the server runs with no pip installs.
    # Inserted first so behaviour does not depend on whatever pypdf a user has installed.
    _sys.path.insert(0, str(_VENDOR))
