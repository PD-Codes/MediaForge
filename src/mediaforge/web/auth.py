"""Authentication for the web UI: local username/password login, first-run
setup token flow, optional OIDC/SSO login, the login_required/admin_required
decorators, session helpers, and the admin user-management API.

The `auth_bp` blueprint defined here is registered by create_app() in app.py
only when auth_enabled=True. login_required/admin_required are applied to
every other registered view function dynamically (not via decorator) at the
end of create_app(), based on the `_exempt`/`_admin_only` endpoint sets.
"""

import os
import re
import secrets
import time
from functools import wraps

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from ..config import MEDIAFORGE_CONFIG_DIR
from ..logger import get_logger
from .db import (
    create_user,
    delete_user,
    find_or_create_sso_user,
    get_db,
    has_any_admin,
    list_users,
    update_user_role,
    verify_user,
)

logger = get_logger(__name__)

_SECRET_KEY_PATH = MEDIAFORGE_CONFIG_DIR / ".flask_secret"

oauth = OAuth()

# Rate limiter — initialized lazily via init_app() in create_app().
# Uses Redis when MEDIAFORGE_REDIS_URL is set; falls back to in-memory storage.
# In-memory is fine for the default single-process deployment — limits are
# scoped to the process lifetime and reset on restart.
def _limiter_storage_uri() -> str:
    redis_url = os.environ.get("MEDIAFORGE_REDIS_URL", "").strip()
    if redis_url:
        return redis_url
    return "memory://"

limiter = Limiter(key_func=get_remote_address, default_limits=[],
                  storage_uri=_limiter_storage_uri())


def get_oidc_config():
    """Read OIDC settings (DB first, then env var fallback). Returns None if
    issuer/client_id/client_secret aren't all configured, meaning SSO login
    is unavailable."""
    try:
        from .db import get_setting as _gs
    except ImportError:
        _gs = lambda k, d="": ""

    def _val(db_key, env_key, default=""):
        return (_gs(db_key) or os.environ.get(env_key, default) or default).strip()

    issuer        = _val("oidc_issuer_url",   "MEDIAFORGE_OIDC_ISSUER_URL")
    client_id     = _val("oidc_client_id",    "MEDIAFORGE_OIDC_CLIENT_ID")
    client_secret = _val("oidc_client_secret","MEDIAFORGE_OIDC_CLIENT_SECRET")
    if not (issuer and client_id and client_secret):
        return None
    return {
        "issuer_url":    issuer,
        "client_id":     client_id,
        "client_secret": client_secret,
        "display_name":  _val("oidc_display_name",  "MEDIAFORGE_OIDC_DISPLAY_NAME", "SSO") or "SSO",
        "admin_user":    _val("oidc_admin_user",     "MEDIAFORGE_OIDC_ADMIN_USER")    or None,
        "admin_subject": _val("oidc_admin_subject",  "MEDIAFORGE_OIDC_ADMIN_SUBJECT") or None,
    }


