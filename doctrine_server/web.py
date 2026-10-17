"""Server-rendered HTML: the landing page and the device sign-in ("link") pages.

Plain HTML with inline CSS, no JavaScript. Forms are protected with a double-submit
token: a random cookie set when /link is shown, plus a hidden field that is an HMAC
of (device user code, cookie). The Discord OAuth `state` is signed the same way.
"""
from __future__ import annotations

import html
import logging
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import auth, db
from .deps import client_ip, get_conn
from .errors import ApiError

log = logging.getLogger("doctrine_server.web")
router = APIRouter()

COOKIE = "doctrine_link"
STATE_TTL = 600

_CSS = """
:root { color-scheme: light dark; --bg:#f4f2ec; --fg:#1d1f1c; --muted:#5f625b;
  --card:#ffffff; --line:#d9d6cc; --accent:#3c5a3a; --accent-fg:#ffffff;
  --discord:#5865f2; --err:#9b2c2c; --ok:#2f6b34; }
@media (prefers-color-scheme: dark) { :root { --bg:#161815; --fg:#e7e5de; --muted:#a3a69c;
  --card:#20231f; --line:#363a33; --accent:#7fa36f; --accent-fg:#10130f; --err:#f08a8a;
  --ok:#8fd394; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 34rem; margin: 0 auto; padding: 2rem 1rem 3rem; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.1rem; margin: 0 0 .75rem; }
p { margin: .5rem 0; }
.muted { color: var(--muted); font-size: .9rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:1.1rem 1.2rem; margin:1rem 0; }
.code { font: 600 2rem/1.2 ui-monospace, "Cascadia Mono", Menlo, monospace;
  letter-spacing: .12em; text-align:center; padding:.5rem 0; }
label { display:block; font-size:.9rem; margin:.6rem 0 .2rem; }
input { width:100%; padding:.55rem .6rem; font:inherit; color:inherit; background:var(--bg);
  border:1px solid var(--line); border-radius:6px; }
button, .button { display:inline-block; margin-top:.9rem; padding:.6rem 1.1rem; font:inherit;
  font-weight:600; border:0; border-radius:6px; background:var(--accent);
  color:var(--accent-fg); text-decoration:none; cursor:pointer; }
.discord { background:var(--discord); color:#fff; }
.error { border-color: var(--err); color: var(--err); }
.success { border-color: var(--ok); }
.success h2 { color: var(--ok); }
code { font-family: ui-monospace, Menlo, monospace; }
"""


def page(server_name: str, title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)} - {html.escape(server_name)}</title>
<style>{_CSS}</style></head>
<body><main>
<h1>{html.escape(server_name)}</h1>
{body}
<p class="muted">A community-run Doctrine dedicated server. It is not operated by
Straits Games.</p>
</main></body></html>"""
    return HTMLResponse(doc, status_code=status, headers={
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                   "form-action 'self' https://discord.com; frame-ancestors 'none'",
    })


def _cfg(request: Request):
    return request.app.state.config


def _secret(request: Request) -> bytes:
    return request.app.state.secret


def _csrf(request: Request, user_code: str, nonce: str) -> str:
    return auth.sign(_secret(request), f"link|{user_code}|{nonce}")


def _set_cookie(request: Request, response, nonce: str) -> None:
    response.set_cookie(COOKIE, nonce, max_age=3600, httponly=True, samesite="lax",
                        secure=_cfg(request).public_url.startswith("https://"), path="/")


# ---------------------------------------------------------------- landing

@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    cfg = _cfg(request)
    address = cfg.public_url or str(request.base_url).rstrip("/")
    motd = f'<div class="card"><p>{html.escape(cfg.motd)}</p></div>' if cfg.motd else ""
    body = f"""
<p>This is a Doctrine dedicated server.</p>
{motd}
<div class="card">
<p>In <strong>Doctrine</strong> choose <strong>Multiplayer &gt; Dedicated Server</strong>
and enter this address:</p>
<p class="code" style="font-size:1.1rem;letter-spacing:0">{html.escape(address)}</p>
</div>"""
    return page(cfg.server_name, "Doctrine server", body)


# ---------------------------------------------------------------- link pages

def _code_entry(request: Request, message: str = "", status: int = 200) -> HTMLResponse:
    err = f'<div class="card error"><p>{html.escape(message)}</p></div>' if message else ""
    body = f"""{err}
