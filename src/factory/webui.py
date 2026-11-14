"""The browser page of the dashboard: three static files shipped in the package and served at ``/ui/``.

The page (``ui/index.html``, ``ui/app.js``, ``ui/style.css`` and the vendored Microsoft sign-in library
``ui/vendor/msal-browser.min.js``) has no build step and loads no third-party script, style or font. It calls
the manager's own read-only JSON endpoints (``dashboard.py``) with a bearer token. With ``FACTORY_MANAGER_AUTH=
entra-jwt`` the page gets that token by signing in with Entra ID (MSAL, authorization code flow with PKCE,
redirect flow); in the other modes the operator pastes a token (docs/operations.md, "The dashboard in a browser").

Serving rules (docs/manager.md, "The browser page"):

* **Only an allow-list is served.** The request path is compared, as the raw string, with the paths of ``SHELL``;
  there is no directory listing, no path is ever joined to a request, and a percent-encoded or dotted variant
  matches nothing. Each file is opened without following a symlink, must be a regular file under a size cap, and is
  read once into memory.
* **The shell carries no data and no secret**, so it is the one thing the manager serves without a token: a browser
  navigation cannot send a bearer header, and the page must load before it can ask for one. Every data call the
  page makes is an ordinary authenticated API call (401 without a token, 403 without a Factory role).
* **One small public config route**, ``GET /ui/config.json``, tells the page whether and how to sign in: the tenant
  id, the client id, the authority and the API scope, all built from the manager's own Entra settings. They are not
  secrets (every sign-in URL carries them) and the route never carries anything else (``sign_in_config``).
* **Strict headers** on every answer: a content security policy that allows nothing but this origin's own files
  (plus, only when Entra sign-in is on, the one Entra login origin for ``connect-src``), ``nosniff``, no referrer,
  no framing, a permissions policy that denies every feature the page does not use, and a same-origin opener
  policy (the redirect flow opens no window). The page is never cached; an asset is cached for a year only under
  the ``?v=<hash>`` address the page itself uses (the hash of the file), so a new image is picked up at once.

Stdlib only. Importing this module has no side effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from .entra_jwt import LOGIN_HOST, validate_tenant

PAGE = "/ui/"
PAGE_REDIRECT = "/ui"  # the same page without the trailing slash: a fixed redirect
ROOT = Path(__file__).resolve().parent / "ui"
MAX_ASSET_BYTES = 512 * 1024

CONFIG = "/ui/config.json"  # generated from the manager's settings, never read from a file
# (URL path, file name, content type). The only paths that are ever served from files; ``index.html`` is a
# template whose ``__VERSION_*__`` markers become the versions of the three scripts and styles it links.
SHELL = (
    (PAGE, "index.html", "text/html; charset=utf-8"),
    ("/ui/app.js", "app.js", "text/javascript; charset=utf-8"),
    ("/ui/style.css", "style.css", "text/css; charset=utf-8"),
    ("/ui/vendor/msal-browser.min.js", "vendor/msal-browser.min.js", "text/javascript; charset=utf-8"),
)
PLACEHOLDERS = {
    "app.js": b"__VERSION_APP__",
    "style.css": b"__VERSION_STYLE__",
    "vendor/msal-browser.min.js": b"__VERSION_MSAL__",
}

# The one origin the page may talk to besides its own: Entra's sign-in endpoint, the same constant the token
# verifier takes its issuer from (entra_jwt.py). Never a wildcard, and only added when sign-in is on.
LOGIN_ORIGIN = LOGIN_HOST
_ORIGIN = re.compile(r"https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
CSP_TEMPLATE = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src {connect}; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Every browser feature the page does not use is denied. (Sign-in happens on Entra's own page, at the top level,
# where this policy does not apply.)
PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer", "autoplay", "bluetooth", "browsing-topics", "camera", "clipboard-read", "clipboard-write",
        "display-capture", "encrypted-media", "fullscreen", "gamepad", "geolocation", "gyroscope", "hid",
        "idle-detection", "local-fonts", "magnetometer", "microphone", "midi", "payment", "picture-in-picture",
        "publickey-credentials-create", "publickey-credentials-get", "screen-wake-lock", "serial", "usb",
        "web-share", "xr-spatial-tracking",
    )
)
HSTS = "max-age=31536000"  # one year, this host only: no includeSubDomains, no preload


def csp(login_origin: str | None = None) -> str:
    """The content security policy; ``login_origin`` (one exact https origin) is added to ``connect-src`` only."""
    if login_origin is not None and not _ORIGIN.fullmatch(login_origin):
        raise ValueError("the login origin must be one exact https origin")
    return CSP_TEMPLATE.format(connect="'self'" if login_origin is None else f"'self' {login_origin}")


def security_headers(login_origin: str | None = None) -> dict:
    return {
        "Content-Security-Policy": csp(login_origin),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Permissions-Policy": PERMISSIONS_POLICY,
    }


CSP = csp()
SECURITY_HEADERS = security_headers()  # without sign-in: connect-src is this origin only
IMMUTABLE = "public, max-age=31536000, immutable"
_VERSION_QUERY = re.compile(r"v=([0-9a-f]{16})")


@dataclass(frozen=True)
class Asset:
    """One served file: its bytes, content type and the version its address carries."""

    path: str
    body: bytes
    content_type: str
    version: str


def split_target(target: str) -> tuple:
    """(path, query) of a request target: the fragment is never sent, and the query is not decoded."""
    path, _, query = target.split("#", 1)[0].partition("?")
    return path, query


def addresses(path: str) -> bool:
    """Whether a request path is one of the page's own: the page, its redirect, the config route or an allow-listed asset."""
    return path in (PAGE_REDIRECT, CONFIG) or any(path == known for known, _, _ in SHELL)