def init_oidc(app, force_sso=False):
    """Register the OIDC client with Authlib and set the app.config OIDC_*
    flags used by templates and routes. If get_oidc_config() returns None
    (SSO not configured), OIDC is left disabled and local login stays active.

    Used by: create_app() in app.py, when sso_enabled=True.
    """
    cfg = get_oidc_config()
    if cfg is None:
        app.config["OIDC_ENABLED"] = False
        app.config["OIDC_DISPLAY_NAME"] = "SSO"
        app.config["OIDC_ADMIN_USER"] = None
        app.config["OIDC_ADMIN_SUBJECT"] = None
        app.config["FORCE_SSO"] = force_sso
        return

    oauth.init_app(app)
    oauth.register(
        name="oidc",
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        server_metadata_url=cfg["issuer_url"].rstrip("/")
        + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    app.config["OIDC_ENABLED"] = True
    app.config["OIDC_DISPLAY_NAME"] = cfg["display_name"]
    app.config["OIDC_ADMIN_USER"] = cfg["admin_user"]
    app.config["OIDC_ADMIN_SUBJECT"] = cfg["admin_subject"]
    app.config["FORCE_SSO"] = force_sso


def get_or_create_secret_key():
    """Return the persistent Flask secret key, generating and saving one
    (mode 0600) on first run so sessions survive process restarts."""
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if _SECRET_KEY_PATH.exists():
        return _SECRET_KEY_PATH.read_bytes()
    key = secrets.token_bytes(32)
    fd = os.open(str(_SECRET_KEY_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


def get_current_user():
    """Return the logged-in user's {id, username, role} from the session, or
    None if not logged in.

    Used by: app.py's context processors (current_user template var),
    request_context.py's get_current_user_info(), and routes/favourites.py
    and routes/queue.py to attribute actions to the current user.
    """
    uid = session.get("user_id")
    if uid is None:
        return None
    return {
        "id": uid,
        "username": session.get("user_name", ""),
        "role": session.get("user_role", "user"),
    }


def refresh_session_role():
    """Re-check the user's role from the DB periodically (every 15s)."""
    uid = session.get("user_id")
    if uid is None:
        return None
    last_check = session.get("_role_checked", 0)
    if time.time() - last_check < 15:
        return None
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            session.clear()
            return redirect(url_for("auth.login"))
        session["user_role"] = row["role"]
        session["_role_checked"] = time.time()
    finally:
        conn.close()
    return None


def login_required(f):
    """Decorator: require a logged-in session; JSON 401 for API/XHR callers,
    redirect to the login page otherwise.

    Used by: create_app() in app.py, which wraps every registered view
    function not in `_exempt`/`_admin_only` with this at startup (rather
    than via per-route decorators).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("user_id") is None:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    """Decorator: require an admin-role session, re-verifying the role from
    the DB on every call (not just trusting the session) since role changes
    should take effect immediately for sensitive admin routes.

    Used by: the /admin/* routes in this module directly, and by create_app()
    in app.py which applies it to the endpoints listed in `_admin_only`.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user_id")
        if uid is None:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("auth.login"))
        # Always verify role from DB for sensitive admin routes
        conn = get_db()
        try:
            row = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
            if not row:
                session.clear()
                if request.is_json or request.path.startswith("/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect(url_for("auth.login"))
            current_role = row["role"]
            session["user_role"] = current_role
            session["_role_checked"] = time.time()
        finally:
            conn.close()
        if current_role != "admin":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "admin access required"}), 403
            return redirect(url_for("index"))
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"], error_message="Too many login attempts. Please wait a minute and try again.")
def login():
    force_sso = current_app.config.get("FORCE_SSO", False)
    oidc_enabled = current_app.config.get("OIDC_ENABLED", False)
    oidc_display_name = current_app.config.get("OIDC_DISPLAY_NAME", "SSO")

    if not force_sso and not has_any_admin():
        return redirect(url_for("auth.setup"))

    error = None
    if request.method == "POST" and not force_sso:
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user, err = verify_user(username, password)
        if user:
            session.clear()  # Prevent session fixation
            session.permanent = True
            session["user_id"] = user["id"]
            session["user_name"] = user["username"]
            session["user_role"] = user["role"]
            from .db import get_user_language as _get_lang
            session["ui_language"] = _get_lang(user["id"])
            session["_lang_synced"] = True
            return redirect(url_for("index"))
        error = err

    resp = make_response(render_template(
        "login.html",
        error=error,
        oidc_enabled=oidc_enabled,
        oidc_display_name=oidc_display_name,
        force_sso=force_sso,
        ui_language=session.get("ui_language", "en"),
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/setup", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"], error_message="Zu viele Versuche. Bitte eine Minute warten.")
def setup():
    import time as _time
    if current_app.config.get("FORCE_SSO", False):
        return redirect(url_for("auth.login"))

    if has_any_admin():
        return redirect(url_for("auth.login"))

    # Allow language pre-selection via ?lang= query param (before login)
    lang_param = request.args.get("lang", "").strip()
    if lang_param in ("en", "de"):
        session["ui_language"] = lang_param

    # Setup token protection 
    expected_token = current_app.config.get("SETUP_TOKEN")
    if expected_token:
        expires = current_app.config.get("SETUP_TOKEN_EXPIRES", 0)
        if _time.time() > expires:
            return render_template("setup_locked.html", ui_language=session.get("ui_language", "en")), 403
        provided = (request.args.get("token") or request.form.get("setup_token") or "").strip()
        if not provided:
            return render_template("setup_token.html", ui_language=session.get("ui_language", "en"))
        try:
            token_ok = secrets.compare_digest(provided.encode(), expected_token.encode())
        except Exception:
            token_ok = False
        if not token_ok:
            return render_template("setup_locked.html", ui_language=session.get("ui_language", "en")), 403

    setup_token = current_app.config.get("SETUP_TOKEN", "")

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if not username:
            error = "Username is required."
        elif len(username) > 64:
            error = "Username must be at most 64 characters."
        elif not re.match(r"^[a-zA-Z0-9._-]+$", username):
            error = "Username may only contain letters, digits, dots, hyphens, and underscores."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            setup_lang = request.form.get("language", "en")
            if setup_lang not in ("en", "de"):
                setup_lang = "en"
            uid = create_user(username, password, role="admin", language=setup_lang)
            # Clear setup token from config once admin is created
            current_app.config.pop("SETUP_TOKEN", None)
            current_app.config.pop("SETUP_TOKEN_EXPIRES", None)
            session.clear()  # Prevent session fixation
            session.permanent = True
            session["user_id"] = uid
            session["user_name"] = username
            session["user_role"] = "admin"
            session["ui_language"] = setup_lang
            session["_lang_synced"] = True
            return redirect(url_for("index"))

    return render_template("setup.html", error=error, setup_token=setup_token)


# ---------------------------------------------------------------------------
# OIDC routes
# ---------------------------------------------------------------------------


@auth_bp.route("/oidc/login")
def oidc_login():
    if not current_app.config.get("OIDC_ENABLED", False):
        return redirect(url_for("auth.login"))
    try:
        nonce = secrets.token_urlsafe(32)
        session["oidc_nonce"] = nonce
        redirect_uri = url_for("auth.oidc_callback", _external=True)
        return oauth.oidc.authorize_redirect(redirect_uri, nonce=nonce)
    except Exception:
        logger.exception("SSO provider unavailable")
        return render_template(
            "login.html",
            error="SSO provider is currently unavailable. Please try again later.",
            oidc_enabled=current_app.config.get("OIDC_ENABLED", False),
            oidc_display_name=current_app.config.get("OIDC_DISPLAY_NAME", "SSO"),
            force_sso=current_app.config.get("FORCE_SSO", False),
        )


@auth_bp.route("/oidc/callback")
def oidc_callback():
    if not current_app.config.get("OIDC_ENABLED", False):
        return redirect(url_for("auth.login"))

    try:
        token = oauth.oidc.authorize_access_token()
        nonce = session.pop("oidc_nonce", None)
        userinfo = token.get("userinfo")
        if userinfo is None:
            userinfo = oauth.oidc.parse_id_token(token, nonce=nonce)

        subject = userinfo.get("sub", "")
        username = (
            userinfo.get("preferred_username") or userinfo.get("email") or subject
        )
        username = re.sub(r"[^a-zA-Z0-9._-]", "_", username)

        issuer = userinfo.get("iss", "")
        if not issuer:
            cfg = get_oidc_config()
            issuer = cfg["issuer_url"] if cfg else ""

        admin_username = current_app.config.get("OIDC_ADMIN_USER")
        admin_subject = current_app.config.get("OIDC_ADMIN_SUBJECT")

        user = find_or_create_sso_user(
            issuer=issuer,
            subject=subject,
            username=username,
            admin_username=admin_username,
            admin_subject=admin_subject,
        )

        logger.info(
            "SSO login: user=%s subject=%s issuer=%s", username, subject, issuer
        )

        session.clear()  # Prevent session fixation
        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["username"]
        session["user_role"] = user["role"]
        from .db import get_user_language as _get_lang
        session["ui_language"] = _get_lang(user["id"])
        session["_lang_synced"] = True
        return redirect(url_for("index"))

    except ValueError as e:
        return render_template(
            "login.html",
            error=str(e),
            oidc_enabled=current_app.config.get("OIDC_ENABLED", False),
            oidc_display_name=current_app.config.get("OIDC_DISPLAY_NAME", "SSO"),
            force_sso=current_app.config.get("FORCE_SSO", False),
        )
    except Exception:
        logger.exception("SSO login failed")
        return render_template(
            "login.html",
            error="SSO login failed. Please try again or contact an administrator.",
            oidc_enabled=current_app.config.get("OIDC_ENABLED", False),
            oidc_display_name=current_app.config.get("OIDC_DISPLAY_NAME", "SSO"),
            force_sso=current_app.config.get("FORCE_SSO", False),
        )


# ---------------------------------------------------------------------------
# Admin dashboard + API
# ---------------------------------------------------------------------------


@auth_bp.route("/admin")
@admin_required
def admin_dashboard():
    return redirect(url_for("settings_page"))


@auth_bp.route("/admin/api/users")
@admin_required
def admin_list_users():
    return jsonify({"users": list_users()})


@auth_bp.route("/admin/api/users", methods=["POST"])
@admin_required
def admin_create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", "user")

    if not username:
        return jsonify({"error": "Benutzername ist erforderlich"}), 400
    if len(username) > 64:
        return jsonify({"error": "Benutzername darf maximal 64 Zeichen lang sein"}), 400
    if not re.match(r"^[a-zA-Z0-9._-]+$", username):
        return jsonify(
            {
                "error": "Benutzername darf nur Buchstaben, Ziffern, Punkte, Bindestriche und Unterstriche enthalten"
            }
        ), 400
    if len(password) < 8:
        return jsonify({"error": "Passwort muss mindestens 8 Zeichen lang sein"}), 400
    if role not in ("admin", "user"):
        return jsonify({"error": "Ungültige Rolle"}), 400

    try:
        uid = create_user(username, password, role)
        return jsonify({"id": uid, "username": username, "role": role})
    except Exception as e:
        return jsonify({"error": str(e)}), 409


@auth_bp.route("/admin/api/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"error": "Cannot delete your own account"}), 400
    ok, err = delete_user(user_id)
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True})


@auth_bp.route("/admin/api/users/<int:user_id>/role", methods=["PUT"])
@admin_required
def admin_update_role(user_id):
    data = request.get_json(silent=True) or {}
    new_role = data.get("role", "")
    ok, err = update_user_role(user_id, new_role)
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True})
