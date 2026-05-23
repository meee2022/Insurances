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

import hashlib
import hmac
import secrets

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).parent
SESSIONS_DIR = ROOT / ".sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"

# ── Auth ──────────────────────────────────────────────────────────────────────
# Set APP_USER / APP_PASS as Railway environment variables to enable login.
# If neither is set the app runs without a password (fine for localhost).
_AUTH_USER = os.environ.get("APP_USER", "").strip()
_AUTH_PASS = os.environ.get("APP_PASS", "").strip()
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)

# Cookie-based session so the browser only shows the login page once.
_SESSION_COOKIE = "tamer_session"
_SESSION_TOKENS: set[str] = set()   # valid tokens (in-memory, reset on restart)

_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>تسجيل الدخول · AL JAZEERAH</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{min-height:100vh;display:grid;place-items:center;
       background:radial-gradient(ellipse 80% 60% at 60% -10%,rgba(0,212,170,.18),transparent 55%),#07091a;
       font-family:'IBM Plex Sans Arabic',sans-serif;color:#e8edf6}
  .box{width:min(92vw,380px);background:rgba(255,255,255,.055);
       border:1px solid rgba(255,255,255,.1);border-radius:24px;
       padding:36px 32px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
  .logo{width:52px;height:52px;border-radius:16px;display:grid;place-items:center;
        background:linear-gradient(135deg,#00a887,#00d4aa);
        box-shadow:0 8px 24px rgba(0,212,170,.4);margin:0 auto 20px}
  h1{text-align:center;font-size:20px;font-weight:700;margin-bottom:4px}
  p{text-align:center;font-size:13px;color:#6b7a99;margin-bottom:28px}
  label{display:block;font-size:12px;color:#6b7a99;margin-bottom:6px;font-weight:600}
  input{width:100%;padding:13px 16px;border-radius:12px;
        border:1.5px solid rgba(255,255,255,.1);background:rgba(255,255,255,.05);
        color:#e8edf6;font-size:15px;outline:none;margin-bottom:16px;
        font-family:inherit;transition:border-color .2s}
  input:focus{border-color:#00d4aa;background:rgba(0,212,170,.05)}
  button{width:100%;padding:14px;border-radius:12px;border:none;cursor:pointer;
         background:linear-gradient(135deg,#00a887,#00d4aa);
         color:#fff;font-size:16px;font-weight:700;font-family:inherit;
         box-shadow:0 8px 24px rgba(0,212,170,.35);transition:all .2s}
  button:hover{transform:translateY(-2px);box-shadow:0 12px 30px rgba(0,212,170,.5)}
  .err{background:rgba(244,63,94,.15);border:1px solid rgba(244,63,94,.3);
       border-radius:10px;padding:10px 14px;font-size:13px;
       color:#f43f5e;text-align:center;margin-bottom:16px;display:none}
  .err.show{display:block}
</style>
</head>
<body>
<div class="box">
  <div class="logo">
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2"
         stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="11" width="18" height="11" rx="2"/>
      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
    </svg>
  </div>
  <h1>فحص تأمين المريض</h1>
  <p>AL JAZEERAH HEALTH CENTER</p>
  __ERR__
  <form method="POST" action="/__login">
    <label>اسم المستخدم</label>
    <input name="username" type="text" autocomplete="username" required autofocus>
    <label>كلمة السر</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">دخول</button>
  </form>
</div>
</body></html>"""


def _check_auth(request: Request) -> bool:
    """Return True if the request is authenticated (or auth is disabled)."""
    if not _AUTH_ENABLED:
        return True
    token = request.cookies.get(_SESSION_COOKIE, "")
    return token in _SESSION_TOKENS


def _login_response(error: bool = False) -> Response:
    err_html = '<div class="err show">اسم المستخدم أو كلمة السر غلط</div>' if error else '<div class="err"></div>'
    html = _LOGIN_PAGE.replace("__ERR__", err_html)
    return Response(content=html, media_type="text/html", status_code=401 if error else 200)


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
    raw = os.environ.get("CONFIG_JSON", "").strip()
    if raw:
        return json.loads(raw)
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


# ── Auth middleware ────────────────────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Block every route unless the user has a valid session cookie."""
    path = request.url.path
    # always allow the login form itself
    if path == "/__login":
        return await call_next(request)
    if not _check_auth(request):
        return _login_response()
    return await call_next(request)


@app.post("/__login")
async def do_login(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    user_ok = hmac.compare_digest(username, _AUTH_USER)
    pass_ok = hmac.compare_digest(password, _AUTH_PASS)
    if _AUTH_ENABLED and not (user_ok and pass_ok):
        return _login_response(error=True)
    token = secrets.token_hex(32)
    _SESSION_TOKENS.add(token)
    resp = Response(status_code=302, headers={"Location": "/"})
    resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


# ── Pages ──────────────────────────────────────────────────────────────────────
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

    # Cap concurrent browsers: each Chromium needs ~250 MB RAM.
    # Railway free tier = 512 MB → max 2 at once; paid = 8 GB → up to 7.
    _RAM_MB   = int(os.environ.get("RAILWAY_MEMORY_MB", "512"))
    _MAX_PAR  = max(1, min(len(selected), _RAM_MB // 300))

    _sem = threading.Semaphore(_MAX_PAR)

    def run(p):
        with _sem:
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

    # Railway / cloud: listen on 0.0.0.0 and use $PORT env var
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PORT") else "127.0.0.1"
    print(f"Open http://localhost:{port}  in your browser")
    uvicorn.run(app, host=host, port=port)