def read_file(directory: Path, name: str) -> bytes | None:
    """The bytes of a regular file of the shell (never through a symlink, at most ``MAX_ASSET_BYTES``), else None."""
    try:
        descriptor = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ASSET_BYTES:
                return None
            raw = handle.read(MAX_ASSET_BYTES + 1)
    except OSError:
        return None
    return raw if len(raw) <= MAX_ASSET_BYTES else None


def version_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


def load(directory: Path = ROOT) -> dict | None:
    """``{url path: Asset}`` for the whole shell, or None when any file is missing or unusable (it never serves half a page)."""
    files = {}
    for _, name, _ in SHELL:
        body = read_file(directory, name)
        if body is None:
            return None
        files[name] = body
    versions = {name: version_of(files[name]).encode("ascii") for name in PLACEHOLDERS}
    page = files["index.html"]
    for name, placeholder in PLACEHOLDERS.items():
        page = page.replace(placeholder, versions[name])
    files["index.html"] = page
    return {
        path: Asset(path, files[name], content_type, version_of(files[name]))
        for path, name, content_type in SHELL
    }


class Shell:
    """The page's files, read on first use and kept; a missing or unusable file is retried on the next request."""

    def __init__(self, directory: Path = ROOT):
        self.directory = directory
        self._lock = threading.Lock()
        self._assets: dict | None = None

    def asset(self, path: str) -> Asset | None:
        with self._lock:
            if self._assets is None:
                self._assets = load(self.directory)
            return None if self._assets is None else self._assets.get(path)


def headers_for(asset: Asset, query: str = "", login_origin: str | None = None) -> dict:
    """The response headers of one file: its type, the security headers and a cache rule.

    The page is never cached. An asset is cached for a year only when the request names the version of the
    file being served (``?v=<hash>``, the address the page links); any other address is revalidated every time.
    """
    named = _VERSION_QUERY.fullmatch(query)
    if asset.path == PAGE:
        cache = "no-store"
    elif named is not None and named.group(1) == asset.version:
        cache = IMMUTABLE
    else:
        cache = "no-cache"
    return {"Content-Type": asset.content_type, "Cache-Control": cache, **security_headers(login_origin)}


