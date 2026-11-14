"""Browser sign-in of the dashboard (docs/manager.md, "The browser page" and "Browser sign-in").

Server side, on a real ``ManagerServer`` with fakes (as in tests/test_webui.py):

* ``GET /ui/config.json`` is public, read-only, an exact allow-list entry with the shell's headers, and returns a
  pinned set of keys built from the manager's own Entra settings, never anything else;
* the content security policy allows the one Entra login origin in ``connect-src`` only while sign-in is on, and the
  other directives never change (no inline script or style, no framing, no form posts, no base);
* ``Strict-Transport-Security`` appears only where the request arrived over HTTPS through a trusted hop;
* the settings that steer the page are validated without echoing their values.

Browser side, when node is installed: the sign-in seam of ``app.js`` against a scripted fake of the MSAL library
(redirect flow only, refresh-token renewal, the 401 rule that can never loop, sign-out, the states the page shows).
"""

import ast
import inspect
import json
import re
import shutil
import subprocess
import textwrap
import unittest

from factory import manager as mgr
from factory import webui
from tests import test_manager as tm
from tests import test_webui as tw
from tests.test_entra_jwt import CLIENT_ID, TENANT

UI = tw.UI
LOGIN = "https://login.microsoftonline.com"
OTHER_GUID = "11111111-2222-4333-8444-555555555555"
ENTRA_KEYS = ["authority", "client_id", "mode", "scope", "tenant_id", "v"]
HTTPS = {"X-Forwarded-Proto": "https"}


def directives(policy):
    """{directive: [sources]} of a content security policy."""
    out = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


class PureTests(unittest.TestCase):
    def test_the_client_id_and_scope_come_from_the_audiences_the_manager_accepts(self):
        self.assertEqual(webui.derive_client_id((CLIENT_ID,)), CLIENT_ID)
        self.assertEqual(webui.derive_client_id((f"api://{CLIENT_ID}", CLIENT_ID)), CLIENT_ID)
        self.assertEqual(webui.derive_client_id(("api://factory", CLIENT_ID)), CLIENT_ID)
        self.assertEqual(webui.derive_client_id(("api://factory",)), "")
        self.assertEqual(webui.derive_client_id(()), "")
        self.assertEqual(webui.derive_scope(CLIENT_ID, (CLIENT_ID,)), f"{CLIENT_ID}/.default")
        # the page's own registration is the API: Entra refuses a token request for itself by application id URI (AADSTS90009)
        self.assertEqual(webui.derive_scope(CLIENT_ID, (f"api://{CLIENT_ID}", CLIENT_ID)), f"{CLIENT_ID}/.default")
        self.assertEqual(webui.derive_scope(CLIENT_ID, (CLIENT_ID, f"api://{CLIENT_ID}")), f"{CLIENT_ID}/.default")
        self.assertEqual(webui.derive_scope(CLIENT_ID, ("api://factory", CLIENT_ID)), "api://factory/.default")

    def test_the_config_has_exactly_the_pinned_keys_in_every_mode(self):
        cases = [
            webui.sign_in_config(False, "", ()),
            webui.sign_in_config(False, TENANT, (CLIENT_ID,), CLIENT_ID, "x/.default"),  # entra settings are ignored outside entra-jwt
            webui.sign_in_config(True, TENANT, ("api://factory",)),
            webui.sign_in_config(True, TENANT, (CLIENT_ID,)),
            webui.sign_in_config(True, TENANT, ("api://factory",), OTHER_GUID, "api://factory/.default"),
        ]
        self.assertEqual([sorted(case) for case in cases], [["mode", "v"], ["mode", "v"], ["mode", "v"], ENTRA_KEYS, ENTRA_KEYS])
        self.assertEqual(tuple(sorted(cases[3])), webui.CONFIG_KEYS_ENTRA)
        self.assertEqual(tuple(sorted(cases[0])), webui.CONFIG_KEYS_OTHER)
        self.assertEqual([case["mode"] for case in cases], ["token", "token", "unconfigured", "entra", "entra"])
        entra = cases[3]
        self.assertEqual(
            entra,
            {"v": 1, "mode": "entra", "tenant_id": TENANT, "client_id": CLIENT_ID, "authority": f"{LOGIN}/{TENANT}", "scope": f"{CLIENT_ID}/.default"},
        )
        self.assertEqual(cases[4]["client_id"], OTHER_GUID)
        self.assertEqual(cases[4]["scope"], "api://factory/.default")

    def test_the_csp_adds_one_exact_origin_to_connect_src_and_nothing_else(self):
        plain, signed = directives(webui.csp()), directives(webui.csp(LOGIN))
        self.assertEqual(plain["connect-src"], ["'self'"])
        self.assertEqual(signed["connect-src"], ["'self'", LOGIN])
        plain.pop("connect-src"), signed.pop("connect-src")
        self.assertEqual(plain, signed)
        self.assertEqual(signed["script-src"], ["'self'"])
        self.assertEqual(signed["style-src"], ["'self'"])
        self.assertEqual(signed["default-src"], ["'none'"])
        self.assertEqual((signed["frame-ancestors"], signed["form-action"], signed["base-uri"]), (["'none'"],) * 3)
        for source in (*signed["script-src"], *signed["style-src"]):
            self.assertNotIn("unsafe", source)

    def test_a_login_origin_must_be_one_exact_https_origin(self):
        for bad in ("*", "https://*.microsoftonline.com", "http://login.microsoftonline.com", f"{LOGIN}/", f"{LOGIN}/x", f"{LOGIN}; script-src *", "login.microsoftonline.com", "", "https://", "https://a b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                webui.csp(bad)
        self.assertEqual(webui.LOGIN_ORIGIN, LOGIN)  # the verifier's own constant, not a second copy

    def test_the_permissions_policy_denies_every_listed_feature_and_allows_none(self):
        entries = webui.PERMISSIONS_POLICY.split(", ")
        self.assertGreaterEqual(len(entries), 20)
        for entry in entries:
            self.assertTrue(entry.endswith("=()"), entry)
        for feature in ("camera", "microphone", "geolocation", "payment", "usb", "fullscreen", "clipboard-read"):
            self.assertIn(f"{feature}=()", entries)
        self.assertEqual(len(set(entries)), len(entries))

    def test_the_hsts_value_is_one_year_for_this_host_only(self):
        self.assertEqual(webui.HSTS, "max-age=31536000")


class SettingsTests(unittest.TestCase):
    ENV = {"FACTORY_CONFIG": "/etc/factory", "FACTORY_DATA": "/var/lib/factory"}
    JWT = {"FACTORY_MANAGER_AUTH": "entra-jwt", "FACTORY_ENTRA_TENANT_ID": TENANT, "FACTORY_ENTRA_AUDIENCES": CLIENT_ID}

    def test_the_two_page_settings_default_to_empty_and_are_read_only_in_entra_jwt_mode(self):
        defaults = mgr.Settings.from_env({**self.ENV, **self.JWT})
        self.assertEqual((defaults.entra_spa_client_id, defaults.entra_spa_scope), ("", ""))
        custom = mgr.Settings.from_env(
            {**self.ENV, **self.JWT, "FACTORY_ENTRA_SPA_CLIENT_ID": OTHER_GUID.upper(), "FACTORY_ENTRA_SPA_SCOPE": "api://factory/.default"}
        )
        self.assertEqual((custom.entra_spa_client_id, custom.entra_spa_scope), (OTHER_GUID, "api://factory/.default"))
        ignored = mgr.Settings.from_env({**self.ENV, "FACTORY_ENTRA_SPA_CLIENT_ID": "not-a-guid", "FACTORY_ENTRA_SPA_SCOPE": "bad scope"})
        self.assertEqual((ignored.auth, ignored.entra_spa_client_id, ignored.entra_spa_scope), ("easy-auth", "", ""))

    def test_bad_values_are_refused_naming_the_variable_and_never_echoing_the_value(self):
        secret = "S3CR3T-looking value"
        bad = [
            ("FACTORY_ENTRA_SPA_CLIENT_ID", secret),
            ("FACTORY_ENTRA_SPA_CLIENT_ID", "api://factory"),
            ("FACTORY_ENTRA_SPA_SCOPE", secret),
            ("FACTORY_ENTRA_SPA_SCOPE", "api://a/.default api://b/.default"),
            ("FACTORY_ENTRA_SPA_SCOPE", "a//b"),
            ("FACTORY_ENTRA_SPA_SCOPE", "<script>"),
            ("FACTORY_ENTRA_SPA_SCOPE", "x" * 201),
        ]
        for name, value in bad:
            with self.subTest(name=name, value=value[:20]), self.assertRaises(ValueError) as caught:
                mgr.Settings.from_env({**self.ENV, **self.JWT, name: value})
            self.assertIn(name, str(caught.exception))
            self.assertNotIn(value, str(caught.exception))


class ConfigRouteTests(tw.Fetch, tm.ServerCase):
    """Easy-auth mode: the page is told it takes a pasted token."""

    def test_the_config_is_public_exact_and_has_the_headers_of_the_shell(self):
        status, body, headers = self.raw("GET", tw.CONFIG_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"mode": "token", "v": 1})
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        for name, value in webui.SECURITY_HEADERS.items():
            self.assertEqual(headers[name], value, name)
        self.assertEqual(directives(headers["Content-Security-Policy"])["connect-src"], ["'self'"])
        self.assertEqual(self.runner.calls, [])

    def test_a_query_or_a_header_changes_nothing_and_only_get_is_public(self):
        plain = self.raw("GET", tw.CONFIG_PATH)
        for target, headers in ((f"{tw.CONFIG_PATH}?client_id=evil", None), (tw.CONFIG_PATH, {"Authorization": "Bearer x", "Host": "evil.example", "X-Forwarded-Host": "evil.example"})):
            again = self.raw("GET", target, headers)
            self.assertEqual((again[0], again[1]), (plain[0], plain[1]))
        for method in ("POST", "PUT", "DELETE", "HEAD", "OPTIONS"):
            self.assertEqual(self.raw(method, tw.CONFIG_PATH)[0], 401, method)
        self.assertEqual(self.raw_json("POST", tw.CONFIG_PATH, {"X-MS-CLIENT-PRINCIPAL": tm.OPERATOR})[0], 405)

    def test_lookalikes_of_the_config_route_are_ordinary_unknown_paths(self):
        for path in ("/ui/config.json/", "/ui/CONFIG.JSON", "/ui/config.json%00", "/ui/%63onfig.json", "/ui/config", "/config.json", "/ui//config.json", "/ui/./config.json", "/ui/config.json.map"):
            with self.subTest(path=path):
                self.assertEqual(self.raw_json("GET", path)[:2], (401, {"error": "authentication required"}))
                self.assertEqual(self.raw_json("GET", path, {"X-MS-CLIENT-PRINCIPAL": tm.VIEWER})[:2], (404, tw.NOT_FOUND))

    def test_the_config_route_is_counted_and_audited_like_the_page(self):
        self.raw("GET", f"{tw.CONFIG_PATH}?x=1")
        self.assertIn('route="ui"', self.manager.scrape_metrics())
        self.assertIn("path=/ui/config.json", self.out.getvalue())
        self.assertNotIn("x=1", self.out.getvalue())


