"""Tests for factory.entra_jwt: RS256 verification, claim checks and the JWKS cache.

No network and no third-party package is needed. The module under test is stdlib-only, so the
tests sign tokens with a tiny RSA implementation of their own (keys are generated in-process
with Python integers). When the ``cryptography`` package is installed, a few interoperability
tests additionally check the verifier against signatures made by a real library and the test
signer against a real verifier; they skip cleanly otherwise.

The helpers at the top (``TestKey``, ``make_token``, ``FakeJwks``, ``Clock``, ...) are also used
by tests/test_manager.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import unittest
import unittest.mock

from factory import entra_jwt as ej
from factory.entra_jwt import EntraJwtVerifier, Identity, JwksCache, JwtError, KeysUnavailable, RsaKey

TENANT = "00000000-0000-4000-8000-0000000000a1"
OTHER_TENANT = "00000000-0000-4000-8000-0000000000b2"
CLIENT_ID = "00000000-0000-4000-8000-0000000000c3"
AUDIENCE_URI = f"api://{CLIENT_ID}"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
NOW = 1_800_000_000.0
INVALID_UTF8 = b"\xff\xfe"
DUPLICATE_ALG = b'{"alg":"none","alg":"RS256","kid":"kid-main"}'
KID = "kid-main"

# --- a tiny RSA, for tests only ----------------------------------------------------

_SMALL_PRIMES = [p for p in range(3, 2000) if all(p % q for q in range(2, int(p**0.5) + 1))]


def _probably_prime(n: int, rounds: int = 24) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _probably_prime(candidate):
            return candidate


class TestKey:
    """An RSA key pair generated now; ``sign`` is RSASSA-PKCS1-v1_5 with SHA-256."""

    def __init__(self, bits: int = 2048, kid: str = KID):
        self.kid = kid
        e = 65537
        while True:
            p, q = _prime(bits // 2), _prime(bits // 2)
            n = p * q
            if p != q and n.bit_length() == bits and (p - 1) % e and (q - 1) % e:
                break
        self.n, self.e = n, e
        self.d = pow(e, -1, (p - 1) * (q - 1))
        self.size = (n.bit_length() + 7) // 8

    @property
    def public(self) -> RsaKey:
        return RsaKey(self.n, self.e)

    @staticmethod
    def encode(message: bytes, size: int) -> bytes:
        """The EMSA-PKCS1-v1_5 encoding, written out independently of the module under test."""
        digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
        return b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info

    def sign_encoded(self, encoded: bytes) -> bytes:
        return pow(int.from_bytes(encoded, "big"), self.d, self.n).to_bytes(self.size, "big")

    def sign(self, message: bytes) -> bytes:
        return self.sign_encoded(self.encode(message, self.size))

    def jwk(self, kid: str | None = None, **overrides) -> dict:
        entry = {"kty": "RSA", "use": "sig", "kid": kid or self.kid, "n": b64(_int_bytes(self.n)), "e": b64(_int_bytes(self.e))}
        entry.update(overrides)
        return entry


def _int_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


_KEYS: dict = {}


def key(label: str = "main", bits: int = 2048) -> TestKey:
    """A cached key per label, so each test run generates a handful of keys at most."""
    if (label, bits) not in _KEYS:
        _KEYS[(label, bits)] = TestKey(bits, kid=f"kid-{label}")
    return _KEYS[(label, bits)]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64json(document) -> str:
    return b64(json.dumps(document, separators=(",", ":")).encode())


def claims(**overrides) -> dict:
    """Claims of a good token for NOW; pass ``name=None`` to remove one."""
    document = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "tid": TENANT,
        "iat": NOW - 10,
        "nbf": NOW - 10,
        "exp": NOW + 3000,
        "roles": ["Factory.Operator"],
        "preferred_username": "operator@example.invalid",
        "oid": "11111111-2222-3333-4444-555555555555",
    }
    document.update(overrides)
    return {name: value for name, value in document.items() if value is not None}


def make_token(signer: TestKey | None = None, payload=None, header=None, *, kid: str | None = None) -> str:
    """A signed RS256 token. ``payload`` and ``header`` replace the good defaults when given."""
    signer = signer or key()
    head = header if header is not None else {"alg": "RS256", "typ": "JWT", "kid": kid or signer.kid}
    body = payload if payload is not None else claims()
    signing_input = f"{b64json(head)}.{b64json(body)}"
    return f"{signing_input}.{b64(signer.sign(signing_input.encode()))}"


def jwks(*keys: TestKey) -> bytes:
    return json.dumps({"keys": [k.jwk() for k in keys]}).encode()


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeJwks:
    """A scripted JWKS endpoint: a body to serve, or an error to raise."""

    def __init__(self, *keys: TestKey):
        self.body = jwks(*keys) if keys else b'{"keys":[]}'
        self.error: Exception | None = None
        self.urls: list = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.body

    def serve(self, *keys: TestKey) -> None:
        self.body, self.error = jwks(*keys), None

    def fail(self, error: Exception | None = None) -> None:
        self.error = error or OSError("jwks endpoint down")


def make_verifier(fetch: FakeJwks, clock: Clock | None = None, wall: Clock | None = None, **cache_options):
    """A verifier whose cache and wall clock are both scripted. Returns (verifier, cache)."""
    cache = JwksCache(JWKS_URL, fetch, clock=clock or Clock(1000.0), **cache_options)
    return EntraJwtVerifier(TENANT, (CLIENT_ID, AUDIENCE_URI), cache, clock=wall or Clock()), cache


def reason(case: unittest.TestCase, verifier: EntraJwtVerifier, token: str) -> str:
    with case.assertRaises(JwtError) as raised:
        verifier.verify(token)
    return raised.exception.reason


# --- the signature -----------------------------------------------------------------


class SignatureTests(unittest.TestCase):
    def test_a_correct_signature_verifies(self):
        message = b"header.payload"
        self.assertTrue(ej.verify_rs256(key().public, message, key().sign(message)))

    def test_a_signature_over_other_bytes_or_by_another_key_fails(self):
        signature = key().sign(b"one message")
        self.assertFalse(ej.verify_rs256(key().public, b"another message", signature))
        self.assertFalse(ej.verify_rs256(key("other").public, b"one message", signature))

    def test_wrong_signature_lengths_and_values_fail(self):
        k, message = key(), b"m"
        signature = k.sign(message)
        value = int.from_bytes(signature, "big")
        self.assertFalse(ej.verify_rs256(k.public, message, signature[1:]))  # shorter
        self.assertFalse(ej.verify_rs256(k.public, message, b"\x00" + signature))  # longer
        self.assertFalse(ej.verify_rs256(k.public, message, (value + k.n).to_bytes(k.size + 1, "big")))
        self.assertFalse(ej.verify_rs256(k.public, message, b"\x00" * k.size))
        self.assertFalse(ej.verify_rs256(k.public, message, b""))

    def test_a_signature_representative_not_below_the_modulus_is_rejected(self):
        """s and s + n are the same number mod n; only the first spelling is a signature."""
        wide = TestKey(2048)
        while wide.n * 5 > 4 * 2**2048:  # a modulus with room below 2^2048 for s + n
            wide = TestKey(2048)
        for index in range(500):
            message = f"message {index}".encode()
            value = int.from_bytes(wide.sign(message), "big")
            if value + wide.n < 256**wide.size:
                break
        else:
            self.fail("no signature small enough to add the modulus to")
        self.assertTrue(ej.verify_rs256(wide.public, message, value.to_bytes(wide.size, "big")))
        self.assertFalse(ej.verify_rs256(wide.public, message, (value + wide.n).to_bytes(wide.size, "big")))

    def test_only_the_exact_encoded_message_is_accepted(self):
        """Each of these is a validly signed message that a lenient PKCS#1 parser might accept."""
        k, message = key(), b"signed bytes"
        digest = hashlib.sha256(message).digest()
        info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
        no_null = bytes.fromhex("302f300b0609608648016503040201") + b"\x04\x20" + digest  # params field absent
        cases = {
            "digestinfo without the NULL parameters": b"\x00\x01" + b"\xff" * (k.size - len(no_null) - 3) + b"\x00" + no_null,
            "garbage after the digest": b"\x00\x01" + b"\xff" * (k.size - len(info) - 3 - 8) + b"\x00" + info + b"garbage!",
            "short padding, zero filled tail": b"\x00\x01" + b"\xff" * 8 + b"\x00" + info + b"\x00" * (k.size - len(info) - 11),
            "padding byte 00 inside the FF run": b"\x00\x01" + b"\xff" * 10 + b"\x00" + b"\xff" * (k.size - len(info) - 14) + b"\x00" + info,
            "block type 02": b"\x00\x02" + b"\xff" * (k.size - len(info) - 3) + b"\x00" + info,
            "no leading zero": b"\x01" + b"\xff" * (k.size - len(info) - 2) + b"\x00" + info,
            "digest of other bytes": TestKey.encode(b"other bytes", k.size),
            "all padding is missing": b"\x00\x01" + b"\x00" + info + b"\x00" * (k.size - len(info) - 3),
        }
        for label, encoded in cases.items():
            with self.subTest(label):
                self.assertEqual(len(encoded), k.size, label)
                self.assertFalse(ej.verify_rs256(k.public, message, k.sign_encoded(encoded)))
        self.assertTrue(ej.verify_rs256(k.public, message, k.sign_encoded(TestKey.encode(message, k.size))))

    def test_a_modulus_too_small_for_the_encoding_never_verifies(self):
        tiny = RsaKey(3 * 5 * 7 * 11 * 13 * 17 * 19 * 23 * 29 * 31 * 37 * 41 * 43 * 47 + 2, 3)
        self.assertFalse(ej.verify_rs256(tiny, b"m", b"\x01" * ((tiny.n.bit_length() + 7) // 8)))


@unittest.skipUnless(__import__("importlib").util.find_spec("cryptography"), "the cryptography package is not installed")
class InteropTests(unittest.TestCase):
    """The verifier and the test signer against a real RSA implementation."""

    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.rsa = rsa
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.private.public_key().public_numbers()
        self.public = RsaKey(numbers.n, numbers.e)

    def test_a_signature_made_by_the_library_verifies(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        message = b"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0"
        signature = self.private.sign(message, padding.PKCS1v15(), hashes.SHA256())
        self.assertTrue(ej.verify_rs256(self.public, message, signature))
        self.assertFalse(ej.verify_rs256(self.public, message + b"x", signature))

    def test_a_whole_token_signed_by_the_library_is_accepted(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        signing_input = f"{b64json({'alg': 'RS256', 'kid': 'lib'})}.{b64json(claims())}"
        signature = self.private.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        jwk = {"kty": "RSA", "kid": "lib", "n": b64(_int_bytes(self.public.n)), "e": b64(_int_bytes(self.public.e))}
        fetch = FakeJwks()
        fetch.body = json.dumps({"keys": [jwk]}).encode()
        verifier, _ = make_verifier(fetch)
        identity = verifier.verify(f"{signing_input}.{b64(signature)}")
        self.assertEqual(identity.roles, frozenset({"Factory.Operator"}))

    def test_the_test_signer_is_accepted_by_the_library(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        k = key()
        public = self.rsa.RSAPublicNumbers(k.e, k.n).public_key()
        public.verify(k.sign(b"message"), b"message", padding.PKCS1v15(), hashes.SHA256())  # raises if wrong


# --- tokens ------------------------------------------------------------------------


class ValidTokenTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeJwks(key())
        self.verifier, _ = make_verifier(self.fetch)

    def test_a_valid_token_yields_the_identity_and_roles(self):
        identity = self.verifier.verify(make_token(payload=claims(roles=["Factory.Operator", "Factory.Viewer"])))
        self.assertEqual(identity, Identity("operator@example.invalid", frozenset({"Factory.Operator", "Factory.Viewer"})))

    def test_both_configured_audiences_are_accepted(self):
        for audience in (CLIENT_ID, AUDIENCE_URI):
            with self.subTest(audience):
                self.verifier.verify(make_token(payload=claims(aud=audience)))

    def test_the_identifier_falls_back_from_username_to_azp_to_oid(self):
        none = {"preferred_username": None, "azp": None}
        cases = [
            (claims(azp="app-client-id"), "operator@example.invalid"),
            (claims(preferred_username=None, azp="app-client-id"), "app-client-id"),
            (claims(**none), "11111111-2222-3333-4444-555555555555"),
            (claims(oid=None, **none), "unknown"),
            (claims(preferred_username="", azp=7), "11111111-2222-3333-4444-555555555555"),
        ]
        for payload, expected in cases:
            with self.subTest(expected):
                self.assertEqual(self.verifier.verify(make_token(payload=payload)).ident, expected)

    def test_a_token_without_roles_verifies_with_no_roles(self):
        # Authorisation (403) is the manager's job; the token itself is fine.
        for payload in (claims(roles=None), claims(roles="Factory.Operator"), claims(roles={"a": 1}), claims(roles=[1, None, ["x"]])):
            with self.subTest(payload.get("roles")):
                self.assertEqual(self.verifier.verify(make_token(payload=payload)).roles, frozenset())

    def test_non_string_role_entries_are_dropped_and_string_ones_kept(self):
        identity = self.verifier.verify(make_token(payload=claims(roles=["Factory.Viewer", 5, None])))
        self.assertEqual(identity.roles, frozenset({"Factory.Viewer"}))

    def test_the_tenant_id_is_compared_case_insensitively(self):
        self.verifier.verify(make_token(payload=claims(tid=TENANT.upper())))


class AlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeJwks(key())
        self.verifier, _ = make_verifier(self.fetch)

    def unsigned(self, header, signature=""):
        return f"{b64json(header)}.{b64json(claims())}.{signature}"

    def test_alg_none_is_rejected_whatever_the_signature_segment(self):
        for signature in ("", "AAAA", b64(key().sign(b"x"))):
            for alg in ("none", "None", "NONE"):
                with self.subTest(alg=alg, signature=signature[:4]):
                    token = self.unsigned({"alg": alg, "kid": KID}, signature)
                    self.assertIn(reason(self, self.verifier, token), ("alg", "malformed"))
        self.assertEqual(self.fetch.urls, [])  # refused before any key was looked up

    def test_hs256_signed_with_the_public_key_as_the_secret_is_rejected(self):
        """The classic key-confusion attack: the "secret" is the published public key."""
        k = key()
        public_jwk_json = json.dumps(k.jwk()).encode()
        for secret in (_int_bytes(k.n), public_jwk_json, k.jwk()["n"].encode()):
            signing_input = f"{b64json({'alg': 'HS256', 'kid': KID})}.{b64json(claims())}"
            signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
            with self.subTest(secret=len(secret)):
                self.assertEqual(reason(self, self.verifier, f"{signing_input}.{b64(signature)}"), "alg")
        self.assertEqual(self.fetch.urls, [])

    def test_every_other_algorithm_and_spelling_is_rejected(self):
        for alg in ("HS256", "HS384", "HS512", "RS384", "RS512", "PS256", "ES256", "EdDSA", "rs256", "RS256 ", "", None, 256, ["RS256"]):
            with self.subTest(alg=alg):
                token = make_token(header={"alg": alg, "kid": KID})
                self.assertEqual(reason(self, self.verifier, token), "alg")
        self.assertEqual(reason(self, self.verifier, make_token(header={"kid": KID})), "alg")
        self.assertEqual(self.fetch.urls, [])

    def test_a_crit_header_is_rejected(self):
        token = make_token(header={"alg": "RS256", "kid": KID, "crit": ["exp"], "exp": 1})
        self.assertEqual(reason(self, self.verifier, token), "crit")

    def test_headers_that_point_at_keys_are_never_followed(self):
        # jku / x5u / jwk / x5c in the header are ignored: the key comes from the tenant's JWKS only.
        attacker = key("attacker")
        header = {"alg": "RS256", "kid": KID, "jku": "https://attacker.invalid/keys", "jwk": attacker.jwk(KID), "x5u": "https://attacker.invalid/c"}
        token = make_token(attacker, header=header)
        self.assertEqual(reason(self, self.verifier, token), "signature")
        self.assertEqual(self.fetch.urls, [JWKS_URL])

    def test_a_missing_or_odd_kid_is_rejected(self):
        for kid in (None, "", 5, ["kid-main"], "k" * (ej.MAX_KID_CHARS + 1)):
            with self.subTest(kid=kid):
                header = {"alg": "RS256", **({} if kid is None else {"kid": kid})}
                self.assertEqual(reason(self, self.verifier, make_token(header=header)), "kid")


class SignatureCheckTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeJwks(key())
        self.verifier, _ = make_verifier(self.fetch)

    def test_a_tampered_payload_is_rejected(self):
        header, _, signature = make_token(payload=claims(roles=["Factory.Viewer"])).split(".")
        forged = b64json(claims(roles=["Factory.Operator"]))
        self.assertEqual(reason(self, self.verifier, f"{header}.{forged}.{signature}"), "signature")

    def test_a_tampered_header_is_rejected(self):
        _, payload, signature = make_token().split(".")
        forged = b64json({"alg": "RS256", "kid": KID, "typ": "at+jwt"})
        self.assertEqual(reason(self, self.verifier, f"{forged}.{payload}.{signature}"), "signature")

    def test_a_flipped_signature_bit_is_rejected(self):
        header, payload, signature = make_token().split(".")
        raw = bytearray(base64.urlsafe_b64decode(signature + "=="))
        raw[10] ^= 0x01
        self.assertEqual(reason(self, self.verifier, f"{header}.{payload}.{b64(bytes(raw))}"), "signature")

    def test_a_token_signed_by_another_key_under_the_published_kid_is_rejected(self):
        self.assertEqual(reason(self, self.verifier, make_token(key("attacker"), kid=KID)), "signature")

    def test_a_token_with_a_different_signature_spelling_is_rejected(self):
        header, payload, signature = make_token().split(".")
        for bad in (signature + "=", signature + "A", signature[:-1], signature.replace("-", "+").replace("_", "/") + "!"):
            with self.subTest(bad[-3:]):
                self.assertIn(reason(self, self.verifier, f"{header}.{payload}.{bad}"), ("malformed", "signature"))

    def test_trailing_bits_of_a_segment_must_be_canonical(self):
        header, payload, signature = make_token().split(".")
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        last = alphabet.index(signature[-1])
        # A signature of 256 bytes ends with a 2-bit tail: the 4 low bits of the last char are padding.
        twin = signature[:-1] + alphabet[last ^ 0x01]
        self.assertEqual(base64.urlsafe_b64decode(twin + "=="), base64.urlsafe_b64decode(signature + "=="))
        self.assertEqual(reason(self, self.verifier, f"{header}.{payload}.{twin}"), "malformed")


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeJwks(key())
        self.wall = Clock()
        self.verifier, _ = make_verifier(self.fetch, wall=self.wall)

    def check(self, expected, **overrides):
        self.assertEqual(reason(self, self.verifier, make_token(payload=claims(**overrides))), expected, overrides)

    def test_wrong_issuer(self):
        for iss in (
            f"https://sts.windows.net/{TENANT}/",  # a v1.0 token
            f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0",
            ISSUER + "/",
            ISSUER.upper(),
            f"{ISSUER}?x=1",
            None,
            7,
            [ISSUER],
        ):
            with self.subTest(iss=iss):
                self.check("iss", iss=iss)

    def test_wrong_audience(self):
        for aud in ("00000000-0000-4000-8000-0000000000ff", "api://other", CLIENT_ID.upper(), AUDIENCE_URI + "/", "", None, 5, [CLIENT_ID], {"a": CLIENT_ID}):
            with self.subTest(aud=aud):
                self.check("aud", aud=aud)

    def test_wrong_tenant(self):
        for tid in (OTHER_TENANT, "", None, 5, [TENANT], f"{TENANT} "):
            with self.subTest(tid=tid):
                self.check("tid", tid=tid)

    def test_expiry_with_sixty_seconds_of_leeway(self):
        self.verifier.verify(make_token(payload=claims(exp=NOW - 59)))  # inside the leeway
        self.verifier.verify(make_token(payload=claims(exp=NOW - 60)))  # the boundary itself
        self.check("exp", exp=NOW - 61)
        self.check("exp", exp=NOW - 86400)
        self.wall.advance(7200)  # the good token from claims() has now expired
        self.assertEqual(reason(self, self.verifier, make_token()), "exp")

    def test_not_before_with_sixty_seconds_of_leeway(self):
        self.verifier.verify(make_token(payload=claims(nbf=NOW + 59)))
        self.verifier.verify(make_token(payload=claims(nbf=NOW + 60)))
        self.check("nbf", nbf=NOW + 61)
        self.check("nbf", nbf=NOW + 86400)

    def test_missing_or_malformed_timestamps(self):
        for exp in (None, "1900000000", True, [], {}):
            with self.subTest(exp=exp):
                self.check("exp", exp=exp)
        for nbf in ("0", True, [], {}):
            with self.subTest(nbf=nbf):
                self.check("nbf", nbf=nbf)

    def test_non_finite_numbers_are_rejected_even_when_validly_signed(self):
        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            body = ('{"iss":"%s","aud":"%s","tid":"%s","exp":%s,"roles":["Factory.Operator"]}' % (ISSUER, CLIENT_ID, TENANT, literal)).encode()
            head = b64json({"alg": "RS256", "kid": KID})
            signing_input = f"{head}.{b64(body)}"
            token = f"{signing_input}.{b64(key().sign(signing_input.encode()))}"
            with self.subTest(literal):
                self.assertIn(reason(self, self.verifier, token), ("malformed", "exp"))

    def test_integers_too_large_for_a_float_are_a_token_error_not_an_internal_error(self):
        # float(10**400) raises OverflowError; the module's contract is that every token problem
        # is a JwtError (a 401), even when the token is validly signed.
        for field in ("exp", "nbf"):
            for literal in ("9" * 400, "-" + "9" * 400):
                exp = literal if field == "exp" else str(NOW + 100)
                nbf = literal if field == "nbf" else None
                parts = ['"iss":"%s"' % ISSUER, '"aud":"%s"' % CLIENT_ID, '"tid":"%s"' % TENANT, '"exp":%s' % exp, '"roles":["Factory.Operator"]']
                if nbf is not None:
                    parts.append('"nbf":%s' % nbf)
                body = ("{" + ",".join(parts) + "}").encode()
                signing_input = f"{b64json({'alg': 'RS256', 'kid': KID})}.{b64(body)}"
                token = f"{signing_input}.{b64(key().sign(signing_input.encode()))}"
                with self.subTest(field=field, literal=literal[:6]):
                    self.assertIn(reason(self, self.verifier, token), ("malformed", field))

    def test_duplicate_keys_in_a_signed_payload_are_rejected(self):
        body = ('{"iss":"%s","aud":"x","aud":"%s","tid":"%s","exp":%d}' % (ISSUER, CLIENT_ID, TENANT, NOW + 100)).encode()
        signing_input = f"{b64json({'alg': 'RS256', 'kid': KID})}.{b64(body)}"
        token = f"{signing_input}.{b64(key().sign(signing_input.encode()))}"
        self.assertEqual(reason(self, self.verifier, token), "malformed")

    def test_claims_are_not_read_before_the_signature_is_checked(self):
        # An unsigned payload with perfect claims but a bad signature must fail on the signature,
        # never leak "which claim was wrong" for a token anyone could have forged.
        header, payload, _ = make_token(payload=claims(aud="wrong")).split(".")
        forged = f"{header}.{payload}.{b64(b'x' * 256)}"
        self.assertEqual(reason(self, self.verifier, forged), "signature")


class MalformedTokenTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeJwks(key())
        self.verifier, _ = make_verifier(self.fetch)

    def test_things_that_are_not_tokens(self):
        good = make_token()
        header, payload, signature = good.split(".")
        cases = {
            "empty": "",
            "one segment": header,
            "two segments": f"{header}.{payload}",
            "four segments": f"{good}.{signature}",
            "empty signature": f"{header}.{payload}.",
            "empty header": f".{payload}.{signature}",
            "empty payload": f"{header}..{signature}",
            "not base64": "!!!.@@@.###",
            "padded segment": f"{header}==.{payload}.{signature}",
            "standard alphabet": f"{header}.{payload}.{signature}".replace("-", "+") + "+/",
            "whitespace": f" {good}",
            "newline inside": f"{header}\n.{payload}.{signature}",
            "non-ascii": f"{header}.{payload}é.{signature}",
            "header is not json": f"{b64(b'not json')}.{payload}.{signature}",
            "header is a list": f"{b64json([])}.{payload}.{signature}",
            "header is a string": f"{b64json('RS256')}.{payload}.{signature}",
            "header is invalid utf-8": f"{b64(INVALID_UTF8)}.{payload}.{signature}",
            "header has duplicate alg": f"{b64(DUPLICATE_ALG)}.{payload}.{signature}",
            "too long": "a" * (ej.MAX_TOKEN_CHARS + 1),
            "not a string": None,
        }
        for label, token in cases.items():
            with self.subTest(label):
                self.assertEqual(reason(self, self.verifier, token), "malformed")
        self.assertEqual(self.fetch.urls, [])

    def test_a_signed_payload_that_is_not_an_object_is_rejected(self):
        for body in (b"[]", b'"text"', b"5", b"null", b"not json", b"\xff"):
            signing_input = f"{b64json({'alg': 'RS256', 'kid': KID})}.{b64(body)}"
            token = f"{signing_input}.{b64(key().sign(signing_input.encode()))}"
            with self.subTest(body):
                self.assertEqual(reason(self, self.verifier, token), "malformed")

    def test_deeply_nested_json_is_a_clean_rejection(self):
        deep = b"[" * 10_000  # as large as a token may be
        token = f"{b64json({'alg': 'RS256', 'kid': KID})}.{b64(deep)}.{b64(b'x' * 256)}"
        self.assertEqual(reason(self, self.verifier, token), "signature")
        signing_input = f"{b64json({'alg': 'RS256', 'kid': KID})}.{b64(deep)}"
        signed = f"{signing_input}.{b64(key().sign(signing_input.encode()))}"
        self.assertEqual(reason(self, self.verifier, signed), "malformed")


# --- the key set -------------------------------------------------------------------


class JwksParsingTests(unittest.TestCase):
    def parse(self, *entries):
        return ej.parse_jwks(json.dumps({"keys": list(entries)}).encode())

    def test_good_keys_are_returned_by_kid(self):
        parsed = self.parse(key().jwk(), key("other").jwk())
        self.assertEqual(set(parsed), {"kid-main", "kid-other"})
        self.assertEqual(parsed["kid-main"], key().public)

    def test_unusable_entries_are_skipped(self):
        small = key("small", 1024)
        cases = {
            "1024-bit modulus": small.jwk(),
            "not RSA": key().jwk(kty="EC"),
            "encryption key": key().jwk(use="enc"),
            "no kid": {k: v for k, v in key().jwk().items() if k != "kid"},
            "empty kid": {**key().jwk(), "kid": ""},
            "even exponent": key().jwk(e=b64(b"\x01\x00\x00")),
            "exponent 1": key().jwk(e=b64(b"\x01")),
            "huge exponent": key().jwk(e=b64(b"\x01" + b"\x00" * 8 + b"\x01")),
            "bad base64 modulus": key().jwk(n="!!!"),
            "modulus not a string": key().jwk(n=5),
            "missing exponent": {k: v for k, v in key().jwk().items() if k != "e"},
            "even modulus": key().jwk(n=b64(_int_bytes(key().n + 1))),
            "not an object": "nope",
        }
        for label, entry in cases.items():
            with self.subTest(label):
                self.assertEqual(self.parse(entry), {})

    def test_a_kid_published_twice_with_different_keys_is_dropped(self):
        parsed = self.parse(key().jwk(), key("other").jwk(kid=KID), key("third").jwk())
        self.assertEqual(set(parsed), {"kid-third"})
        self.assertEqual(set(self.parse(key().jwk(), key().jwk())), {KID})  # identical repeats are harmless

    def test_documents_that_are_not_key_sets_raise(self):
        for raw in (b"", b"not json", b"[]", b'{"keys":"x"}', b'{"nokeys":[]}', b"\xff", b'{"keys":[],"keys":[]}'):
            with self.subTest(raw):
                with self.assertRaises(ValueError):
                    ej.parse_jwks(raw)


class JwksCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(1000.0)
        self.fetch = FakeJwks(key())
        self.reports: list = []
        self.cache = JwksCache(JWKS_URL, self.fetch, clock=self.clock, report=self.reports.append)

    def test_the_first_lookup_fetches_the_tenant_url_and_later_ones_use_the_cache(self):
        self.assertEqual(self.cache.key(KID), key().public)
        self.cache.key(KID)
        self.clock.advance(ej.KEYS_TTL_SECONDS - 1)
        self.cache.key(KID)
        self.assertEqual(self.fetch.urls, [JWKS_URL])

    def test_the_default_url_is_the_v2_discovery_document_of_the_tenant(self):
        self.assertEqual(ej.jwks_url(TENANT), JWKS_URL)
        self.assertEqual(ej.issuer(TENANT), ISSUER)

    def test_an_unknown_kid_refetches_and_finds_a_rotated_key(self):
        self.cache.key(KID)
        self.fetch.serve(key(), key("rotated"))
        self.clock.advance(ej.KEYS_MIN_INTERVAL_SECONDS)
        self.assertEqual(self.cache.key("kid-rotated"), key("rotated").public)
        self.assertEqual(len(self.fetch.urls), 2)

    def test_unknown_kids_cannot_force_fetches_faster_than_the_minimum_interval(self):
        self.cache.key(KID)
        for index in range(50):
            with self.assertRaises(JwtError) as raised:
                self.cache.key(f"junk-{index}")
            self.assertEqual(raised.exception.reason, "unknown-kid")
        self.assertEqual(len(self.fetch.urls), 1)
        self.clock.advance(ej.KEYS_MIN_INTERVAL_SECONDS - 1)
        with self.assertRaises(JwtError):
            self.cache.key("junk-again")
        self.assertEqual(len(self.fetch.urls), 1)
        self.clock.advance(1)
        with self.assertRaises(JwtError):
            self.cache.key("junk-again")
        self.assertEqual(len(self.fetch.urls), 2)

    def test_a_kid_the_tenant_does_not_publish_is_a_token_error_not_an_outage(self):
        with self.assertRaises(JwtError) as raised:
            self.cache.key("not-published")
        self.assertEqual(raised.exception.reason, "unknown-kid")

    def test_an_outage_at_start_fails_closed_and_is_retried_only_after_the_interval(self):
        self.fetch.fail()
        for _ in range(5):
            with self.assertRaises(KeysUnavailable):
                self.cache.key(KID)
        self.assertEqual(len(self.fetch.urls), 1)
        self.fetch.serve(key())
        with self.assertRaises(KeysUnavailable):
            self.cache.key(KID)  # still inside the interval: no new attempt
        self.clock.advance(ej.KEYS_MIN_INTERVAL_SECONDS)
        self.assertEqual(self.cache.key(KID), key().public)
        self.assertEqual(len(self.fetch.urls), 2)

    def test_every_kind_of_bad_answer_is_an_outage(self):
        bodies = [b"", b"not json", b"[]", b'{"keys":[]}', json.dumps({"keys": [key("small", 1024).jwk()]}).encode()]
        for body in bodies:
            cache = JwksCache(JWKS_URL, lambda url, body=body: body, clock=self.clock)
            with self.subTest(body[:20]):
                with self.assertRaises(KeysUnavailable):
                    cache.key(KID)

    def test_any_exception_from_the_fetcher_is_an_outage(self):
        for error in (OSError("down"), TimeoutError(), ValueError("x"), KeyError("k"), RuntimeError("bug")):
            def broken(url, error=error):
                raise error

            with self.subTest(type(error).__name__):
                with self.assertRaises(KeysUnavailable):
                    JwksCache(JWKS_URL, broken, clock=self.clock).key(KID)

    def test_a_failed_refresh_keeps_known_keys_working_until_they_are_too_old(self):
        self.cache.key(KID)
        self.fetch.fail()
        self.clock.advance(ej.KEYS_TTL_SECONDS + 1)  # past the ttl: a refresh is due, and it fails
        self.assertEqual(self.cache.key(KID), key().public)
        with self.assertRaises(KeysUnavailable):
            self.cache.key("kid-rotated")  # an unknown kid cannot be judged while the endpoint is down
        self.clock.advance(ej.KEYS_MAX_STALE_SECONDS)
        with self.assertRaises(KeysUnavailable):
            self.cache.key(KID)  # too old: fail closed

    def test_a_bad_refresh_does_not_replace_good_keys(self):
        self.cache.key(KID)
        self.fetch.body = b'{"keys":[]}'
        self.clock.advance(ej.KEYS_TTL_SECONDS + 1)
        self.assertEqual(self.cache.key(KID), key().public)

    def test_a_refresh_replaces_the_whole_set_so_withdrawn_keys_disappear(self):
        self.cache.key(KID)
        self.fetch.serve(key("rotated"))
        self.clock.advance(ej.KEYS_TTL_SECONDS + 1)
        with self.assertRaises(JwtError):
            self.cache.key(KID)
        self.assertEqual(self.cache.key("kid-rotated"), key("rotated").public)

    def test_failures_are_reported_by_exception_type_only(self):
        self.fetch.fail(OSError("secret-host.invalid refused the connection"))
        with self.assertRaises(KeysUnavailable):
            self.cache.key(KID)
        self.assertEqual(self.reports, ["OSError"])

    def test_a_failing_report_callback_changes_nothing(self):
        def explode(kind):
            raise RuntimeError("logger is broken")

        cache = JwksCache(JWKS_URL, FakeJwks(), clock=self.clock, report=explode)
        with self.assertRaises(KeysUnavailable):
            cache.key(KID)

    def test_concurrent_lookups_of_an_unknown_kid_make_one_fetch(self):
        gate = threading.Barrier(8)
        results: list = []

        def lookup():
            gate.wait()
            try:
                self.cache.key(KID)
                results.append("ok")
            except Exception as error:
                results.append(type(error).__name__)

        threads = [threading.Thread(target=lookup) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(results, ["ok"] * 8)
        self.assertEqual(len(self.fetch.urls), 1)


class VerifierWithCacheTests(unittest.TestCase):
    def test_unknown_kid_then_refetch_accepts_a_token_signed_by_the_new_key(self):
        fetch = FakeJwks(key())
        clock = Clock(1000.0)
        verifier, _ = make_verifier(fetch, clock)
        verifier.verify(make_token())
        rotated = make_token(key("rotated"))
        self.assertEqual(reason(self, verifier, rotated), "unknown-kid")  # published nowhere yet, interval not over
        fetch.serve(key(), key("rotated"))
        self.assertEqual(reason(self, verifier, rotated), "unknown-kid")  # still inside the minimum interval
        clock.advance(ej.KEYS_MIN_INTERVAL_SECONDS)
        verifier.verify(rotated)
        verifier.verify(make_token())

    def test_jwks_outage_makes_an_unverifiable_token_unavailable_not_valid(self):
        fetch = FakeJwks(key())
        fetch.fail()
        verifier, _ = make_verifier(fetch)
        with self.assertRaises(KeysUnavailable):
            verifier.verify(make_token())

    def test_jwks_outage_does_not_hide_tokens_that_are_wrong_regardless_of_keys(self):
        fetch = FakeJwks()
        fetch.fail()
        verifier, _ = make_verifier(fetch)
        header, payload, _ = make_token().split(".")
        for token in ("", "a.b", make_token(header={"alg": "none", "kid": KID}), make_token(header={"alg": "HS256", "kid": KID})):
            with self.subTest(token[:12]):
                with self.assertRaises(JwtError):
                    verifier.verify(token)

    def test_known_keys_keep_working_through_an_outage_and_wrong_tokens_stay_401(self):
        fetch = FakeJwks(key())
        clock = Clock(1000.0)
        verifier, _ = make_verifier(fetch, clock)
        verifier.verify(make_token())
        fetch.fail()
        clock.advance(ej.KEYS_TTL_SECONDS + 1)
        verifier.verify(make_token())
        self.assertEqual(reason(self, verifier, make_token(payload=claims(aud="nope"))), "aud")
        self.assertEqual(reason(self, verifier, make_token(key("attacker"), kid=KID)), "signature")

    def test_unusable_keys_in_the_set_do_not_mask_good_ones(self):
        fetch = FakeJwks()
        fetch.body = json.dumps({"keys": [key("small", 1024).jwk(), key().jwk()]}).encode()
        verifier, _ = make_verifier(fetch)
        verifier.verify(make_token())
        self.assertEqual(reason(self, verifier, make_token(key("small", 1024))), "unknown-kid")


class ConfigurationTests(unittest.TestCase):
    def test_tenant_must_be_a_guid_and_is_lower_cased(self):
        self.assertEqual(ej.validate_tenant(f" {TENANT.upper()} "), TENANT)
        for bad in ("", "contoso.onmicrosoft.com", "common", "<<TENANT_ID>>", TENANT + "0", "../" + TENANT, f"{TENANT}/x"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    ej.validate_tenant(bad)

    def test_audiences_are_a_comma_separated_list(self):
        self.assertEqual(ej.validate_audiences(f"{CLIENT_ID.upper()}, api://{CLIENT_ID.upper()} ,,{CLIENT_ID}"), (CLIENT_ID, AUDIENCE_URI))
        self.assertEqual(ej.validate_audiences("api://factory-manager"), ("api://factory-manager",))
        self.assertEqual(ej.validate_audiences("https://factory.example.invalid/api"), ("https://factory.example.invalid/api",))
        for bad in ("", " , ", "<<CLIENT_ID>>", "has space inside", "api://<<ID>>", "-leading", "x" * 300, "café"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    ej.validate_audiences(bad)

    def test_the_verifier_refuses_a_bad_configuration(self):
        with self.assertRaises(ValueError):
            EntraJwtVerifier("not-a-guid", (CLIENT_ID,), FakeJwks())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            EntraJwtVerifier(TENANT, (), FakeJwks())  # type: ignore[arg-type]

    def test_from_config_builds_a_cache_for_the_tenant(self):
        fetch = FakeJwks(key())
        verifier = EntraJwtVerifier.from_config(TENANT.upper(), (CLIENT_ID,), fetch=fetch)
        verifier._clock = Clock()  # type: ignore[assignment]
        verifier.verify(make_token())
        self.assertEqual(fetch.urls, [JWKS_URL])


class FetchTests(unittest.TestCase):
    def test_only_https_is_fetched(self):
        for url in ("http://login.microsoftonline.com/x", "file:///etc/passwd", "ftp://x/y", ""):
            with self.subTest(url):
                with self.assertRaises(ValueError):
                    ej.fetch_https(url)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(ej._NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.invalid/"))

    def test_the_response_size_is_capped(self):
        class Response:
            def __init__(self, size):
                self.size = size

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return b"x" * min(limit, self.size)

        for size, ok in ((100, True), (ej.MAX_JWKS_BYTES, True), (ej.MAX_JWKS_BYTES + 10, False)):
            opener = unittest.mock.Mock()
            opener.open.return_value = Response(size)
            with self.subTest(size), unittest.mock.patch("urllib.request.build_opener", return_value=opener):
                if ok:
                    self.assertEqual(len(ej.fetch_https("https://login.microsoftonline.com/x")), size)
                else:
                    with self.assertRaises(ValueError):
                        ej.fetch_https("https://login.microsoftonline.com/x")
                self.assertEqual(opener.open.call_args.kwargs["timeout"], ej.FETCH_TIMEOUT_SECONDS)


class NoLeakTests(unittest.TestCase):
    def test_error_text_never_contains_token_or_claim_values(self):
        fetch = FakeJwks(key())
        verifier, _ = make_verifier(fetch)
        token = make_token(payload=claims(aud="SECRET-AUDIENCE", preferred_username="SECRET-USER"))
        with self.assertRaises(JwtError) as raised:
            verifier.verify(token)
        text = f"{raised.exception!s} {raised.exception!r} {raised.exception.reason}"
        for secret in ("SECRET-AUDIENCE", "SECRET-USER", token.split(".")[2], token):
            self.assertNotIn(secret, text)

    def test_the_module_never_prints_or_logs(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            fetch = FakeJwks(key())
            verifier, _ = make_verifier(fetch)
            verifier.verify(make_token())
            with self.assertRaises(JwtError):
                verifier.verify(make_token(payload=claims(aud="x")))
            fetch.fail()
            failing = JwksCache(JWKS_URL, fetch, clock=Clock())
            with self.assertRaises(KeysUnavailable):
                failing.key(KID)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_the_module_is_standard_library_only(self):
        import sys

        with open(ej.__file__, encoding="utf-8") as handle:
            source = handle.read()
        imported = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
        self.assertTrue(imported <= set(sys.stdlib_module_names) | {"__future__"}, imported)


if __name__ == "__main__":
    unittest.main()
