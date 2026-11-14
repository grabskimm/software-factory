#!/usr/bin/env python3
"""Mint a repository-scoped GitHub App installation token.

This is the only code in the project allowed to import ``cryptography`` (for the
RS256 signature). It runs from its own virtualenv so the stdlib-only ``factory``
package never needs the dependency; the supervisor calls it as a subprocess.

    token_broker.py --app-id N --installation-id N --repo org/name [--api-url URL]

The PEM private key is read from the environment variable GITHUB_APP_PRIVATE_KEY
only: never from argv (visible in ``ps`` and /proc), never written to disk, never
printed. On success exactly one JSON line ``{"token": ..., "expires_at": ...}``
goes to stdout and the exit status is 0. On any failure a short message that
cannot contain the key, the JWT, a token or a response body goes to stderr and
the exit status is 1.

GitHub answers a token request that asks for a permission the installation has not
granted with 422 and no permission name. On that status, and only that status, the
broker makes one more read-only request (``GET /app/installations/<id>``, same
JWT, same host) and compares the installation's granted permissions with
PERMISSIONS. If some are missing it prints ``permission missing: actions, issues
(HTTP 422: ...)``, a line built only from our own permission names, so nothing
GitHub sends can reach it. It never retries the token request with a narrower set:
that would hide a rollout that is still incomplete.
"""

import argparse
import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KEY_ENV = "GITHUB_APP_PRIVATE_KEY"
DEFAULT_API = "https://api.github.com"
USER_AGENT = "factory-token-broker"
API_VERSION = "2022-11-28"
# Least privilege for the supervisor: push branches, open PRs, post the gate
# status. Merging is done by GitHub auto-merge, never by this identity.
# "issues": "write" is needed ONLY to maintain the factory:* status labels on the issues
# Factory works on (adding and removing a label is an issue write; there is no narrower
# permission) and to post the one outcome comment on an issue that is blocked, needs a decision or
# failed (the same permission covers issue comments; there is no narrower one), to open a NEW issue
# for what a builder or reviewer observed outside its job and the phase issues of an approved epic plan, with
# the one status comment on the epic (the same permission covers creating issues and comments: no new permission).
# Factory never closes an issue or edits its title or body: src/factory/issue_labels.py (the labels
# endpoints), src/factory/issue_comments.py (the issue comments endpoints),
# src/factory/observed_issues.py (creating an issue and its one label) and src/factory/epic_phases.py (creating a
# phase issue with its one origin label, and the status comment, written through issue_comments.py) are the only code that writes
# to one. (Slice 2 of the phase delivery adds none: the pull request first line of a phase is a pull request body write. Slice 3 adds the
# relabel of a stalled epic's parent from factory:plan-approved to factory:blocked, src/factory/epic_gate.py apply called by
# src/factory/epic_phases.py: a label write under the same permission, so no new permission.)
# "actions": "read" is needed only to READ check results: `gh pr view --json statusCheckRollup`
# asks GraphQL for each check's workflowRun, which answers "Resource not accessible by
# integration" without it (observed live). Without it Factory cannot see a merged or closed
# pull request or a CI failure. It does not allow changing workflows or their files; that is
# the separate "workflows" permission, which the Factory App must never have.
PERMISSIONS = {
    "actions": "read",
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "statuses": "write",
    "checks": "read",
    "metadata": "read",
}
TIMEOUT_SECONDS = 30
# The follow-up read after a 422 only adds detail to an error, so it gets less time.
LOOKUP_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1 << 20
# Access levels from weakest to strongest: a grant of "write" satisfies a request for "read".
_LEVELS = {"read": 1, "write": 2, "admin": 3}
USAGE = (
    "usage: token_broker.py --app-id N --installation-id N --repo org/name "
    "[--api-url URL]; the private key is read from " + KEY_ENV
)
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_REPO = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_HTTP_HINTS = {
    401: " (JWT rejected: check the app id and the private key)",
    403: " (check the app permissions and repository access)",
    404: " (installation or repository not found: check the installation id and repo)",
    422: " (GitHub rejected the requested repository or permissions)",
}


class BrokerError(Exception):
    """A failure whose message is safe to print: it never carries secret data."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse would echo the offending values; a key passed by mistake on
        # the command line must not be copied into stderr.
        raise BrokerError("invalid arguments. " + USAGE)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would resend the Authorization header."""

    def redirect_request(self, *args, **kwargs):
        return None


def parse_args(argv):
    parser = _Parser(prog="token_broker.py", usage=USAGE, allow_abbrev=False)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--installation-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--api-url", default=DEFAULT_API)
    return parser.parse_args(argv)


def _positive_int(value, name):
    if not _NUMBER.fullmatch(value):
        raise BrokerError(f"{name} must be a positive integer")
    return int(value)