<div class="card"><h2>Link your game</h2>
<form method="get" action="/link">
<label for="code">Code shown in Doctrine</label>
<input id="code" name="code" placeholder="ABCD-EFGH" autocomplete="off" required>
<button type="submit">Continue</button>
</form></div>"""
    return page(_cfg(request).server_name, "Sign in", body, status)


def _message(request: Request, title: str, text: str, status: int, kind: str = "error") -> HTMLResponse:
    body = f'<div class="card {kind}"><h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></div>'
    return page(_cfg(request).server_name, title, body, status)


def _linked(request: Request, player: auth.Player) -> HTMLResponse:
    return _message(request, "Linked — return to Doctrine",
                    f"Signed in as {player.display_name}. You can close this tab; the game "
                    "finishes signing in by itself.", 200, kind="success")


def _expired(request: Request) -> HTMLResponse:
    return _message(request, "Code expired",
                    "This sign-in code has expired or was already used. Start signing in "
                    "again from Doctrine.", 400)


def _link_page(request: Request, user_code: str, nonce: str, error: str = "",
               status: int = 200, name: str = "") -> HTMLResponse:
    cfg = _cfg(request)
    shown = auth.format_user_code(user_code)
    csrf = _csrf(request, user_code, nonce)
    parts = []
    if error:
        parts.append(f'<div class="card error"><p>{html.escape(error)}</p></div>')
    if cfg.motd:
        parts.append(f'<div class="card"><p>{html.escape(cfg.motd)}</p></div>')
    parts.append(f"""<div class="card"><h2>Sign in to play</h2>
<p>Doctrine is asking to link to this server with the code:</p>
<p class="code">{html.escape(shown)}</p>
<p class="muted">Only continue if this matches the code shown in your game.</p></div>""")
    if cfg.discord_oauth_enabled:
        parts.append(f"""<div class="card"><h2>Discord</h2>
<a class="button discord" href="/auth/discord/start?code={quote(shown)}">Sign in with Discord</a>
</div>""")
    if cfg.local_enabled:
        hidden = (f'<input type="hidden" name="code" value="{html.escape(shown)}">'
                  f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">')
        n = html.escape(name)
        parts.append(f"""<div class="card"><h2>Sign in</h2>