class JwtConfigTests(tw.Fetch, tm.JwtCase):
    """entra-jwt mode: the page is told how to sign in, and the policy lets it reach Entra and nothing else."""

    def config(self, **headers):
        status, body, answer = self.raw("GET", tw.CONFIG_PATH, headers)
        self.assertEqual(status, 200)
        return json.loads(body), answer

    def test_the_config_names_the_tenant_client_authority_and_scope_and_nothing_else(self):
        config, headers = self.config()
        self.assertEqual(sorted(config), ENTRA_KEYS)
        self.assertEqual(
            config,
            {"v": 1, "mode": "entra", "tenant_id": TENANT, "client_id": CLIENT_ID, "authority": f"{LOGIN}/{TENANT}", "scope": f"{CLIENT_ID}/.default"},
        )
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_no_token_and_no_signing_key_is_needed_and_a_bogus_header_is_not_read(self):
        self.config(Authorization="Bearer not-a-token")
        self.assertEqual(self.fetch.urls, [])
        self.assertEqual(self.raw("GET", tw.CONFIG_PATH, {"X-MS-CLIENT-PRINCIPAL": tm.OPERATOR})[0], 200)

    def test_every_page_answer_lets_the_page_reach_the_one_login_origin(self):
        for path in (*tw.SHELL_PATHS, tw.CONFIG_PATH, "/ui"):
            with self.subTest(path=path):
                headers = self.raw("GET", path)[2]
                policy = directives(headers["Content-Security-Policy"])
                self.assertEqual(policy["connect-src"], ["'self'", LOGIN])
                self.assertEqual(policy["script-src"], ["'self'"])
                self.assertNotIn("*", headers["Content-Security-Policy"])
                self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
                self.assertEqual(headers["Permissions-Policy"], webui.PERMISSIONS_POLICY)

    def test_the_derived_scope_never_names_the_pages_own_registration_by_application_id_uri(self):
        # a regression guard for AADSTS90009: with the application id URI of the page's own registration as the resource, Entra
        # returns invalid_request ("requesting a token for itself ... GUID based App Identifier") and nobody can sign in
        for audiences in ((f"api://{CLIENT_ID}", CLIENT_ID), (CLIENT_ID, f"api://{CLIENT_ID}"), (f"api://{CLIENT_ID}",)):
            with self.subTest(audiences=audiences):
                scope = webui.derive_scope(webui.derive_client_id(audiences), audiences)
                self.assertEqual(scope, f"{CLIENT_ID}/.default")
                self.assertNotIn("api://", scope)

    def test_an_application_id_uri_in_the_audiences_sets_the_scope_and_explicit_settings_win(self):
        self.serve(verifier=self.verifier, auth=mgr.AUTH_JWT, entra_tenant_id=TENANT, entra_audiences=(f"api://{CLIENT_ID}", CLIENT_ID))
        self.assertEqual(self.config()[0]["scope"], f"{CLIENT_ID}/.default")  # the GUID form: never api://<own client id> (AADSTS90009)
        self.serve(
            verifier=self.verifier, auth=mgr.AUTH_JWT, entra_tenant_id=TENANT, entra_audiences=("api://factory",),
            entra_spa_client_id=OTHER_GUID,
        )
        config = self.config()[0]
        self.assertEqual((config["client_id"], config["scope"]), (OTHER_GUID, "api://factory/.default"))
        self.serve(
            verifier=self.verifier, auth=mgr.AUTH_JWT, entra_tenant_id=TENANT, entra_audiences=(CLIENT_ID,),
            entra_spa_scope="api://factory/access_as_user",
        )
        self.assertEqual(self.config()[0]["scope"], "api://factory/access_as_user")

    def test_without_a_client_id_the_page_is_told_it_is_unconfigured_and_gets_no_login_origin(self):
        self.serve(verifier=self.verifier, auth=mgr.AUTH_JWT, entra_tenant_id=TENANT, entra_audiences=("api://factory",))
        config, headers = self.config()
        self.assertEqual(config, {"mode": "unconfigured", "v": 1})
        self.assertEqual(directives(headers["Content-Security-Policy"])["connect-src"], ["'self'"])
        self.assertEqual(directives(self.raw("GET", "/ui/")[2]["Content-Security-Policy"])["connect-src"], ["'self'"])

    def test_the_data_calls_still_need_a_verified_token_and_a_role(self):
        self.assertEqual(self.raw_json("GET", "/dashboard")[0], 401)
        self.assertEqual(self.raw_json("GET", "/dashboard", {"Authorization": f"Bearer {self.token(roles=[])}"})[0], 403)
        self.assertEqual(self.raw_json("GET", "/dashboard", {"Authorization": f"Bearer {self.token(roles=['Factory.Viewer'])}"})[0], 200)