def _api_base(url):
    """Accept https (or plain http to loopback for tests); reject credentials in the URL."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise BrokerError("--api-url is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise BrokerError("--api-url must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise BrokerError("--api-url must not contain credentials, a query or a fragment")
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        # The JWT would cross the network in clear text.
        raise BrokerError("--api-url must use https unless it points at loopback")
    return url.rstrip("/")


def validate(args):
    """Check every input before touching the key or the network."""
    app_id = _positive_int(args.app_id, "--app-id")
    installation_id = _positive_int(args.installation_id, "--installation-id")
    match = _REPO.fullmatch(args.repo)
    if not match or match.group(2) in (".", ".."):
        raise BrokerError("--repo must look like org/name")
    return app_id, installation_id, match.group(2), _api_base(args.api_url)


def read_key():
    pem = os.environ.get(KEY_ENV, "")
    if not pem.strip():
        raise BrokerError(f"{KEY_ENV} is not set")
    if "\\n" in pem and "\n" not in pem:
        # Some secret stores flatten a PEM onto one line with literal "\n".
        pem = pem.replace("\\n", "\n")
    return pem.encode()


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id, pem, now):
    """RS256 JWT valid for 10 minutes, back-dated 60 s to tolerate clock skew."""
    try:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise BrokerError("the cryptography package is not installed") from None
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise BrokerError(f"{KEY_ENV} is not a usable unencrypted PEM private key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise BrokerError(f"{KEY_ENV} must hold an RSA private key")
    segments = []
    for part in (
        {"alg": "RS256", "typ": "JWT"},
        {"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
    ):
        segments.append(_b64url(json.dumps(part, separators=(",", ":")).encode()))
    signing_input = ".".join(segments).encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(signature)}"


class _Status(Exception):
    """GitHub answered with an HTTP error status. Its body is deliberately never read."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _headers(jwt, **extra):
    return {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
        **extra,
    }


def _send(request, timeout):
    """Send one request and return the body; redirects are refused and the body is capped.

    A non-2xx answer raises ``_Status`` without reading its body.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise _Status(status) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", None)
        name = type(reason if isinstance(reason, BaseException) else error).__name__
        raise BrokerError(f"cannot reach the GitHub API ({name})") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BrokerError("GitHub API response is too large")
    return raw


def missing_permissions(granted):
    """Our PERMISSIONS names that ``granted`` (an installation's permission map) does not cover.

    Sorted, and drawn only from PERMISSIONS: a name in ``granted`` is looked up, never copied.
    Returns [] when ``granted`` is not a usable map, so the caller keeps its generic message.
    """
    if not isinstance(granted, dict) or not granted:
        return []
    missing = []
    for name, wanted in PERMISSIONS.items():
        have = granted.get(name)
        if _LEVELS.get(have if isinstance(have, str) else "", 0) < _LEVELS[wanted]:
            missing.append(name)
    return sorted(missing)


def _lookup_missing(api, installation_id, jwt):
    """Ask GitHub which requested permissions the installation lacks; [] when that cannot be told."""
    request = urllib.request.Request(
        f"{api}/app/installations/{installation_id}", method="GET", headers=_headers(jwt)
    )
    try:
        document = json.loads(_send(request, LOOKUP_TIMEOUT_SECONDS))
    except (_Status, BrokerError, ValueError, RecursionError):
        return []
    return missing_permissions(document.get("permissions")) if isinstance(document, dict) else []


def request_token(api, installation_id, repo_name, jwt):
    """POST the token request once; the body is never echoed, whatever GitHub answers."""
    body = json.dumps({"repositories": [repo_name], "permissions": PERMISSIONS})
    request = urllib.request.Request(
        f"{api}/app/installations/{installation_id}/access_tokens",
        data=body.encode(),
        method="POST",
        headers=_headers(jwt, **{"Content-Type": "application/json"}),
    )
    try:
        raw = _send(request, TIMEOUT_SECONDS)
    except _Status as error:
        status = error.status
        # A 422 may mean the installation has not accepted a permission. Say which one; never
        # retry with fewer permissions.
        missing = _lookup_missing(api, installation_id, jwt) if status == 422 else []
        if missing:
            raise BrokerError(
                f"permission missing: {', '.join(missing)} (HTTP 422: the installation has not granted them)"
            ) from None
        raise BrokerError(f"GitHub API returned HTTP {status}{_HTTP_HINTS.get(status, '')}") from None
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    token = document.get("token") if isinstance(document, dict) else None
    expires_at = document.get("expires_at") if isinstance(document, dict) else None
    if not (isinstance(token, str) and token and isinstance(expires_at, str) and expires_at):
        raise BrokerError("GitHub API returned an unexpected response")
    return token, expires_at


def _no_core_dumps():
    """A crash must not leave the key (in memory) in a core file."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError) as error:
        print(
            f"token_broker: could not disable core dumps: {type(error).__name__}",
            file=sys.stderr,
        )


def main(argv=None):
    try:
        _no_core_dumps()
        args = parse_args(sys.argv[1:] if argv is None else argv)
        app_id, installation_id, repo_name, api = validate(args)
        jwt = make_jwt(app_id, read_key(), int(time.time()))
        token, expires_at = request_token(api, installation_id, repo_name, jwt)
        sys.stdout.write(json.dumps({"token": token, "expires_at": expires_at}, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except BrokerError as error:
        print(f"token_broker: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # never let a traceback (and its locals' text) escape
        print(f"token_broker: unexpected error ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