def redirect_headers(login_origin: str | None = None) -> dict:
    """The headers of ``/ui`` -> ``/ui/``: a fixed, relative location and an empty body."""
    return {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "Location": PAGE,
        **security_headers(login_origin),
    }


# --- the sign-in configuration of the page -----------------------------------------------------

MODE_ENTRA = "entra"  # the page signs in with Entra ID (MSAL) and the manager verifies the token
MODE_TOKEN = "token"  # easy-auth or local mode: the page takes a token the operator pastes
MODE_UNCONFIGURED = "unconfigured"  # entra-jwt, but the client id of the page cannot be told: no sign-in, no paste
CONFIG_VERSION = 1
CONFIG_KEYS_ENTRA = ("authority", "client_id", "mode", "scope", "tenant_id", "v")
CONFIG_KEYS_OTHER = ("mode", "v")
_GUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def derive_client_id(audiences: tuple) -> str:
    """The first audience that is a client id (a bare GUID, or ``api://<GUID>``), else the empty string."""
    for audience in audiences:
        guid = audience[len("api://") :] if audience.startswith("api://") else audience
        if _GUID.fullmatch(guid):
            return guid
    return ""


def derive_scope(client_id: str, audiences: tuple) -> str:
    """``<resource>/.default`` of the API the manager accepts tokens for.

    ``.default`` asks for the permissions already granted to the page's own registration and, for a user, puts
    the app roles in the ``roles`` claim, so no scope has to be exposed. The API is normally the page's own
    registration (a single-page-application redirect URI on it). **An application asking for a token for itself
    must name the resource by its client id (a GUID), not by its application id URI**: with
    ``api://<client id>/.default`` Entra answers ``invalid_request`` / AADSTS90009 ("is requesting a token for
    itself. This scenario is supported only if resource is specified using the GUID based App Identifier"),
    which the page shows as "The Microsoft sign-in did not complete (code: invalid_request)". The token is the
    same either way (a v2 token's ``aud`` is the client id, which the manager accepts). Another ``api://`` URI in
    the audiences names a different registration, so it is used as written.
    """
    if f"api://{client_id}" in audiences:
        return f"{client_id}/.default"
    for audience in audiences:
        if audience.startswith("api://"):
            return f"{audience}/.default"
    return f"{client_id}/.default"


def sign_in_config(jwt_mode: bool, tenant: str, audiences: tuple, client_id: str = "", scope: str = "") -> dict:
    """The body of ``GET /ui/config.json``: exactly the keys of ``CONFIG_KEYS_ENTRA``, or ``CONFIG_KEYS_OTHER``.

    Nothing here is a secret and nothing is read from a request. ``client_id`` and ``scope`` are the optional
    explicit settings; empty means "derive it from the audiences the manager accepts".
    """
    if not jwt_mode:
        return {"v": CONFIG_VERSION, "mode": MODE_TOKEN}
    chosen = client_id or derive_client_id(audiences)
    if not chosen or not _GUID.fullmatch(chosen):
        return {"v": CONFIG_VERSION, "mode": MODE_UNCONFIGURED}
    checked_tenant = validate_tenant(tenant)
    return {
        "v": CONFIG_VERSION,
        "mode": MODE_ENTRA,
        "tenant_id": checked_tenant,
        "client_id": chosen,
        "authority": f"{LOGIN_ORIGIN}/{checked_tenant}",
        "scope": scope or derive_scope(chosen, audiences),
    }


def config_headers(config: dict) -> dict:
    """The headers of the config route: JSON, never cached, the same security headers as the page."""
    login_origin = LOGIN_ORIGIN if config.get("mode") == MODE_ENTRA else None
    return {"Content-Type": "application/json", "Cache-Control": "no-store", **security_headers(login_origin)}


def config_body(config: dict) -> bytes:
    return json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")