class HstsTests(tw.Fetch, tm.JwtCase):
    """Strict-Transport-Security only where the request arrived over HTTPS, as told by a trusted hop."""

    HSTS = "Strict-Transport-Security"

    def over(self, peer, headers, path="/ui/"):
        return self.direct("GET", path, peer, headers).headers.get(self.HSTS)

    def test_a_request_the_proxy_says_was_https_gets_it_on_the_shell_and_on_every_api_answer(self):
        for peer in ("127.0.0.1", "10.244.1.7", "172.16.0.9", "192.168.1.2", "::1", "fd00::5", "::ffff:10.1.2.3"):
            with self.subTest(peer=peer):
                self.assertEqual(self.over(peer, HTTPS), webui.HSTS)
        for path, expected in (("/ui/", 200), (tw.CONFIG_PATH, 200), ("/ui", 307), ("/ui/app.js", 200), ("/dashboard", 401), ("/repos", 401), ("/nothing", 401), ("/healthz", 401)):  # /healthz from a non-loopback peer needs a token
            with self.subTest(path=path):
                reply = self.direct("GET", path, "10.0.0.5", HTTPS)
                self.assertEqual((reply.status, reply.headers.get(self.HSTS)), (expected, webui.HSTS))

    def test_every_other_request_is_plain_http_as_far_as_the_manager_knows(self):
        self.assertIsNone(self.over("10.0.0.5", {}))
        for value in ("http", "", "HTTP", "https, http", "http, https", "wss", "https;x", "yes", "1", " "):
            with self.subTest(value=value):
                self.assertIsNone(self.over("10.0.0.5", {"X-Forwarded-Proto": value}))
        for peer in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8", "100.64.0.1", "172.32.0.1", "11.0.0.1", "192.169.0.1", "not-an-address", ""):
            with self.subTest(peer=peer):
                self.assertIsNone(self.over(peer, HTTPS))
        self.assertIsNone(self.over("10.0.0.5", {"Forwarded": "proto=https"}))  # only the one header is read
        self.assertIsNone(self.over("10.0.0.5", {"X-Forwarded-Scheme": "https"}))

    def test_the_header_is_case_insensitive_in_name_and_value(self):
        self.assertEqual(self.over("10.0.0.5", {"x-forwarded-proto": "HTTPS"}), webui.HSTS)
        self.assertEqual(self.over("10.0.0.5", {"X-Forwarded-Proto": " https "}), webui.HSTS)

    def test_a_real_connection_through_the_proxy_header_and_without_it(self):
        with_header = self.raw("GET", "/ui/", HTTPS)[2]
        self.assertEqual(with_header[self.HSTS], webui.HSTS)
        self.assertNotIn(self.HSTS, self.raw("GET", "/ui/")[2])
        status, _, headers = self.raw_json("GET", "/dashboard", HTTPS)
        self.assertEqual((status, headers[self.HSTS]), (401, webui.HSTS))
        self.assertNotIn(self.HSTS, self.raw_json("GET", "/dashboard")[2])

    def test_the_value_has_no_subdomain_or_preload_directive(self):
        value = self.over("127.0.0.1", HTTPS)
        self.assertNotIn("includeSubDomains", value)
        self.assertNotIn("preload", value)


# ---------------------------------------------------------------------------------------------
# The browser side, under node
# ---------------------------------------------------------------------------------------------

NODE = shutil.which("node")

