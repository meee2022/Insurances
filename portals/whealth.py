"""W Health (whealthapp.whealthtpa.com) eligibility check.

Shares the JBM_HIMS flow with Lifeline.
"""
from pathlib import Path
from ._jbm_hims import jbm_hims_check

PORTAL_NAME = "whealth"
ROOT = Path(__file__).parent.parent / "exploration"


def check(emirates_id: str, **_) -> dict:
    return jbm_hims_check(PORTAL_NAME, emirates_id, ROOT)
