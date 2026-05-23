"""FastAPI web UI for the eligibility checker.

Run:  python webapp.py
Open:  http://localhost:8000
"""
import base64
import importlib
import json
import os
import queue
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# tell every portal module to launch its browser hidden — the web UI doesn't
# need browser windows popping up. Set BEFORE we import any portal module.
os.environ.setdefault("TAMER_HEADLESS", "1")

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).parent
SESSIONS_DIR = ROOT / ".sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"

AVAILABLE = ["almadallah", "adnic", "whealth", "lifeline", "aafiya", "gig_axa", "globalmed"]

# nice display labels for every portal we know about — even ones we haven't
# wired up yet, so the admin page shows them all
PORTAL_LABELS = {
    "almadallah":   {"en": "Almadallah",    "ar": "المدلة"},
    "adnic":        {"en": "ADNIC",         "ar": "أبوظبي الوطنية"},
    "whealth":      {"en": "W Health",      "ar": "دبليو هيلث"},
    "lifeline":     {"en": "Lifeline",      "ar": "لايف لاين"},
    "inayahtpa":    {"en": "Inayah TPA",    "ar": "عناية"},
    "gig_axa":      {"en": "GIG / AXA",     "ar": "GIG"},
    "globalmed":    {"en": "Global Med",    "ar": "غلوبل ميد"},
    "aafiya":       {"en": "Aafiya",        "ar": "عافية"},
    "fmc":          {"en": "FMC",           "ar": "فاطمة"},
    "healthaspire": {"en": "Health Aspire", "ar": "هيلث أسباير"},
}


def _read_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------
# Interactive CAPTCHA support
# ------------------------------------------------------------------
# Some portals (e.g. GIG/AXA) guard their login with an image CAPTCHA.
# Instead of OCR, we surface the image to the web UI and let the user
# type the answer. Each pending challenge is parked here until the user
# POSTs the answer to /captcha/submit, which unblocks the waiting thread.
CAPTCHA_WAIT_SECONDS = 180
_PENDING_CAPTCHAS: dict = {}
_PENDING_LOCK = threading.Lock()


def submit_captcha_answer(cid: str, answer: str) -> bool:
    with _PENDING_LOCK:
        entry = _PENDING_CAPTCHAS.get(cid)
        if not entry:
            return False
        entry["answer"] = answer
        entry["event"].set()
    return True


def _run_portal(portal_name: str, eid: str, captcha_solver=None) -> dict:
    try:
        mod = importlib.import_module(f"portals.{portal_name}")
        return mod.check(eid, captcha_solver=captcha_solver)
    except Exception as e:
        return {
            "portal": portal_name,
            "status": "ERROR",
            "message": str(e),
            "details": {},
        }


app = FastAPI(title="Insurance Eligibility Check")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(ROOT / "static" / "admin.html")


@app.get("/portals")
def list_portals():
    """Tell the UI which portals are wired up."""
    return {"portals": AVAILABLE}


# ============================================================
# Admin endpoints — manage credentials, sessions, manual login
# ============================================================

@app.get("/admin/portals")
def admin_list_portals():
    """List every portal in config.json with status + session metadata."""
    cfg = _read_config()
    out = []
    for name, data in cfg["portals"].items():
        label = PORTAL_LABELS.get(name, {"en": name, "ar": ""})
        session_file = SESSIONS_DIR / f"{name}.json"
        session_age_h = None
        if session_file.exists():
            session_age_h = round(
                (time.time() - session_file.stat().st_mtime) / 3600, 1
            )
        out.append({
            "name": name,
            "label_en": label["en"],
            "label_ar": label["ar"],
            "url": data.get("url", ""),
            "username": data.get("username", ""),
            "password": data.get("password", ""),
            "implemented": name in AVAILABLE,
            "session": {
                "exists": session_file.exists(),
                "age_hours": session_age_h,
            },
        })
    return {"portals": out}


@app.post("/admin/portals/{name}")
def admin_update_portal(name: str, body: dict = Body(...)):
    """Update one portal's credentials. Body: { url?, username?, password? }"""
    cfg = _read_config()
    if name not in cfg["portals"]:
        raise HTTPException(status_code=404, detail="unknown portal")
    for key in ("url", "username", "password"):
        if key in body:
            cfg["portals"][name][key] = body[key]
    _write_config(cfg)
    return {"ok": True}


@app.post("/admin/portals/{name}/open")
def admin_open_portal(name: str):
    """Open the portal's URL in the user's default browser (no automation)."""
    cfg = _read_config()
    if name not in cfg["portals"]:
        raise HTTPException(status_code=404, detail="unknown portal")
    webbrowser.open(cfg["portals"][name]["url"])
    return {"ok": True}


@app.post("/admin/portals/{name}/clear-session")
def admin_clear_session(name: str):
    """Delete the saved session cookies for a portal."""
    session_file = SESSIONS_DIR / f"{name}.json"
    if session_file.exists():
        session_file.unlink()
        return {"ok": True, "deleted": True}
    return {"ok": True, "deleted": False}


@app.get("/check/stream")
def check_stream(eid: str, portals: str = "all"):
    """Stream results as each portal finishes (Server-Sent Events).

    Result events carry one portal's result dict. If a portal needs a CAPTCHA
    solved, it emits a ``captcha`` event (image + challenge id); the UI shows
    it, the user types the answer and POSTs it to /captcha/submit, and the
    portal thread resumes. The UI updates its cards live.
    """
    selected = AVAILABLE if portals == "all" else [p for p in portals.split(",") if p in AVAILABLE]
    q: "queue.Queue" = queue.Queue()

    def solver(portal_name: str, image_bytes: bytes, prompt: str = "") -> str:
        """Called from a portal thread; blocks until the user answers."""
        cid = uuid.uuid4().hex
        ev = threading.Event()
        with _PENDING_LOCK:
            _PENDING_CAPTCHAS[cid] = {"event": ev, "answer": None}
        q.put(("captcha", {
            "id": cid,
            "portal": portal_name,
            "image": base64.b64encode(image_bytes).decode("ascii"),
            "prompt": prompt,
        }))
        got = ev.wait(timeout=CAPTCHA_WAIT_SECONDS)
        with _PENDING_LOCK:
            entry = _PENDING_CAPTCHAS.pop(cid, {})
        return (entry.get("answer") or "") if got else ""

    def run(p):
        q.put(("result", _run_portal(p, eid, solver)))

    def event_stream():
        # tell the UI which portals to render placeholder cards for
        yield f"event: start\ndata: {json.dumps({'portals': selected})}\n\n"
        ex = ThreadPoolExecutor(max_workers=len(selected) or 1)
        for p in selected:
            ex.submit(run, p)
        remaining = len(selected)
        while remaining > 0:
            kind, payload = q.get()
            if kind == "result":
                remaining -= 1
                yield f"event: result\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:  # captcha
                yield f"event: captcha\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ex.shutdown(wait=False)
        yield f"event: done\ndata: {json.dumps({'stopped': False, 'portal': None})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/captcha/submit")
def captcha_submit(body: dict = Body(...)):
    """Receive a user-typed CAPTCHA answer and resume the waiting portal."""
    cid = body.get("id", "")
    answer = body.get("answer", "")
    if not submit_captcha_answer(cid, answer):
        raise HTTPException(status_code=404, detail="unknown or expired captcha")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    print("Open http://localhost:8000  in your browser")
    uvicorn.run(app, host="127.0.0.1", port=8000)