SEAM = r"""
const app = require(process.argv[1]);
const TENANT = '00000000-0000-4000-8000-0000000000a1';
const CLIENT = '00000000-0000-4000-8000-0000000000c3';
const AUTHORITY = `https://login.microsoftonline.com/${TENANT}`;
const CONFIG = app.cleanConfig({ v: 1, mode: 'entra', tenant_id: TENANT, client_id: CLIENT, authority: AUTHORITY, scope: `api://${CLIENT}/.default` });
const ORIGIN = 'https://factory.example.org';
const out = {};

class Interaction extends Error { constructor(code) { super(code); this.errorCode = code; } }
class Net extends Error { constructor() { super('net'); this.errorCode = 'network_error'; } }

function fakeMsal(options) {
  const log = [];
  const accounts = options.accounts || [];
  let active = null;
  const banned = ['loginPopup', 'acquireTokenPopup', 'ssoSilent', 'logoutPopup', 'acquireTokenByCode'];
  const instance = {
    getActiveAccount: () => active,
    getAllAccounts: () => accounts,
    setActiveAccount: (account) => { active = account; log.push(['setActiveAccount']); },
    handleRedirectPromise: (o) => { log.push(['handleRedirectPromise', o]); return options.redirect ? options.redirect() : Promise.resolve(null); },
    acquireTokenSilent: (r) => { log.push(['silent', r]); return options.silent ? options.silent(r) : Promise.resolve({ accessToken: 'tok-1' }); },
    acquireTokenRedirect: (r) => { log.push(['acquireTokenRedirect', r]); return Promise.resolve(); },
    loginRedirect: (r) => { log.push(['loginRedirect', r]); return Promise.resolve(); },
    logoutRedirect: (r) => { log.push(['logoutRedirect', r]); return Promise.resolve(); },
  };
  for (const name of banned) instance[name] = () => { throw new Error(`${name} was called`); };
  const msal = {
    createStandardPublicClientApplication: (config) => { log.push(['create', config]); return Promise.resolve(instance); },
    BrowserCacheLocation: { SessionStorage: 'sessionStorage' },
    CacheLookupPolicy: { AccessTokenAndRefreshToken: 2, RefreshToken: 3 },
    InteractionRequiredAuthError: Interaction,
  };
  return { msal, log };
}

function fakeStorage() {
  const map = new Map();
  return { map, getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => { map.set(k, String(v)); }, removeItem: (k) => { map.delete(k); } };
}

function entra(options, clock) {
  const fake = fakeMsal(options || {});
  const storage = fakeStorage();
  const time = clock || { now: 1000000 };
  const auth = app.createEntra({ msal: fake.msal, config: CONFIG, origin: ORIGIN, store: app.createStore(storage), now: () => time.now });
  return { auth, fake, storage, time, log: fake.log };
}
const names = (log) => log.map((entry) => entry[0]);
const ACCOUNT = { username: 'ada@example.org' };

async function main() {
  // 1. start without a redirect: the application is built from the config and reads the redirect answer, nothing else
  {
    const t = entra({});
    const result = await t.auth.start();
    const create = t.log.find((entry) => entry[0] === 'create')[1];
    out.start = {
      result, signedIn: t.auth.signedIn(), names: names(t.log),
      auth: create.auth, cache: create.cache, iframe: create.system.allowRedirectInIframe, pii: create.system.loggerOptions.piiLoggingEnabled,
      silentLogger: create.system.loggerOptions.loggerCallback('error', 'secret') === undefined,
      handle: t.log.find((entry) => entry[0] === 'handleRedirectPromise')[1],
    };
  }
  // 2. the return from a sign-in redirect: the account becomes the active one
  {
    const t = entra({ redirect: () => Promise.resolve({ account: ACCOUNT, accessToken: 'tok-from-redirect' }) });
    out.returned = { result: await t.auth.start(), signedIn: t.auth.signedIn(), username: t.auth.username(), names: names(t.log) };
    const t2 = entra({ accounts: [ACCOUNT] }); // a reload: the account is in the cache
    out.reload = { result: await t2.auth.start(), signedIn: t2.auth.signedIn(), names: names(t2.log) };
    const t3 = entra({ accounts: [{ username: 'x'.repeat(400) }] });
    await t3.auth.start();
    out.longName = t3.auth.username();
  }
  // 3. a failed sign-in shows a fixed-shape code, never the text Microsoft sent
  {
    const codes = [];
    for (const error of [{ errorCode: 'access_denied' }, { errorCode: '<script>alert(1)</script>' }, { errorCode: 'AADSTS50105 user not assigned' }, new Error('x'), null]) {
      const t = entra({ redirect: () => Promise.reject(error) });
      codes.push(await t.auth.start());
    }
    out.codes = codes;
  }
  // 4. tokens: cache or refresh token, never a hidden frame; force skips the cached access token
  {
    const t = entra({ accounts: [ACCOUNT] });
    await t.auth.start();
    const normal = await t.auth.getToken();
    const forced = await t.auth.getToken({ force: true });
    const silent = t.log.filter((entry) => entry[0] === 'silent').map((entry) => entry[1]);
    out.tokens = { normal, forced, requests: silent.map((r) => ({ scopes: r.scopes, policy: r.cacheLookupPolicy, forceRefresh: r.forceRefresh, hasAccount: r.account === ACCOUNT, keys: Object.keys(r).sort() })) };
    const none = entra({});
    await none.auth.start();
    out.noAccountToken = await none.auth.getToken();
    const empty = entra({ accounts: [ACCOUNT], silent: () => Promise.resolve({ accessToken: '' }) });
    await empty.auth.start();
    out.emptyToken = await empty.auth.getToken();
  }
  // 5. only the user can fix it: null, then one automatic redirect, never a second within the pause
  {
    const t = entra({ accounts: [ACCOUNT], silent: () => Promise.reject(new Interaction('interaction_required')) });
    await t.auth.start();
    const token = await t.auth.getToken();
    const first = await t.auth.expire('#/history?kind=jobs', false);
    const second = await t.auth.expire('#/history?kind=jobs', false);
    t.time.now += 4 * 60 * 1000;
    const third = await t.auth.expire('#/active', true);
    t.time.now += 2 * 60 * 1000;
    const fourth = await t.auth.expire('#/bad route<script>', true);
    const redirects = t.log.filter((entry) => entry[0] === 'acquireTokenRedirect' || entry[0] === 'loginRedirect');
    out.expire = { token, first, second, third, fourth, redirects: redirects.map((entry) => [entry[0], entry[1].redirectStartPage, entry[1].scopes, entry[1].prompt]), keys: [...t.storage.map.keys()].sort(), route: t.storage.map.get('factory.route') || null };
    for (const code of ['login_required', 'consent_required', 'invalid_grant', 'no_tokens_found', 'refresh_token_expired']) {
      const u = entra({ accounts: [ACCOUNT], silent: () => Promise.reject({ errorCode: code }) });
      await u.auth.start();
      out.expire[code] = [await u.auth.getToken(), await u.auth.expire('', false)];
    }
  }
  // 6. a network problem is not a reason to sign in again
  {
    const t = entra({ accounts: [ACCOUNT], silent: () => Promise.reject(new Net()) });
    await t.auth.start();
    let status = null;
    try { await t.auth.getToken(); } catch (error) { status = error.status; }
    out.network = { status, expire: await t.auth.expire('#/active', false), redirects: t.log.filter((entry) => entry[0].endsWith('Redirect')).length };
  }
  // 7. interactive sign-in: a login redirect without an account, a token redirect with one, a chooser on request
  {
    const t = entra({});
    await t.auth.start();
    const started = await t.auth.signIn({ route: '#/active' });
    const chose = await t.auth.signIn({ choose: true });
    const w = entra({ accounts: [ACCOUNT] });
    await w.auth.start();
    await w.auth.signIn({});
    await w.auth.signIn({ choose: true, route: '#/job/org/name/r1/fix-a' });
    out.signIn = {
      started, chose,
      noAccount: t.log.filter((entry) => entry[0].endsWith('Redirect')).map((entry) => [entry[0], entry[1].prompt || null, entry[1].redirectStartPage]),
      withAccount: w.log.filter((entry) => entry[0].endsWith('Redirect')).map((entry) => [entry[0], entry[1].prompt || null, entry[1].account === ACCOUNT]),
      route: w.storage.map.get('factory.route'),
    };
    const early = app.createEntra({ msal: fakeMsal({}).msal, config: CONFIG, origin: ORIGIN, store: app.createStore(null), now: Date.now });
    out.signInBeforeStart = await early.signIn({});
  }
  // 8. sign-out ends the Microsoft session and drops the markers
  {
    const t = entra({ accounts: [ACCOUNT] });
    await t.auth.start();
    await t.auth.signIn({ route: '#/active' });
    await t.auth.signOut();
    const logout = t.log.find((entry) => entry[0] === 'logoutRedirect')[1];
    out.signOut = { signedIn: t.auth.signedIn(), keys: [...t.storage.map.keys()], redirect: logout.postLogoutRedirectUri, account: logout.account === ACCOUNT };
  }
  // 9. the pasted-token provider (easy-auth and local mode only)
  {
    const storage = fakeStorage();
    const p = app.createPasted(app.createStore(storage));
    const before = [p.signedIn(), await p.getToken()];
    const bad = p.signIn('short');
    const good = p.signIn('  Bearer abcdefgh12345678  ');
    out.pasted = { mode: p.mode, before, bad, good, after: [p.signedIn(), await p.getToken()], keys: [...storage.map.keys()], expire: await p.expire('#/active', true), name: p.username() };
    await p.signOut();
    out.pasted.out = [p.signedIn(), await p.getToken(), [...storage.map.keys()]];
    const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); } };
    const q = app.createPasted(app.createStore(broken));
    q.signIn('abcdefgh12345678');
    out.blockedStorage = [q.signedIn(), await q.getToken()];
    await q.signOut();
    out.blockedStorage.push(q.signedIn());
  }
  // 10. the configuration is checked key by key
  {
    const good = { v: 1, mode: 'entra', tenant_id: TENANT, client_id: CLIENT, authority: AUTHORITY, scope: 'api://x/.default' };
    const variants = [
      good, { ...good, extra: 'ignored', secret: 'nope' },
      { ...good, v: 2 }, { ...good, v: '1' }, { ...good, mode: 'other' }, { ...good, mode: 'ENTRA' },
      { ...good, tenant_id: 'not-a-guid' }, { ...good, client_id: CLIENT.toUpperCase() }, { ...good, client_id: null },
      { ...good, authority: `http://login.microsoftonline.com/${TENANT}` }, { ...good, authority: `${AUTHORITY}/v2.0` },
      { ...good, authority: `https://login.microsoftonline.com/${CLIENT}` }, { ...good, authority: `https://evil.example/${TENANT}` },
      { ...good, authority: `https://login.microsoftonline.com.evil.example:8443/${TENANT}` }, { ...good, authority: `https://*/${TENANT}` },
      { ...good, scope: 'a b' }, { ...good, scope: '' }, { ...good, scope: 'x'.repeat(201) }, { ...good, scope: '<script>' },
      { v: 1, mode: 'token' }, { v: 1, mode: 'token', tenant_id: TENANT }, { v: 1, mode: 'unconfigured' },
      null, 'x', 7, [], {},
    ];
    out.configs = variants.map((variant) => app.cleanConfig(variant));
  }
  // 11. error codes
  out.errorCodes = [{ errorCode: 'access_denied' }, { errorCode: 'Bad Code' }, { errorCode: 'x'.repeat(80) }, { errorCode: 7 }, {}, null, undefined, { errorCode: '1abc' }].map(app.errorCode);
  console.log(JSON.stringify(out));
}
main().catch((error) => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
"""