<form method="post" action="/link">{hidden}
<input type="hidden" name="action" value="signin">
<label for="si-name">Display name</label>
<input id="si-name" name="name" value="{n}" autocomplete="username" required>
<label for="si-pw">Password</label>
<input id="si-pw" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form></div>
<div class="card"><h2>Create account</h2>
<form method="post" action="/link">{hidden}
<input type="hidden" name="action" value="register">
<label for="re-name">Display name ({auth.NAME_MIN}-{auth.NAME_MAX} characters, shown to other players)</label>
<input id="re-name" name="name" minlength="{auth.NAME_MIN}" maxlength="{auth.NAME_MAX}" autocomplete="username" required>
<label for="re-pw">Password (at least {auth.PASSWORD_MIN} characters)</label>
<input id="re-pw" name="password" type="password" minlength="{auth.PASSWORD_MIN}" autocomplete="new-password" required>
<label for="re-join">Join code (ask whoever runs this server)</label>
<input id="re-join" name="join_code" autocomplete="off" required>
<button type="submit">Create account</button>
</form></div>""")
    resp = page(cfg.server_name, "Sign in", "\n".join(parts), status)
    _set_cookie(request, resp, nonce)
    return resp


@router.get("/link", response_class=HTMLResponse)
def link_get(request: Request, code: str = "", conn=Depends(get_conn)):
    if not code:
        return _code_entry(request)
    user_code = auth.normalize_user_code(code)
    if not user_code:
        return _code_entry(request, "That doesn't look like a sign-in code.", 400)
    if auth.pending_device(conn, user_code) is None:
        return _expired(request)
    nonce = request.cookies.get(COOKIE) or secrets.token_urlsafe(24)
    return _link_page(request, user_code, nonce)


_ERRORS = {
    "bad_name": f"Display names are {auth.NAME_MIN}-{auth.NAME_MAX} characters: letters, "
                "digits, spaces and . ' - _ only.",
    "bad_password": f"Passwords must be at least {auth.PASSWORD_MIN} characters.",
    "name_taken": "That display name is already taken.",
}


@router.post("/link", response_class=HTMLResponse)
def link_post(request: Request, code: str = Form(""), csrf: str = Form(""),
              action: str = Form(""), name: str = Form(""), password: str = Form(""),
              join_code: str = Form(""), conn=Depends(get_conn)):
    cfg = _cfg(request)
    if not cfg.local_enabled:
        return _message(request, "Not available", "Local accounts are not enabled here.", 404)
    user_code = auth.normalize_user_code(code)
    nonce = request.cookies.get(COOKIE, "")
    if not user_code or not nonce or not auth.verify_sig(
            _secret(request), f"link|{user_code}|{nonce}", csrf):
        return _message(request, "Form expired",
                        "This form expired or your browser blocked its cookie. Go back, "
                        "reload the page and try again.", 400)
    if auth.pending_device(conn, user_code) is None:
        return _expired(request)
    ip = client_ip(request)
    limiter = request.app.state.fail_limiter
    if limiter.blocked(ip):
        return _link_page(request, user_code, nonce, "Too many failed attempts. Wait a few "
                          "minutes and try again.", 429, name)

    if action == "register":
        if not auth.constant_time_equals(join_code.strip(), cfg.join_code):
            limiter.record(ip)
            return _link_page(request, user_code, nonce, "Wrong join code.", 403)
        try:
            player = auth.create_local_player(conn, name, password)
        except ApiError as e:
            return _link_page(request, user_code, nonce, _ERRORS.get(e.code, e.code),
                              400 if e.status == 400 else e.status)
        log.info("local account created: %s (%s)", player.display_name, player.id)
    elif action == "signin":
        player = auth.authenticate_local(conn, name, password)
        if player is None:
            limiter.record(ip)
            return _link_page(request, user_code, nonce, "Wrong display name or password.",
                              403, name)
        if player.disabled:
            return _message(request, "Account disabled",
                            "This account has been disabled by the server operator.", 403)
    else:
        return _message(request, "Bad request", "Unknown form action.", 400)

    if not auth.approve_device(conn, user_code, player.id):
        return _expired(request)
    return _linked(request, player)


# ---------------------------------------------------------------- Discord OAuth

@router.get("/auth/discord/start")
def discord_start(request: Request, code: str = "", conn=Depends(get_conn)):
    cfg = _cfg(request)
    oauth = request.app.state.discord_oauth
    if not cfg.discord_oauth_enabled or oauth is None:
        return _message(request, "Not available", "Discord sign-in is not enabled here.", 404)
    user_code = auth.normalize_user_code(code)
    if not user_code or auth.pending_device(conn, user_code) is None:
        return _expired(request)
    nonce = request.cookies.get(COOKIE)
    if not nonce:   # came here directly: show the link page first (it sets the cookie)
        return RedirectResponse(f"/link?code={quote(auth.format_user_code(user_code))}", 303)
    ts = str(int(time.time()))
    sig = auth.sign(_secret(request), f"oauth|{user_code}|{ts}|{nonce}")
    return RedirectResponse(oauth.authorize_url(f"{user_code}.{ts}.{sig}"), 303)


@router.get("/auth/discord/callback")
async def discord_callback(request: Request, code: str = "", state: str = "",
                           error: str = ""):
    cfg = _cfg(request)
    oauth = request.app.state.discord_oauth
    if not cfg.discord_oauth_enabled or oauth is None:
        return _message(request, "Not available", "Discord sign-in is not enabled here.", 404)
    if error:
        return _message(request, "Sign-in cancelled",
                        "Discord sign-in was cancelled. Start again from Doctrine.", 400)
    # Verify the signed state (binds this callback to the browser that started it).
    try:
        user_code, ts, sig = state.split(".")
        age = time.time() - int(ts)
    except ValueError:
        return _message(request, "Bad request", "Invalid sign-in state.", 400)
    nonce = request.cookies.get(COOKIE, "")
    if (not nonce or not (0 <= age <= STATE_TTL)
            or not auth.verify_sig(_secret(request), f"oauth|{user_code}|{ts}|{nonce}", sig)):
        return _message(request, "Bad request",
                        "This sign-in link is invalid or expired. Start again from Doctrine.", 400)
    if not code:
        return _message(request, "Bad request", "Discord did not return a sign-in code.", 400)

    db_path = request.app.state.db_path

    def with_conn(fn, *args):
        conn = db.connect(db_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    if await run_in_threadpool(with_conn, auth.pending_device, user_code) is None:
        return _expired(request)

    from .discord.oauth import OAuthError
    try:
        token = await oauth.exchange_code(code)
        user = await oauth.fetch_user(token)
    except OAuthError as e:
        log.warning("Discord OAuth failed: %s", e)
        return _message(request, "Discord sign-in failed",
                        "Discord did not accept the sign-in. Try again.", 502)

    user_id = str(user.get("id") or "")
    if not user_id.isdigit():
        return _message(request, "Discord sign-in failed", "Unexpected Discord response.", 502)

    checker = request.app.state.guild_checker
    if cfg.discord_guild_id and checker is not None:
        try:
            member = await checker(user_id)
        except Exception:  # noqa: BLE001
            log.exception("guild membership check failed")
            member = None
        if member is None:
            return _message(request, "Try again later",
                            "The server could not check your Discord membership right now.", 503)
        if not member:
            invite = f" Join it first: {cfg.discord_invite_url}" if cfg.discord_invite_url else ""
            return _message(request, "Not a member",
                            "This server is only open to members of its Discord server." + invite,
                            403)

    username = str(user.get("username") or user_id)
    display = str(user.get("global_name") or username)
    player = await run_in_threadpool(with_conn, auth.upsert_discord_player, user_id,
                                     username, display)
    if player.disabled:
        return _message(request, "Account disabled",
                        "This account has been disabled by the server operator.", 403)
    if not await run_in_threadpool(with_conn, auth.approve_device, user_code, player.id):
        return _expired(request)
    log.info("Discord sign-in: %s (%s)", player.display_name, player.id)
    return _linked(request, player)
