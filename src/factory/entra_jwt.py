"""Entra ID bearer-token (JWT) verification for the manager. Standard library only.

Used when ``FACTORY_MANAGER_AUTH=entra-jwt`` (the AKS deployment, where there is no
Easy Auth in front of the manager). The checks, in order:

1. The token is three canonical base64url segments, ASCII, at most ``MAX_TOKEN_CHARS``.
2. The header is a JSON object with ``alg`` exactly ``RS256`` (``none``, ``HS*`` and every
   other algorithm are refused before any key is looked up), a ``kid`` and no ``crit``.
3. The signature verifies against the tenant's published key for that ``kid``. The
   verification is RSASSA-PKCS1-v1_5 with SHA-256 done by hand: the signature must be
   exactly as long as the modulus, and the full encoded message that comes out of the
   public-key operation is compared, in constant time, with the one built from the
   signing input (``00 01 FF..FF 00 DigestInfo``). There is no padding parser, so there
   is nothing lenient to exploit.
4. Only then is the payload read, and ``iss``, ``aud``, ``tid``, ``exp`` and ``nbf`` are
   checked (60 s of clock-skew leeway) and ``roles`` is extracted.

Keys come from the tenant JWKS document. They are cached, re-read when a token names an
unknown ``kid`` (never more often than once per ``min_interval``), and a fetch that fails
or returns nothing usable keeps the failure on record: a token whose key cannot be
obtained is then refused with ``KeysUnavailable`` (the manager answers 503), never
accepted. Every other problem is a ``JwtError`` (401). The reason strings of
``JwtError`` are fixed codes for tests; neither they nor anything else in this module
carries token or claim values, and this module logs nothing itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

LOGIN_HOST = "https://login.microsoftonline.com"
ALGORITHM = "RS256"
LEEWAY_SECONDS = 60.0
MAX_TOKEN_CHARS = 16 * 1024
MAX_KID_CHARS = 128
MAX_JWKS_BYTES = 1024 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
# Cached keys are used for this long before the next token triggers a refresh ...
KEYS_TTL_SECONDS = 3600.0
# ... and are kept for this long if refreshing keeps failing; past that every token is refused.
KEYS_MAX_STALE_SECONDS = 24 * 3600.0
# No fetch (successful or not) starts sooner than this after the previous one.
KEYS_MIN_INTERVAL_SECONDS = 60.0
MIN_MODULUS_BITS = 2048
MAX_MODULUS_BITS = 8192
MAX_EXPONENT_BITS = 32

# DER of DigestInfo for SHA-256 up to (not including) the 32-byte digest (RFC 8017, section 9.2).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_AUDIENCE = re.compile(r"[A-Za-z0-9][\x21-\x7e]*")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


class JwtError(Exception):
    """The token is not acceptable. ``reason`` is a fixed code, never token content."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class KeysUnavailable(Exception):
    """The signing keys could not be obtained, so the token can be neither accepted nor rejected."""


@dataclass(frozen=True)
class Identity:
    """What a verified token says about its bearer."""

    ident: str
    roles: frozenset


@dataclass(frozen=True)
class RsaKey:
    n: int
    e: int


class KeySource(Protocol):
    def key(self, kid: str) -> RsaKey: ...


# --- configuration -----------------------------------------------------------------


def validate_tenant(text: str) -> str:
    """The tenant GUID, lower-cased. Domain names are refused: ``iss`` and ``tid`` carry the GUID."""
    value = (text or "").strip()
    if not _GUID.fullmatch(value):
        raise ValueError("must be a tenant id (a GUID)")
    return value.lower()


def validate_audiences(text: str) -> tuple:
    """The accepted ``aud`` values from a comma-separated list (client id GUID and/or ``api://...``)."""
    audiences: list = []
    for part in (text or "").split(","):
        value = part.strip()
        if not value:
            continue
        if len(value) > 256 or not _AUDIENCE.fullmatch(value) or "<" in value or ">" in value:
            raise ValueError("must be a comma-separated list of client ids or application id URIs")
        guid = value[len("api://") :] if value.lower().startswith("api://") else value
        if _GUID.fullmatch(guid):
            value = value.lower()  # tokens carry GUIDs in lower case
        if value not in audiences:
            audiences.append(value)
    if not audiences:
        raise ValueError("must list at least one audience")
    return tuple(audiences)


def validate_client_id(text: str) -> str:
    """The client id (a GUID) of the app registration the browser page signs in with, lower-cased; empty means unset."""
    value = (text or "").strip()
    if not value:
        return ""
    if not _GUID.fullmatch(value):
        raise ValueError("must be a client id (a GUID)")
    return value.lower()


_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:/-]{0,199}")