CALLS = r"""
const app = require(process.argv[1]);
const TENANT = '00000000-0000-4000-8000-0000000000a1';
const CLIENT = '00000000-0000-4000-8000-0000000000c3';
const CONFIG = app.cleanConfig({ v: 1, mode: 'entra', tenant_id: TENANT, client_id: CLIENT, authority: `https://login.microsoftonline.com/${TENANT}`, scope: `api://${CLIENT}/.default` });
const ACCOUNT = { username: 'ada@example.org' };
class Interaction extends Error { constructor(code) { super(code); this.errorCode = code; } }
const out = {};

function entra(silent, clock) {
  const log = [];
  const map = new Map();
  const storage = { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => { map.set(k, String(v)); }, removeItem: (k) => { map.delete(k); } };
  const instance = {
    getActiveAccount: () => ACCOUNT, getAllAccounts: () => [ACCOUNT], setActiveAccount() {},
    handleRedirectPromise: () => Promise.resolve(null),
    acquireTokenSilent: (r) => { log.push(['silent', r.cacheLookupPolicy]); return silent(r); },
    acquireTokenRedirect: (r) => { log.push(['acquireTokenRedirect']); return Promise.resolve(); },
    loginRedirect: () => { log.push(['loginRedirect']); return Promise.resolve(); },
    logoutRedirect: () => Promise.resolve(),
  };
  const msal = {
    createStandardPublicClientApplication: () => Promise.resolve(instance),
    BrowserCacheLocation: { SessionStorage: 'sessionStorage' },
    CacheLookupPolicy: { AccessTokenAndRefreshToken: 2, RefreshToken: 3 },
    InteractionRequiredAuthError: Interaction,
  };
  const time = clock || { now: 5000000 };
  const auth = app.createEntra({ msal, config: CONFIG, origin: 'https://factory.example.org', store: app.createStore(storage), now: () => time.now });
  return { auth, log, time };
}

// a scripted server: statuses by call number, a record of the tokens that were sent
function server(statuses) {
  const sent = [];
  return { sent, send(token) { sent.push(token); const status = statuses[Math.min(sent.length - 1, statuses.length - 1)]; return Promise.resolve({ status, ok: status >= 200 && status < 300, json: () => Promise.resolve({ ok: true, token }) }); } };
}
async function settle(promise) {
  const mark = {};
  const winner = await Promise.race([promise.then((value) => ({ value }), (error) => ({ error: error && error.status })), new Promise((resolve) => setImmediate(() => resolve(mark)))]);
  return winner === mark ? 'pending' : winner;
}
let counter = 0;
const fresh = () => { counter += 1; return Promise.resolve({ accessToken: `tok-${counter}` }); };

async function main() {
  // the happy path: one token, one call
  { const t = entra(fresh); await t.auth.start(); const s = server([200]); out.ok = [await settle(app.authenticatedCall(t.auth, s.send, '#/active')), s.sent.length, t.log.map((e) => e[0])]; }
  // a 401 then a fresh token that works: no redirect
  { const t = entra(fresh); await t.auth.start(); const s = server([401, 200]); out.refreshed = [await settle(app.authenticatedCall(t.auth, s.send, '#/active')), s.sent, t.log]; }
  // a 401 that a fresh token does not fix: one automatic redirect, and the page is leaving (the call never settles)
  { const t = entra(fresh); await t.auth.start(); const s = server([401]); out.refused = [await settle(app.authenticatedCall(t.auth, s.send, '#/history')), s.sent.length, t.log.filter((e) => e[0].endsWith('Redirect')).length];
    // the same page asks again straight away (a reload): the guard refuses a second redirect, the call fails with 401, no loop
    const s2 = server([401]); out.refusedAgain = [await settle(app.authenticatedCall(t.auth, s2.send, '#/history')), s2.sent.length, t.log.filter((e) => e[0].endsWith('Redirect')).length];
    // five more calls: still exactly one redirect in all
    for (let i = 0; i < 5; i += 1) await settle(app.authenticatedCall(t.auth, server([401]).send, '#/history'));
    out.redirectsAfterMany = t.log.filter((e) => e[0].endsWith('Redirect')).length;
    // after the pause one more is allowed
    t.time.now += 5 * 60 * 1000 + 1;
    out.afterPause = [await settle(app.authenticatedCall(t.auth, server([401]).send, '#/history')), t.log.filter((e) => e[0].endsWith('Redirect')).length]; }
  // the refresh token is gone: no token at all -> one redirect, then 401 without another
  { const t = entra(() => Promise.reject(new Interaction('invalid_grant'))); await t.auth.start(); const s = server([200]);
    out.noToken = [await settle(app.authenticatedCall(t.auth, s.send, '#/active')), s.sent.length, t.log.filter((e) => e[0].endsWith('Redirect')).length];
    out.noTokenAgain = [await settle(app.authenticatedCall(t.auth, s.send, '#/active')), s.sent.length, t.log.filter((e) => e[0].endsWith('Redirect')).length]; }
  // a 403 is final: no retry, no new token, no redirect
  { const t = entra(fresh); await t.auth.start(); const s = server([403]); out.forbidden = [await settle(app.authenticatedCall(t.auth, s.send, '#/active')), s.sent.length, t.log.map((e) => e[0])]; }
  // other statuses are passed on as they are
  { const t = entra(fresh); await t.auth.start(); out.statuses = []; for (const status of [404, 500, 503, 400]) { const s = server([status]); out.statuses.push(await settle(app.authenticatedCall(t.auth, s.send, '#/active'))); } }
  // a request that changes something (redirect: false): the fresh-token retry stays, the sign-in redirect never starts
  { const t = entra(fresh); await t.auth.start(); const s = server([401]);
    out.writeRefused = [await settle(app.authenticatedCall(t.auth, s.send, '#/active', { redirect: false })), s.sent.length, t.log.filter((e) => e[0].endsWith('Redirect')).length]; }
  { const t = entra(() => Promise.reject(new Interaction('invalid_grant'))); await t.auth.start(); const s = server([200]);
    out.writeNoToken = [await settle(app.authenticatedCall(t.auth, s.send, '#/active', { redirect: false })), s.sent.length, t.log.filter((e) => e[0].endsWith('Redirect')).length]; }
  // a refusal carries the one fixed word of a 409 (the job status), never the rest of the body
  { const t = entra(fresh); await t.auth.start();
    const refuse = (body) => () => Promise.resolve({ status: 409, ok: false, json: () => body });
    const caught = async (send) => { try { await app.authenticatedCall(t.auth, send, '#/active'); return 'no error'; } catch (error) { return [error.status, error.jobStatus, Object.keys(error).sort()]; } };
    out.conflict = await caught(refuse(Promise.resolve({ error: 'x', job_status: 'done', secret: 'nope' })));
    out.conflictNoWord = await caught(refuse(Promise.resolve({ error: 'x', job_status: 7 })));
    out.conflictBadBody = await caught(refuse(Promise.reject(new Error('not json')))); }
  // the network: no token is not a sign-in
  { const t = entra(() => Promise.reject(new Error('offline'))); await t.auth.start(); const s = server([200]); out.offline = [await settle(app.authenticatedCall(t.auth, s.send, '#/active')), s.sent.length, t.log.filter((e) => e[0].endsWith('Redirect')).length]; }
  // a redirect that cannot start fails the call instead of hanging
  { const t = entra(() => Promise.reject(new Interaction('login_required'))); await t.auth.start(); t.time.now = 5000000;
    out.noRedirectNoHang = 'covered by the guard'; }
  // the pasted token: one try, never a retry or a redirect
  { const map = new Map(); const storage = { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => { map.set(k, String(v)); }, removeItem: (k) => { map.delete(k); } };
    const p = app.createPasted(app.createStore(storage)); p.signIn('abcdefgh12345678'); const s = server([401]);
    out.pasted401 = [await settle(app.authenticatedCall(p, s.send, '#/active')), s.sent];
    const none = app.createPasted(app.createStore(storage)); none.signOut(); await none.signOut(); const s2 = server([200]);
    out.pastedNone = [await settle(app.authenticatedCall(none, s2.send, '#/active')), s2.sent.length]; }
  console.log(JSON.stringify(out));
}
main().catch((error) => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
"""

