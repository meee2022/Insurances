"""Look up a patient across one or all portals.

Examples:
    python check.py 784198241075476                      # all portals
    python check.py 784198241075476 --portal almadallah  # one portal
"""
import argparse
import importlib
import json
import sys
from pathlib import Path

# force utf-8 on Windows so we can print emirati/arabic chars and box symbols
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# only portals that have a working module live here; we'll append as we add them
AVAILABLE = ["almadallah", "adnic", "whealth", "lifeline", "inayahtpa", "gig_axa"]


def run_one(portal: str, eid: str) -> dict:
    mod = importlib.import_module(f"portals.{portal}")
    return mod.check(eid)


def print_result(r: dict):
    icon = {"ELIGIBLE": "[OK] ", "NOT_ELIGIBLE": "[--] ", "ERROR": "[ERR]", "UNKNOWN": "[?]  "}.get(r["status"], "[?]")
    print(f"{icon} {r['portal']:<14} {r['status']:<14} {r.get('message','')}")
    for k, v in (r.get("details") or {}).items():
        if isinstance(v, dict):
            print(f"        {k}:")
            for kk, vv in v.items():
                print(f"          {kk}: {vv}")
        else:
            print(f"        {k}: {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("emirates_id")
    ap.add_argument("--portal", choices=AVAILABLE + ["all"], default="all")
    args = ap.parse_args()

    portals = AVAILABLE if args.portal == "all" else [args.portal]
    print(f"checking Emirates ID: {args.emirates_id}\n")
    for p in portals:
        try:
            result = run_one(p, args.emirates_id)
        except Exception as e:
            result = {"portal": p, "status": "ERROR", "message": str(e), "details": {}}
        print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