def validate_scope(text: str) -> str:
    """The one scope the browser page asks for (for example ``api://<client id>/.default``); empty means unset."""
    value = (text or "").strip()
    if not value:
        return ""
    if not _SCOPE.fullmatch(value) or "//" in value.replace("://", "", 1):
        raise ValueError("must be one scope such as api://<client id>/.default")
    return value


def issuer(tenant: str) -> str:
    return f"{LOGIN_HOST}/{tenant}/v2.0"


def jwks_url(tenant: str) -> str:
    return f"{LOGIN_HOST}/{tenant}/discovery/v2.0/keys"


# --- encoding helpers --------------------------------------------------------------


def _b64url(segment: str) -> bytes:
    """Decode one canonical, unpadded base64url segment; anything else is malformed."""
    if not _BASE64URL.fullmatch(segment) or len(segment) % 4 == 1:
        raise JwtError("malformed")
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    # Reject non-canonical encodings (non-zero trailing bits), so one signature has one spelling.
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != segment:
        raise JwtError("malformed")
    return raw


def _no_duplicates(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constants(name: str) -> None:
    raise ValueError("non-finite number")


def _json_object(raw: bytes) -> dict:
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except (ValueError, RecursionError):  # bad UTF-8, bad JSON, duplicates, NaN
        raise JwtError("malformed") from None
    if not isinstance(document, dict):
        raise JwtError("malformed")
    return document


# --- RSA ---------------------------------------------------------------------------


def verify_rs256(key: RsaKey, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 / SHA-256, by comparing the complete expected encoded message."""
    size = (key.n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= key.n:
        return False
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    if size < len(digest_info) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    actual = pow(value, key.e, key.n).to_bytes(size, "big")
    return hmac.compare_digest(actual, expected)


def parse_jwks(raw: bytes) -> dict:
    """``{kid: RsaKey}`` from a JWKS document. Unusable entries are skipped; a bad document is a ValueError."""
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except (UnicodeDecodeError, RecursionError):
        raise ValueError("unreadable key set") from None
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("key set has no keys list")
    keys: dict = {}
    ambiguous: set = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > MAX_KID_CHARS:
            continue
        if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
            continue
        try:
            key = RsaKey(int.from_bytes(_b64url(entry["n"]), "big"), int.from_bytes(_b64url(entry["e"]), "big"))
        except (KeyError, TypeError, JwtError):
            continue
        if not MIN_MODULUS_BITS <= key.n.bit_length() <= MAX_MODULUS_BITS or key.n % 2 == 0:
            continue
        if key.e < 3 or key.e % 2 == 0 or key.e.bit_length() > MAX_EXPONENT_BITS:
            continue
        if kid in keys and keys[kid] != key:
            ambiguous.add(kid)  # one kid, two different keys: trust neither
        keys[kid] = key
    return {kid: key for kid, key in keys.items() if kid not in ambiguous}


# --- key cache ---------------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None


def fetch_https(url: str) -> bytes:
    """GET an https URL: bounded time and size, no redirects. The default JWKS fetcher."""
    if not url.startswith("https://"):
        raise ValueError("https only")
    opener = urllib.request.build_opener(_NoRedirects)
    with opener.open(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        body = response.read(MAX_JWKS_BYTES + 1)
    if len(body) > MAX_JWKS_BYTES:
        raise ValueError("response too large")
    return body


class JwksCache:
    """The tenant's signing keys: cached, refreshed on demand, closed when they cannot be refreshed.

    ``fetch(url) -> bytes`` is injectable so tests never touch the network. ``report`` receives the
    exception type name of a failed refresh (nothing else) so the owner can log it.
    """

    def __init__(
        self,
        url: str,
        fetch: Callable[[str], bytes] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = KEYS_TTL_SECONDS,
        max_stale: float = KEYS_MAX_STALE_SECONDS,
        min_interval: float = KEYS_MIN_INTERVAL_SECONDS,
        report: Callable[[str], None] | None = None,
    ):
        self._url = url
        self._fetch = fetch or fetch_https
        self._clock = clock
        self._ttl = ttl
        self._max_stale = max_stale
        self._min_interval = min_interval
        self._report = report
        self._mutex = threading.Lock()  # guards the fields below; never held across a fetch
        self._fetch_lock = threading.Lock()  # one fetch at a time
        self._keys: dict = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._last_ok = False

    def key(self, kid: str) -> RsaKey:
        """The key for ``kid``; JwtError if the tenant does not publish it, KeysUnavailable if that is unknowable."""
        found = self._cached(kid)
        if found is not None:
            return found
        with self._fetch_lock:
            found = self._cached(kid)  # another request may have refreshed while this one waited
            if found is not None:
                return found
            if self._refresh_due():
                self._refresh()
        with self._mutex:
            self._expire(self._clock())
            found = self._keys.get(kid)
            usable = self._last_ok and bool(self._keys)
        if found is not None:
            return found  # possibly past its ttl, but a refresh failed or is not due yet
        if not usable:
            raise KeysUnavailable()
        raise JwtError("unknown-kid")

    def _cached(self, kid: str) -> RsaKey | None:
        """The key if it is cached and still within its ttl."""
        with self._mutex:
            now = self._clock()
            self._expire(now)
            if self._fetched_at is None or now - self._fetched_at > self._ttl:
                return None
            return self._keys.get(kid)

    def _expire(self, now: float) -> None:
        if self._fetched_at is not None and now - self._fetched_at > self._max_stale:
            self._keys, self._fetched_at, self._last_ok = {}, None, False

    def _refresh_due(self) -> bool:
        with self._mutex:
            return self._attempted_at is None or self._clock() - self._attempted_at >= self._min_interval

    def _refresh(self) -> None:
        try:
            keys = parse_jwks(self._fetch(self._url))
            if not keys:
                raise ValueError("no usable keys")
        except Exception as error:  # fail closed on anything, including a broken injected fetcher
            with self._mutex:
                self._attempted_at = self._clock()
                self._last_ok = False
            if self._report is not None:
                try:
                    self._report(type(error).__name__)
                except Exception:  # a failing logger must not turn into an acceptance or a crash
                    pass
            return
        with self._mutex:
            self._keys = keys
            self._fetched_at = self._attempted_at = self._clock()
            self._last_ok = True


# --- the verifier ------------------------------------------------------------------


class EntraJwtVerifier:
    """Turns a bearer token into an ``Identity`` or raises JwtError / KeysUnavailable."""

    def __init__(
        self,
        tenant: str,
        audiences: tuple,
        keys: KeySource,
        *,
        clock: Callable[[], float] = time.time,
        leeway: float = LEEWAY_SECONDS,
    ):
        self._tenant = validate_tenant(tenant)
        self._issuer = issuer(self._tenant)
        self._audiences = frozenset(validate_audiences(",".join(audiences)))
        self._keys = keys
        self._clock = clock
        self._leeway = leeway

    @classmethod
    def from_config(
        cls,
        tenant: str,
        audiences: tuple,
        *,
        fetch: Callable[[str], bytes] | None = None,
        report: Callable[[str], None] | None = None,
    ) -> "EntraJwtVerifier":
        tenant = validate_tenant(tenant)
        return cls(tenant, audiences, JwksCache(jwks_url(tenant), fetch, report=report))

    def verify(self, token: str) -> Identity:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS or not token.isascii():
            raise JwtError("malformed")
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise JwtError("malformed")
        header = _json_object(_b64url(parts[0]))
        signature = _b64url(parts[2])
        kid = self._check_header(header)
        key = self._keys.key(kid)
        if not verify_rs256(key, f"{parts[0]}.{parts[1]}".encode("ascii"), signature):
            raise JwtError("signature")
        # Nothing below runs on bytes that were not signed by the tenant's key.
        return self._check_claims(_json_object(_b64url(parts[1])))

    @staticmethod
    def _check_header(header: Mapping[str, object]) -> str:
        alg = header.get("alg")
        if not isinstance(alg, str) or alg != ALGORITHM:
            raise JwtError("alg")
        if "crit" in header:
            raise JwtError("crit")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > MAX_KID_CHARS:
            raise JwtError("kid")
        return kid

    def _check_claims(self, claims: Mapping[str, object]) -> Identity:
        if claims.get("iss") != self._issuer:
            raise JwtError("iss")
        audience = claims.get("aud")
        if not isinstance(audience, str) or audience not in self._audiences:
            raise JwtError("aud")
        tid = claims.get("tid")
        if not isinstance(tid, str) or tid.lower() != self._tenant:
            raise JwtError("tid")
        now = self._clock()
        expires = _timestamp(claims.get("exp"))
        if expires is None or now > expires + self._leeway:
            raise JwtError("exp")
        if "nbf" in claims:
            not_before = _timestamp(claims["nbf"])
            if not_before is None or now < not_before - self._leeway:
                raise JwtError("nbf")
        return Identity(_identifier(claims), _roles(claims))


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        # An integer too large for a float raises OverflowError (in both calls); that is a
        # bad token like any other, not an internal error.
        number = float(value)
        if not math.isfinite(number):
            return None
    except OverflowError:
        return None
    return number


def _identifier(claims: Mapping[str, object]) -> str:
    for name in ("preferred_username", "azp", "oid"):
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _roles(claims: Mapping[str, object]) -> frozenset:
    roles = claims.get("roles")
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(role for role in roles if isinstance(role, str))
