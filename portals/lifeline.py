"""Lifeline (tpa.lifelinetpa.com) eligibility check.

Same JBM_HIMS template as W Health — only the host and credentials change.
"""
from pathlib import Path
from ._jbm_hims import jbm_hims_check

PORTAL_NAME = "lifeline"
ROOT = Path(__file__).parent.parent / "exploration"


def check(emirates_id: str, **_) -> dict:
    return jbm_hims_check(PORTAL_NAME, emirates_id, ROOT)