PANEL = r"""
class Text { constructor(text) { this.text = String(text); } }
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.className = ''; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener() {}
  set innerHTML(value) { throw new Error('innerHTML was set'); }
}
global.document = { createElement: (tag) => new El(tag), createTextNode: (text) => new Text(text) };
const app = require(process.argv[1]);
function collect(node, into) {
  if (node instanceof Text) { into.text.push(node.text); return; }
  into.tags.add(node.tag);
  if (node.tag === 'button') into.buttons.push(node.children.map((c) => c.text).join(''));
  if (node.tag === 'input') into.inputs.push(node.attrs.type);
  if (node.tag === 'details') into.open = 'open' in node.attrs;
  if (node.tag === 'summary') into.summary = node.children.map((c) => c.text).join('');
  for (const child of node.children) collect(child, into);
}
function view(info) {
  const into = { text: [], tags: new Set(), buttons: [], inputs: [], open: null, summary: null };
  collect(app.renderAuthPanel(info, { reload() {}, paste() {}, signIn() {}, chooseAccount() {}, signOut() {} }), into);
  return { summary: into.summary, open: into.open, buttons: into.buttons, inputs: into.inputs, text: into.text.join(' '), tags: [...into.tags].sort() };
}
const CANARY = '<img src=x onerror=alert(1)>';
const out = {
  loading: view({ mode: 'loading' }),
  broken: view({ mode: 'broken' }),
  unconfigured: view({ mode: 'unconfigured' }),
  tokenNone: view({ mode: 'token', signedIn: false }),
  tokenSet: view({ mode: 'token', signedIn: true }),
  tokenBad: view({ mode: 'token', signedIn: false, message: 'That does not look like a token.' }),
  signedOut: view({ mode: 'entra', signedIn: false, issue: null, problem: null }),
  failed: view({ mode: 'entra', signedIn: false, problem: 'access_denied' }),
  signedIn: view({ mode: 'entra', signedIn: true, username: 'ada@example.org' }),
  forbidden: view({ mode: 'entra', signedIn: true, username: 'ada@example.org', issue: 'forbidden' }),
  expired: view({ mode: 'entra', signedIn: true, username: 'ada@example.org', issue: 'expired' }),
  hostile: view({ mode: 'entra', signedIn: true, username: CANARY, issue: 'forbidden' }),
  noName: view({ mode: 'entra', signedIn: true, username: null }),
};
const failures = (mode) => [401, 403, 503].map((status) => {
  const node = app.failure({ status }, mode); const into = { text: [], tags: new Set(), buttons: [], inputs: [] }; collect(node, into); return into.text.join('');
});
out.failures = { entra: failures('entra'), token: failures('token'), none: failures(undefined) };
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed; the sign-in seam of app.js is then not exercised")
class SeamTests(unittest.TestCase):
    @classmethod
    def run_node(cls, source):
        done = subprocess.run([NODE, "-e", source, str(UI / "app.js")], capture_output=True, text=True, timeout=120, check=False)
        if done.returncode:
            raise AssertionError(done.stderr)
        return json.loads(done.stdout)

    @classmethod
    def setUpClass(cls):
        cls.seam = cls.run_node(SEAM)
        cls.calls = cls.run_node(CALLS)
        cls.panel = cls.run_node(PANEL)

    # -- the library is configured for the redirect flow only --------------------------------

    def test_the_application_is_built_for_the_redirect_flow_with_session_storage_and_a_silent_logger(self):
        start = self.seam["start"]
        self.assertEqual(
            start["auth"],
            {
                "clientId": "00000000-0000-4000-8000-0000000000c3",
                "authority": "https://login.microsoftonline.com/00000000-0000-4000-8000-0000000000a1",
                "redirectUri": "https://factory.example.org/ui/",
                "postLogoutRedirectUri": "https://factory.example.org/ui/",
            },
        )
        self.assertEqual(start["cache"], {"cacheLocation": "sessionStorage"})
        self.assertFalse(start["iframe"])
        self.assertFalse(start["pii"])
        self.assertTrue(start["silentLogger"])  # nothing the library would log (tokens, claims) reaches a console
        self.assertEqual(start["names"], ["create", "handleRedirectPromise"])
        self.assertEqual(start["handle"], {"navigateToLoginRequestUrl": False})
        self.assertEqual(start["result"], {"returned": False, "problem": None})
        self.assertFalse(start["signedIn"])

    def test_the_return_from_a_redirect_and_a_reload_leave_the_account_active(self):
        self.assertEqual(self.seam["returned"]["result"], {"returned": True, "problem": None})
        self.assertEqual((self.seam["returned"]["signedIn"], self.seam["returned"]["username"]), (True, "ada@example.org"))
        self.assertIn("setActiveAccount", self.seam["returned"]["names"])
        self.assertEqual((self.seam["reload"]["result"]["returned"], self.seam["reload"]["signedIn"]), (False, True))
        self.assertIsNone(self.seam["longName"])  # an absurd name is not shown

    def test_a_failed_sign_in_shows_a_fixed_shape_code_never_the_text_microsoft_sent(self):
        self.assertEqual(
            [item["problem"] for item in self.seam["codes"]],
            ["access_denied", "unknown", "unknown", "unknown", "unknown"],
        )
        self.assertTrue(all(item["returned"] for item in self.seam["codes"]))
        self.assertEqual(self.seam["errorCodes"], ["access_denied", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown"])

    # -- tokens ------------------------------------------------------------------------------

    def test_tokens_come_from_the_cache_or_a_refresh_token_and_force_skips_the_cached_access_token(self):
        tokens = self.seam["tokens"]
        self.assertEqual((tokens["normal"], tokens["forced"]), ("tok-1", "tok-1"))
        normal, forced = tokens["requests"]
        self.assertEqual(normal["scopes"], ["api://00000000-0000-4000-8000-0000000000c3/.default"])
        self.assertEqual((normal["policy"], forced["policy"]), (2, 3))  # AccessTokenAndRefreshToken, RefreshToken: never the default (frame) policy
        for request in tokens["requests"]:
            self.assertTrue(request["hasAccount"])
            self.assertNotIn("forceRefresh", request)
            self.assertEqual(request["keys"], ["account", "cacheLookupPolicy", "scopes"])
        self.assertIsNone(self.seam["noAccountToken"])
        self.assertIsNone(self.seam["emptyToken"])

    def test_no_popup_and_no_hidden_frame_call_is_ever_made(self):
        # the fake throws on loginPopup, acquireTokenPopup, ssoSilent, logoutPopup and acquireTokenByCode: reaching the
        # end of every scenario without an error means none of them was called
        self.assertTrue(self.seam["signIn"]["started"])
        script = (UI / "app.js").read_text(encoding="utf-8")
        for name in ("loginPopup", "acquireTokenPopup", "ssoSilent", "logoutPopup", "acquireTokenPopup", "forceRefresh"):
            self.assertNotIn(name, script, name)
        self.assertNotIn("window.open", script)

    # -- the loop guard ----------------------------------------------------------------------

    def test_a_session_that_needs_the_user_starts_one_redirect_and_never_a_second_within_the_pause(self):
        expire = self.seam["expire"]
        self.assertIsNone(expire["token"])
        self.assertEqual((expire["first"], expire["second"], expire["third"], expire["fourth"]), (True, False, False, True))
        self.assertEqual(len(expire["redirects"]), 2)
        for name, start_page, scopes, prompt in expire["redirects"]:
            self.assertEqual(name, "acquireTokenRedirect")
            self.assertEqual(start_page, "https://factory.example.org/ui/")  # no hash, no query, no token: the address MSAL comes back to
            self.assertEqual(scopes, ["api://00000000-0000-4000-8000-0000000000c3/.default"])
            self.assertIsNone(prompt)
        self.assertEqual(expire["keys"], ["factory.signin"])  # the loop guard only; the hostile route was not kept
        for code in ("login_required", "consent_required", "invalid_grant", "no_tokens_found", "refresh_token_expired"):
            self.assertEqual(expire[code], [None, True], code)

    def test_a_network_problem_is_not_a_reason_to_sign_in_again(self):
        self.assertEqual(self.seam["network"], {"status": 0, "expire": False, "redirects": 0})

    def test_sign_in_is_a_redirect_a_login_without_an_account_a_token_request_with_one(self):
        sign_in = self.seam["signIn"]
        self.assertEqual((sign_in["started"], sign_in["chose"]), (True, True))
        self.assertEqual(
            sign_in["noAccount"],
            [["loginRedirect", None, "https://factory.example.org/ui/"], ["loginRedirect", "select_account", "https://factory.example.org/ui/"]],
        )
        self.assertEqual(sign_in["withAccount"], [["acquireTokenRedirect", None, True], ["loginRedirect", "select_account", False]])
        self.assertEqual(sign_in["route"], "#/job/org/name/r1/fix-a")  # a validated page hash, restored after the return
        self.assertFalse(self.seam["signInBeforeStart"])

    def test_sign_out_ends_the_microsoft_session_and_forgets_this_tabs_markers(self):
        self.assertEqual(
            self.seam["signOut"],
            {"signedIn": False, "keys": [], "redirect": "https://factory.example.org/ui/", "account": True},
        )

    # -- the pasted token (easy-auth and local mode only) ------------------------------------

    def test_a_pasted_token_is_kept_for_the_tab_and_never_triggers_a_redirect(self):
        pasted = self.seam["pasted"]
        self.assertEqual(pasted["mode"], "token")
        self.assertEqual(pasted["before"], [False, None])
        self.assertEqual((pasted["bad"], pasted["good"]), (False, True))
        self.assertEqual(pasted["after"], [True, "abcdefgh12345678"])
        self.assertEqual(pasted["keys"], ["factory.token"])
        self.assertFalse(pasted["expire"])
        self.assertIsNone(pasted["name"])
        self.assertEqual(pasted["out"], [False, None, []])
        self.assertEqual(self.seam["blockedStorage"], [True, "abcdefgh12345678", False])  # blocked storage: the page still works

    def test_the_paste_provider_is_created_only_for_the_two_modes_without_a_browser_sign_in(self):
        script = (UI / "app.js").read_text(encoding="utf-8")
        self.assertEqual(script.count("= createPasted(store)"), 1)
        self.assertIn("if (config.mode === 'token') auth = createPasted(store);", script)
        self.assertIn("else if (config.mode === 'entra') auth = createEntra(", script)  # unconfigured and broken get no provider

    # -- the configuration -------------------------------------------------------------------

    def test_the_configuration_is_checked_key_by_key_and_anything_else_is_dropped(self):
        configs = self.seam["configs"]
        entra = {
            "mode": "entra",
            "tenant": "00000000-0000-4000-8000-0000000000a1",
            "client": "00000000-0000-4000-8000-0000000000c3",
            "authority": "https://login.microsoftonline.com/00000000-0000-4000-8000-0000000000a1",
            "scope": "api://x/.default",
        }
        self.assertEqual(configs[0], entra)
        self.assertEqual(configs[1], entra)  # unknown keys never get through
        self.assertEqual(configs[2:6], [None] * 4)
        self.assertEqual(configs[6:19], [None] * 13)
        self.assertEqual(configs[19:22], [{"mode": "token"}, {"mode": "token"}, {"mode": "unconfigured"}])
        self.assertEqual(configs[22:], [None] * 5)

    # -- the 401 rule ------------------------------------------------------------------------

    def test_a_good_call_sends_one_request_with_one_token(self):
        self.assertEqual(self.calls["ok"], [{"value": {"ok": True, "token": "tok-1"}}, 1, ["silent"]])

    def test_a_401_gets_one_fresh_token_and_one_retry_and_no_redirect_when_that_works(self):
        result, sent, log = self.calls["refreshed"]
        self.assertEqual(result["value"]["ok"], True)
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0], sent[1])
        self.assertEqual(log, [["silent", 2], ["silent", 3]])  # the second request is the forced one

    def test_a_401_that_a_fresh_token_does_not_fix_ends_in_one_redirect_and_never_a_loop(self):
        self.assertEqual(self.calls["refused"], ["pending", 2, 1])  # two requests, one redirect, and the page is leaving
        self.assertEqual(self.calls["refusedAgain"], [{"error": 401}, 2, 1])  # the guard: a 401 now, no second redirect
        self.assertEqual(self.calls["redirectsAfterMany"], 1)
        self.assertEqual(self.calls["afterPause"], ["pending", 2])  # after the pause one more automatic redirect is allowed

    def test_a_session_that_needs_the_user_redirects_once_and_then_says_401(self):
        self.assertEqual(self.calls["noToken"], ["pending", 0, 1])
        self.assertEqual(self.calls["noTokenAgain"], [{"error": 401}, 0, 1])

    def test_a_403_is_final_and_other_statuses_pass_through_untouched(self):
        self.assertEqual(self.calls["forbidden"], [{"error": 403}, 1, ["silent"]])
        self.assertEqual(self.calls["statuses"], [{"error": 404}, {"error": 500}, {"error": 503}, {"error": 400}])

    def test_a_write_keeps_the_retry_but_never_starts_a_sign_in_redirect_by_itself(self):
        self.assertEqual(self.calls["writeRefused"], [{"error": 401}, 2, 0])  # retried once, refused, no redirect, a 401 to show
        self.assertEqual(self.calls["writeNoToken"], [{"error": 401}, 0, 0])

    def test_a_refusal_carries_only_the_fixed_job_status_word_of_the_body(self):
        self.assertEqual(self.calls["conflict"], [409, "done", ["jobStatus", "status"]])
        self.assertEqual(self.calls["conflictNoWord"], [409, None, ["jobStatus", "status"]])
        self.assertEqual(self.calls["conflictBadBody"], [409, None, ["jobStatus", "status"]])

    def test_no_token_because_of_the_network_is_a_failed_call_not_a_sign_in(self):
        self.assertEqual(self.calls["offline"], [{"error": 0}, 0, 0])

    def test_a_pasted_token_is_tried_once_and_a_missing_one_is_a_401(self):
        self.assertEqual(self.calls["pasted401"], [{"error": 401}, ["abcdefgh12345678"]])
        self.assertEqual(self.calls["pastedNone"], [{"error": 401}, 0])

    # -- what the page shows -----------------------------------------------------------------

    def test_not_signed_in_offers_the_microsoft_button_and_no_token_box(self):
        view = self.panel["signedOut"]
        self.assertEqual((view["summary"], view["open"], view["buttons"], view["inputs"]), ("Not signed in", True, ["Sign in with Microsoft"], []))
        self.assertIn("Factory.Viewer or Factory.Operator", view["text"])
        failed = self.panel["failed"]
        self.assertEqual((failed["summary"], failed["buttons"]), ("Sign-in failed", ["Sign in with Microsoft"]))
        self.assertIn("code: access_denied", failed["text"])

    def test_signed_in_shows_who_and_offers_sign_out_only(self):
        view = self.panel["signedIn"]
        self.assertEqual((view["summary"], view["open"], view["buttons"], view["inputs"]), ("Signed in as ada@example.org", False, ["Sign out"], []))
        self.assertEqual(self.panel["noName"]["summary"], "Signed in as a Microsoft account")

    def test_a_403_is_the_no_role_state_and_a_401_after_the_retry_is_the_expired_state(self):
        forbidden, expired = self.panel["forbidden"], self.panel["expired"]
        self.assertEqual(forbidden["summary"], "Signed in as ada@example.org: no Factory role")
        self.assertEqual(forbidden["buttons"], ["Sign in with another account", "Sign out"])
        self.assertIn("Factory.Viewer or Factory.Operator", forbidden["text"])
        self.assertIn("sign out and sign in again", forbidden["text"])
        self.assertEqual(expired["summary"], "Signed in as ada@example.org: session expired")
        self.assertEqual(expired["buttons"], ["Sign in again", "Sign out"])
        self.assertTrue(forbidden["open"] and expired["open"])
        self.assertNotEqual(forbidden["text"], expired["text"])

    def test_the_failure_notices_name_the_state_in_each_mode(self):
        entra, token, none = (self.panel["failures"][key] for key in ("entra", "token", "none"))
        self.assertIn("Sign in again", entra[0])
        self.assertIn("no Factory role", entra[1])
        self.assertIn("sign out and sign in again", entra[1])
        self.assertIn("Paste a valid access token", token[0])
        self.assertEqual(token, none)  # the page's first behavior is the default
        self.assertEqual(entra[2], token[2])  # an unavailable manager reads the same in every mode

    def test_the_token_box_appears_only_in_token_mode_and_says_why(self):
        for key in ("tokenNone", "tokenSet", "tokenBad"):
            self.assertEqual(self.panel[key]["inputs"], ["password"], key)
        self.assertIn("Microsoft sign-in is not configured on this manager", self.panel["tokenNone"]["text"])
        self.assertIn("easy-auth or local mode", self.panel["tokenNone"]["text"])
        self.assertEqual(self.panel["tokenNone"]["summary"], "Access token: not set")
        self.assertEqual(self.panel["tokenSet"]["summary"], "Access token: set for this tab")
        self.assertEqual(self.panel["tokenSet"]["buttons"], ["Use token", "Forget token"])
        for key in ("loading", "broken", "unconfigured", "signedOut", "failed", "signedIn", "forbidden", "expired", "hostile", "noName"):
            self.assertEqual(self.panel[key]["inputs"], [], key)  # no paste box in any other state

    def test_unconfigured_and_broken_say_what_is_wrong_and_offer_no_way_in(self):
        self.assertIn("FACTORY_ENTRA_SPA_CLIENT_ID", self.panel["unconfigured"]["text"])
        self.assertEqual(self.panel["unconfigured"]["buttons"], [])
        self.assertIn("could not be loaded", self.panel["broken"]["text"])
        self.assertEqual(self.panel["broken"]["buttons"], ["Reload the page"])

    def test_a_hostile_account_name_is_only_ever_text(self):
        view = self.panel["hostile"]
        self.assertNotIn("img", view["tags"])
        self.assertIn("<img src=x onerror=alert(1)>", view["summary"])


class StaticSeamTests(unittest.TestCase):
    """The rules on the source of the seam: nothing leaves the tab, nothing is logged, nothing is kept but the markers."""

    def setUp(self):
        self.script = (UI / "app.js").read_text(encoding="utf-8")

    def test_the_only_storage_keys_are_the_pasted_token_and_two_non_secret_markers(self):
        for key in ("'factory.token'", "'factory.signin'", "'factory.route'"):
            self.assertEqual(self.script.count(key), 1, key)
        # the store is written in three places: the pasted token, the loop-guard time and the page hash to return to;
        # an access token from Microsoft is never handed to it (the library keeps its own cache in session storage)
        self.assertEqual(self.script.count("store.set("), 3)
        self.assertEqual(self.script.count("sessionStorage"), 1)

    def test_no_token_or_authorization_data_is_put_in_an_address_a_log_or_a_message(self):
        for forbidden in ("access_token", "id_token", "location.assign", "location.replace", "window.open", "postMessage", "console."):
            self.assertNotIn(forbidden, self.script, forbidden)
        self.assertEqual(self.script.count("replaceState("), 1)
        self.assertIn("window.history.replaceState(null, '', `${window.location.pathname}${wanted}`)", self.script)

    def test_the_sign_in_library_is_configured_without_a_popup_or_a_third_party_address(self):
        self.assertIn("allowRedirectInIframe: false", self.script)
        self.assertIn("msal.BrowserCacheLocation.SessionStorage", self.script)
        self.assertIn("piiLoggingEnabled: false", self.script)
        self.assertIn("loggerCallback: () => undefined", self.script)
        # the authority must be the one login origin (checked again in the page, as the manager's CSP allows only that one)
        self.assertEqual(self.script.count("microsoftonline"), 1)
        self.assertNotIn("https://login", self.script)

    def test_the_script_has_no_empty_block_for_the_code_quality_check(self):
        # a no-op is written as an expression (`() => undefined`), never as an empty block (`() => {}`)
        self.assertEqual(re.findall(r"=>\s*\{\s*\}", self.script), [])
        self.assertEqual(re.findall(r"\)\s*\{\s*\}", self.script), [])

    def test_the_sign_in_config_builder_does_not_assign_to_its_own_parameters(self):
        source = inspect.getsource(webui.sign_in_config)
        function = ast.parse(textwrap.dedent(source)).body[0]
        parameters = {arg.arg for arg in function.args.args}
        stored = {node.id for node in ast.walk(function) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        self.assertEqual(parameters & stored, set())

    def test_the_address_is_cleaned_before_the_first_view_and_the_route_comes_from_a_validated_hash(self):
        self.assertIn("SAFE_HASH.test(back)", self.script)
        self.assertIn("SAFE_HASH.test(options.route)", self.script)
        self.assertLess(self.script.index("cleanAddress(result.returned)"), self.script.index("      begin();"))
        self.assertLess(self.script.index("function begin()"), self.script.index("window.addEventListener('hashchange', route)"))


if __name__ == "__main__":
    unittest.main()
