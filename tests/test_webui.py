"""The browser page of the dashboard: the static shell at ``/ui/`` (docs/manager.md, "The browser page").

A real ``ManagerServer`` on fakes, as in tests/test_dashboard_api.py. What is pinned:

* the shell (page, its three assets and the config route) is the only thing served without an identity, in both authentication modes,
  and every other path, every data call and every other method on a shell path keeps the 401, 403, 404, 405 order;
* only the allow-list is ever served (no traversal, no variants, no symlink, no oversized file, no half page);
* the security headers and the cache rules;
* a page view starts no process and makes no GitHub call;
* the files are shipped (package data) and follow the rules that keep agent text inert (no innerHTML and friends);
* when node is installed, the pure functions of app.js and its renderers run against hostile data in a fake DOM.
"""

import fnmatch
import hashlib
import http.client
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import webui
from tests import test_manager as tm

SRC = Path(webui.__file__).resolve().parent
UI = SRC / "ui"
REPO_ROOT = SRC.parent.parent
NOT_FOUND = {"error": "not found"}
SHELL_PATHS = ("/ui/", "/ui/app.js", "/ui/style.css", "/ui/vendor/msal-browser.min.js")
CONFIG_PATH = "/ui/config.json"
# The pinned SHA-256 of the vendored sign-in library, as src/factory/ui/vendor/README.md promises: a changed
# file must change this value and that README together, on purpose.
MSAL_SHA256 = "56a548eae945c2479adf61d969140999e9f602ff627694cc0692346890556f52"
EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Paths that look like the shell but are not on the allow-list: all of them are ordinary unknown paths.
LOOKALIKES = (
    "/ui/index.html",
    "/ui/../healthz",
    "/ui/%2e%2e/healthz",
    "/ui/%2E%2E/%2E%2E/etc/passwd",
    "/ui/app.js/",
    "/ui//app.js",
    "/ui/./app.js",
    "/ui/%61pp.js",
    "/ui/app.js%00",
    "/ui/APP.JS",
    "/ui/app.js.map",
    "/ui/missing.js",
    "/ui/ui/",
    "/uix",
    "/ui/sub/",
    "/UI/",
)
DATA_PATHS = (
    "/",
    "/repos",
    "/dashboard",
    "/history/runs",
    "/history/jobs",
    "/repos/org/name/runs/r1/jobs/fix-a",
    "/repos/org/name/runs/r1/status",
    "/trigger-pause",
    "/metrics",
    "/favicon.ico",
    "/robots.txt",
)


class Fetch:
    """Raw requests (the page is not JSON, which ``ServerCase.call`` expects)."""

    def raw(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def raw_json(self, method, path, headers=None):
        status, body, headers = self.raw(method, path, headers)
        return status, json.loads(body) if body else None, headers


class ShellTests(Fetch, tm.ServerCase):
    def test_the_page_and_its_assets_are_served_without_any_identity(self):
        for path in SHELL_PATHS:
            with self.subTest(path=path):
                status, body, headers = self.raw("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, webui.load()[path].body)
                self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(self.raw("GET", "/ui/app.js")[1], (UI / "app.js").read_bytes())
        self.assertEqual(self.raw("GET", "/ui/style.css")[1], (UI / "style.css").read_bytes())
        self.assertEqual(self.runner.calls, [])

    def test_content_types(self):
        types = {path: self.raw("GET", path)[2]["Content-Type"] for path in SHELL_PATHS}
        self.assertEqual(
            types,
            {
                "/ui/": "text/html; charset=utf-8",
                "/ui/app.js": "text/javascript; charset=utf-8",
                "/ui/style.css": "text/css; charset=utf-8",
                "/ui/vendor/msal-browser.min.js": "text/javascript; charset=utf-8",
            },
        )

    def test_every_answer_carries_the_security_headers(self):
        for path in (*SHELL_PATHS, "/ui"):
            with self.subTest(path=path):
                _, _, headers = self.raw("GET", path)
                self.assertEqual(headers["Content-Security-Policy"], EXPECTED_CSP)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
                self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
                self.assertEqual(headers["Permissions-Policy"], webui.PERMISSIONS_POLICY)
                self.assertNotIn("Strict-Transport-Security", headers)  # only where the request arrived over HTTPS

    def test_the_page_is_never_cached_and_an_asset_only_under_its_own_version(self):
        page = self.raw("GET", "/ui/")
        self.assertEqual(page[2]["Cache-Control"], "no-store")
        html = page[1].decode("utf-8")
        for name in ("app.js", "style.css", "vendor/msal-browser.min.js"):
            version = webui.version_of((UI / name).read_bytes())
            self.assertIn(f'"{name}?v={version}"', html)
            self.assertNotIn("__VERSION", html)
            with self.subTest(name=name):
                self.assertEqual(self.raw("GET", f"/ui/{name}?v={version}")[2]["Cache-Control"], webui.IMMUTABLE)
                for query in ("", "?v=0000000000000000", "?v=", f"?v={version}&x=1", "?x=1"):
                    self.assertEqual(self.raw("GET", f"/ui/{name}{query}")[2]["Cache-Control"], "no-cache", query)

    def test_a_query_on_the_page_changes_nothing(self):
        plain = self.raw("GET", "/ui/")
        queried = self.raw("GET", "/ui/?token=abc&x=1#frag")
        self.assertEqual((queried[0], queried[1]), (plain[0], plain[1]))

    def test_the_address_without_a_slash_redirects_to_the_page(self):
        status, body, headers = self.raw("GET", "/ui")
        self.assertEqual((status, body, headers["Location"]), (307, b"", "/ui/"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.raw("GET", "/ui?x=1")[2]["Location"], "/ui/")

    def test_only_the_allow_list_is_public_and_lookalikes_follow_the_normal_order(self):
        for path in LOOKALIKES:
            with self.subTest(path=path):
                self.assertEqual(self.raw_json("GET", path)[:2], (401, {"error": "authentication required"}))
                self.assertEqual(self.raw_json("GET", path, {"X-MS-CLIENT-PRINCIPAL": tm.NO_ROLE})[:2], (403, {"error": "forbidden"}))
                self.assertEqual(self.raw_json("GET", path, {"X-MS-CLIENT-PRINCIPAL": tm.VIEWER})[:2], (404, NOT_FOUND))

    def test_the_allow_list_is_exactly_the_four_files_the_config_route_and_the_redirect(self):
        self.assertEqual(tuple(path for path, _, _ in webui.SHELL), SHELL_PATHS)
        for path in (*SHELL_PATHS, CONFIG_PATH, "/ui"):
            self.assertTrue(webui.addresses(path))
        for path in (*LOOKALIKES, *DATA_PATHS, "", "/ui/index.html"):
            self.assertFalse(webui.addresses(path), path)

    def test_other_methods_on_a_shell_path_are_not_public(self):
        for path in (*SHELL_PATHS, "/ui"):
            for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.raw(method, path)[0], 401)
                    self.assertEqual(self.raw(method, path, {"X-MS-CLIENT-PRINCIPAL": tm.NO_ROLE})[0], 403)
                    for principal in (tm.VIEWER, tm.OPERATOR):
                        status, _, headers = self.raw(method, path, {"X-MS-CLIENT-PRINCIPAL": principal})
                        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_no_other_path_became_public(self):
        for path in DATA_PATHS:
            for method in ("GET", "POST", "HEAD"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.raw(method, path)[0], 401)

    def test_the_data_calls_the_page_makes_still_need_a_role(self):
        for path in ("/dashboard", "/history/runs", "/history/jobs?limit=5"):
            with self.subTest(path=path):
                self.assertEqual(self.raw_json("GET", path)[:2], (401, {"error": "authentication required"}))
                self.assertEqual(self.raw_json("GET", path, {"X-MS-CLIENT-PRINCIPAL": tm.NO_ROLE})[:2], (403, {"error": "forbidden"}))
                self.assertEqual(self.raw_json("GET", path, {"X-MS-CLIENT-PRINCIPAL": tm.VIEWER})[0], 200)

    def test_a_page_view_starts_no_process_and_makes_no_github_call(self):
        def refuse(*args, **kwargs):
            raise AssertionError("a process was started")

        with patch.object(subprocess, "Popen", refuse), patch.object(subprocess, "run", refuse):
            for path in (*SHELL_PATHS, "/ui"):
                self.raw("GET", path)
            for path in ("/dashboard", "/history/runs", "/history/jobs"):
                self.assertEqual(self.raw_json("GET", path, {"X-MS-CLIENT-PRINCIPAL": tm.VIEWER})[0], 200)
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_the_audit_line_names_the_path_without_the_query(self):
        self.raw("GET", "/ui/?token=sekret-value")
        line = [line for line in self.out.getvalue().splitlines() if line.startswith("audit")][-1]
        self.assertIn("principal=anonymous", line)
        self.assertIn("path=/ui/ ", f"{line} ")
        self.assertNotIn("sekret", f"{self.out.getvalue()}{self.err.getvalue()}")

    def test_the_page_is_counted_under_its_own_route(self):
        self.raw("GET", "/ui/")
        text = self.manager.scrape_metrics()
        self.assertIn('factory_manager_requests_total{route="ui",method="GET",status_class="2xx"} 1', text)

    def test_a_missing_file_is_an_unknown_path_and_the_page_recovers_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.manager.webui = webui.Shell(directory)
            self.assertEqual(self.raw_json("GET", "/ui/")[:2], (404, NOT_FOUND))
            (directory / "vendor").mkdir()
            for _, name, _ in webui.SHELL:
                shutil.copy(UI / name, directory / name)
            self.assertEqual(self.raw("GET", "/ui/")[0], 200)
            self.assertEqual(self.raw("GET", "/ui/app.js")[0], 200)


class JwtShellTests(Fetch, tm.JwtCase):
    def test_the_shell_is_public_and_the_data_calls_need_a_verified_token(self):
        for path in SHELL_PATHS:
            self.assertEqual(self.raw("GET", path)[0], 200)
        # a bogus or stale Authorization header on the shell is simply not read
        self.assertEqual(self.raw("GET", "/ui/", {"Authorization": "Bearer not-a-token"})[0], 200)
        for path in ("/dashboard", "/history/runs", "/history/jobs", "/repos", "/repos/org/name/runs/r1/jobs/fix-a", "/ui/other"):
            with self.subTest(path=path):
                status, payload, headers = self.raw_json("GET", path)
                self.assertEqual((status, payload, headers["WWW-Authenticate"]), (*tm.UNAUTHENTICATED, "Bearer"))
                self.assertEqual(self.raw_json("GET", path, {"X-MS-CLIENT-PRINCIPAL": tm.OPERATOR})[0], 401)  # Easy Auth header ignored
                self.assertEqual(self.raw_json("GET", path, {"Authorization": "Bearer not-a-token"})[0], 401)

    def test_a_token_without_a_factory_role_is_403_and_a_viewer_reads(self):
        nobody = {"Authorization": f"Bearer {self.token(roles=[])}"}
        viewer = {"Authorization": f"Bearer {self.token(roles=['Factory.Viewer'])}"}
        for path in ("/dashboard", "/history/runs"):
            self.assertEqual(self.raw_json("GET", path, nobody)[:2], (403, {"error": "forbidden"}))
            self.assertEqual(self.raw_json("GET", path, viewer)[0], 200)
        self.assertEqual(self.raw_json("GET", "/ui/other", viewer)[:2], (404, NOT_FOUND))
        self.assertEqual(self.raw("POST", "/ui/", viewer)[0], 405)
        self.assertEqual(self.raw("POST", "/ui/")[0], 401)

    def test_the_shell_needs_no_signing_keys(self):
        self.assertEqual(self.fetch.urls, [])
        for path in SHELL_PATHS:
            self.raw("GET", path)
        self.assertEqual(self.fetch.urls, [])  # the keys were never asked for: no token was read


class ShellFileTests(unittest.TestCase):
    def copy_shell(self, directory):
        (directory / "vendor").mkdir(exist_ok=True)
        for _, name, _ in webui.SHELL:
            shutil.copy(UI / name, directory / name)

    def test_the_shipped_files_load_and_the_versions_are_those_of_the_files(self):
        loaded = webui.load()
        self.assertEqual(set(loaded), set(SHELL_PATHS))
        self.assertEqual(loaded["/ui/app.js"].version, webui.version_of((UI / "app.js").read_bytes()))
        self.assertIn(loaded["/ui/style.css"].version.encode(), loaded["/ui/"].body)
        self.assertIn(loaded["/ui/vendor/msal-browser.min.js"].version.encode(), loaded["/ui/"].body)

    def test_a_symlink_a_directory_a_missing_or_oversized_file_is_refused_and_no_half_page_is_served(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.copy_shell(directory)
            self.assertIsNotNone(webui.load(directory))
            outside = directory / "outside.js"
            outside.write_text("secret")
            (directory / "app.js").unlink()
            (directory / "app.js").symlink_to(outside)
            self.assertIsNone(webui.read_file(directory, "app.js"))
            self.assertIsNone(webui.load(directory))
            (directory / "app.js").unlink()
            (directory / "app.js").mkdir()
            self.assertIsNone(webui.load(directory))
            (directory / "app.js").rmdir()
            self.assertIsNone(webui.load(directory))  # missing
            (directory / "app.js").write_bytes(b"x" * (webui.MAX_ASSET_BYTES + 1))
            self.assertIsNone(webui.load(directory))
            (directory / "app.js").write_bytes(b"x" * webui.MAX_ASSET_BYTES)
            self.assertIsNotNone(webui.load(directory))

    def test_the_shell_reads_each_file_once_and_retries_after_a_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            shell = webui.Shell(directory)
            self.assertIsNone(shell.asset("/ui/"))
            self.copy_shell(directory)
            first = shell.asset("/ui/app.js")
            self.assertIsNotNone(first)
            (directory / "app.js").write_text("changed")
            self.assertIs(shell.asset("/ui/app.js"), first)
            self.assertIsNone(shell.asset("/ui/other.js"))

    def test_the_redirect_and_asset_headers_have_the_security_set(self):
        loaded = webui.load()
        for asset in loaded.values():
            self.assertLessEqual(webui.SECURITY_HEADERS.items(), webui.headers_for(asset).items())
        self.assertLessEqual(webui.SECURITY_HEADERS.items(), webui.redirect_headers().items())
        self.assertEqual(webui.CSP, EXPECTED_CSP)


def setuptools_floor():
    """The major version of setuptools that ``build-system.requires`` of pyproject.toml asks for."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        requires = " ".join(tomllib.load(handle)["build-system"]["requires"])
    return int(re.search(r"setuptools\s*>=\s*(\d+)", requires).group(1))


def ambient_setuptools_major():
    """The major version of the setuptools this interpreter imports, 0 when there is none."""
    if importlib.util.find_spec("setuptools") is None:
        return 0
    try:
        return int(importlib.metadata.version("setuptools").split(".")[0])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return 0


def dockerfile_context_sources(text):
    """What the Dockerfile takes from the build context: the sources of every ``COPY`` that is not ``--from`` another
    stage, and the ``source=`` of every bind mount that has no ``from=``. Continuation lines are joined first."""
    found = []
    for line in re.sub(r"\\\n", " ", text).splitlines():
        words = line.split()
        if words[:1] == ["COPY"]:
            arguments = [word for word in words[1:] if not word.startswith("--")]
            if not any(word.startswith("--from=") for word in words[1:]):
                found.extend(argument.rstrip("/") for argument in arguments[:-1])  # the last word is the destination
        elif words[:1] == ["RUN"]:
            for mount in re.findall(r"--mount=(\S+)", line):
                options = dict(option.partition("=")[::2] for option in mount.split(","))
                if options.get("type") == "bind" and "from" not in options and "source" in options:
                    found.append(options["source"].strip("/"))
    return found


def dockerignore_excludes(lines, path):
    """Whether a build context built with these .dockerignore lines leaves ``path`` out.

    The rule of Docker: the lines are read in order, a line that matches the path or one of its parent directories turns
    it out, and a ``!`` line turns it back in. Only the pattern forms this repository uses: ``*``, ``?`` and ``**``.
    """
    parts = path.split("/")
    candidates = ["/".join(parts[: count + 1]) for count in range(len(parts))]
    excluded = False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        if negated != excluded:  # a "!" line only matters for a path that is out, any other line for one that is in
            continue
        regex = "".join(
            {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(piece) or re.escape(piece)
            for piece in re.split(r"(\*\*/|\*\*|\*|\?)", pattern.strip("/"))
        )
        if any(re.fullmatch(regex, candidate) for candidate in candidates):
            excluded = not negated
    return excluded


class PackagingTests(unittest.TestCase):
    def test_every_shipped_file_matches_the_package_data_and_nothing_else_is_in_the_directory(self):
        with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
            patterns = tomllib.load(handle)["tool"]["setuptools"]["package-data"]["factory"]
        names = sorted(name for _, name, _ in webui.SHELL)
        for name in names:
            self.assertTrue((UI / name).is_file(), name)
            self.assertTrue(any(fnmatch.fnmatch(f"ui/{name}", pattern) for pattern in patterns), name)
        self.assertEqual(sorted(entry.name for entry in UI.iterdir()), sorted({name.split("/")[0] for name in names}))
        self.assertEqual(
            sorted(entry.name for entry in (UI / "vendor").iterdir()),
            ["README.md", "msal-browser.LICENSE", "msal-browser.min.js"],
        )
        for entry in (UI / "vendor").iterdir():  # the licence and the provenance ship with the library
            self.assertTrue(any(fnmatch.fnmatch(f"ui/vendor/{entry.name}", pattern) for pattern in patterns), entry.name)
        self.assertFalse(any(entry.is_symlink() for entry in (*UI.iterdir(), *(UI / "vendor").iterdir())))

    def test_a_setuptools_build_copies_the_page_files_into_the_package(self):
        """The real build step of the image (``pip install /src``), without the wheel tooling: ``build_py`` on a copy.

        ``dist_info`` runs on the same copy: the metadata of the package must carry the SPDX licence and both licence files.
        """
        floor = setuptools_floor()
        ambient = ambient_setuptools_major()
        if ambient < floor:
            reason = (
                f"setuptools {floor} or newer is needed to read the licence fields of pyproject.toml "
                f"(the ambient one is {ambient or 'missing'}); `python -m pip install --upgrade 'setuptools>={floor}'`"
            )
            if os.environ.get("CI"):  # the CI job installs it: a run there that skipped would hide a broken build
                self.fail(reason)
            self.skipTest(reason)
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp, "work")
            work.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):  # what the Dockerfile copies next to src
                shutil.copy(REPO_ROOT / name, work)
            shutil.copytree(SRC.parent, work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            built = Path(temp, "lib")
            command = [sys.executable, "-c", "import setuptools; setuptools.setup()", "-q", "build_py", "--build-lib", str(built)]
            done = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=120, check=False)
            self.assertEqual(done.returncode, 0, done.stderr[-500:])
            for _, name, _ in webui.SHELL:
                self.assertEqual((built / "factory" / "ui" / name).read_bytes(), (UI / name).read_bytes(), name)
            self.assertIsNotNone(webui.load(built / "factory" / "ui"))
            info = Path(temp, "info")
            info.mkdir()
            command = [sys.executable, "-c", "import setuptools; setuptools.setup()", "-q", "dist_info", "--output-dir", str(info)]
            done = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=120, check=False)
            self.assertEqual(done.returncode, 0, done.stderr[-500:])
            self.assertNotRegex(done.stderr, r"(?i)licen[cs]e.*deprecated")  # a deprecated licence field is a build that stops working one day
            (dist_info,) = info.glob("*.dist-info")
            metadata = (dist_info / "METADATA").read_text(encoding="utf-8")
            self.assertIn("License-Expression: Apache-2.0", metadata)
            for name in ("LICENSE", "NOTICE"):
                self.assertEqual((dist_info / "licenses" / name).read_bytes(), (REPO_ROOT / name).read_bytes(), name)

    def test_the_licence_files_exist_and_everything_the_dockerfile_takes_from_the_context_is_re_included(self):
        for name in ("LICENSE", "NOTICE"):  # the Dockerfile copies both next to pyproject.toml
            self.assertTrue((REPO_ROOT / name).is_file(), name)
        with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
            self.assertEqual(tomllib.load(handle)["project"]["license-files"], ["LICENSE", "NOTICE"])
        patterns = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        sources = dockerfile_context_sources((REPO_ROOT / "Dockerfile").read_text(encoding="utf-8"))
        # the parser found the COPY lines and the bind mount, not nothing
        self.assertLessEqual({"pyproject.toml", "README.md", "LICENSE", "NOTICE", "src", "image/toolchains.sh", "managed-settings.json"}, set(sources))
        for source in sources:
            with self.subTest(source=source):
                target = REPO_ROOT / source
                self.assertTrue(target.exists(), source)
                self.assertFalse(dockerignore_excludes(patterns, source), source)
                if target.is_dir():  # a directory is no good if the files in it are all left out
                    files = sorted(
                        path.relative_to(REPO_ROOT).as_posix()
                        for path in target.rglob("*")
                        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and not any(p.endswith(".egg-info") for p in path.parts)
                    )
                    self.assertTrue(files, source)
                    self.assertFalse(dockerignore_excludes(patterns, files[0]), files[0])
        # the matcher is not vacuous: what the image does not take stays out, and a lost re-inclusion shows
        for left_out in (".git/config", "tests/test_webui.py", ".github/workflows/ci.yml", "src/factory/__pycache__/cli.cpython-313.pyc", "infra/aks.tf"):
            self.assertTrue(dockerignore_excludes(patterns, left_out), left_out)
        for lost in ("!LICENSE", "!NOTICE", "!src", "!tools/token_broker.py", "!image/claude/package.json"):
            without = [line for line in patterns if line.strip() != lost]
            self.assertEqual(len(without), len(patterns) - 1, lost)
            path = lost[1:]
            self.assertFalse(dockerignore_excludes(patterns, path), path)
            self.assertTrue(dockerignore_excludes(without, path), path)

    def test_the_image_build_context_and_install_carry_the_package_data(self):
        dockerignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()
        self.assertIn("!src", dockerignore)
        self.assertFalse([line for line in dockerignore if line.startswith("src/") or line.endswith(("*.js", "*.css", "*.html"))])
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        self.assertIn("COPY src /src/src", dockerfile)
        smoke = (REPO_ROOT / "image" / "smoke.sh").read_text()
        self.assertIn("webui.load()", smoke)


class StaticRuleTests(unittest.TestCase):
    """The rules that keep text from a run inert: checked on the source, so a change that breaks one fails here."""

    def setUp(self):
        self.index = (UI / "index.html").read_text(encoding="utf-8")
        self.script = (UI / "app.js").read_text(encoding="utf-8")
        self.style = (UI / "style.css").read_text(encoding="utf-8")

    def test_the_script_never_parses_markup_or_runs_text(self):
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\"", "DOMParser", "createContextualFragment", "srcdoc"):
            self.assertNotIn(forbidden, self.script, forbidden)

    def test_a_view_never_shows_a_left_out_part_as_text(self):
        # A view leaves out what it does not show as null (the signed-out board has no "Try again"); replaceChildren would
        # insert the text "null" under the sign-in prompt, so show() drops it first, as the box of a stop control does.
        show = self.script[self.script.index("    function show(nodes, focus) {"):]
        show = show[: show.index("    function markNav(")]
        self.assertIn("main.replaceChildren(...nodes.filter((node) => node !== null && node !== undefined));", show)
        self.assertNotIn("main.replaceChildren(...nodes);", self.script)

    def test_the_script_parses_in_older_browsers(self):
        # A regular expression with a lookbehind is a syntax error before Safari 16.4: the whole script would not run.
        for forbidden in ("(?<!", "(?<="):
            self.assertNotIn(forbidden, self.script, forbidden)

    def test_the_script_spreads_objects_instead_of_assigning_into_a_literal(self):
        # object spread builds the same copy; Object.assign into an object literal is a flagged best-practice violation
        self.assertIsNone(re.search(r"Object\.assign\(\s*\{", self.script))

    def test_the_script_sets_no_style_stores_nothing_but_the_token_and_logs_nothing(self):
        for forbidden in (".style", "cssText", "localStorage", "indexedDB", "document.cookie", "console.", "XMLHttpRequest", "WebSocket", "sendBeacon", "postMessage", "import(", "importScripts"):
            self.assertNotIn(forbidden, self.script, forbidden)
        self.assertEqual(self.script.count("sessionStorage"), 1)  # createStore: the one place that touches storage

    def test_the_token_reaches_one_fetch_to_a_same_origin_path_and_never_a_url(self):
        # two fetches in all: the API call (the only one with a token) and the public config route (none)
        self.assertEqual(self.script.count("window.fetch("), 2)
        self.assertEqual(self.script.count("Authorization"), 1)
        config_call = self.script[self.script.index("window.fetch('/ui/config.json'") :].split(".then(", 1)[0]
        self.assertNotIn("Authorization", config_call)
        self.assertIn("credentials: 'omit'", config_call)
        self.assertIn("!/^\\/[a-z]/.test(path)", self.script)
        self.assertNotIn("token=", self.script)
        self.assertNotIn("location.search", self.script)
        self.assertNotIn("location.href", self.script)

    def test_no_third_party_address_in_any_file(self):
        for text in (self.index, self.script, self.style):
            self.assertNotIn("http://", text)
            self.assertNotIn("@import", text)
            self.assertNotIn("url(", text)
        self.assertEqual(sorted({part.split("/")[0] for part in self.script.split("https://")[1:]}), ["github.com"])
        self.assertNotIn("https://", self.index)

    def test_the_page_has_no_inline_script_style_or_handler_and_has_landmarks(self):
        lowered = self.index.lower()
        self.assertEqual(lowered.count("<script"), 2)  # the vendored sign-in library, then the page's own script
        self.assertIn('src="vendor/msal-browser.min.js?v=__version_msal__"', lowered)
        self.assertIn('src="app.js?v=__version_app__"', lowered)
        self.assertLess(lowered.index("msal-browser"), lowered.index("app.js"))
        for forbidden in (" style=", "<style", " onclick", " onload", " onerror", "javascript:", "<iframe", "<object", "<embed", "<form"):
            self.assertNotIn(forbidden, lowered, forbidden)
        for landmark in ("<header", "<nav", "<main", "<footer", 'lang="en"', 'name="viewport"', "skip"):
            self.assertIn(landmark, lowered, landmark)

    def test_the_vendored_sign_in_library_is_exactly_the_pinned_file(self):
        library = (UI / "vendor" / "msal-browser.min.js").read_bytes()
        self.assertEqual(hashlib.sha256(library).hexdigest(), MSAL_SHA256)
        readme = (UI / "vendor" / "README.md").read_text(encoding="utf-8")
        self.assertIn(MSAL_SHA256, readme)
        self.assertIn("5.25.1", readme)
        self.assertTrue(library.startswith(b"/*! @azure/msal-browser v5.25.1"))
        for forbidden in (b"eval(", b"new Function", b"innerHTML", b"sourceMappingURL"):
            self.assertNotIn(forbidden, library, forbidden)

    def test_the_style_sheet_is_responsive_and_keeps_the_focus_visible(self):
        self.assertIn("@media (max-width", self.style)
        self.assertIn(":focus-visible", self.style)
        self.assertIn("prefers-color-scheme: dark", self.style)

    def test_the_style_sheet_is_built_on_tokens_with_dark_tokens_a_phone_navigation_and_focus_outlines(self):
        root, dark = self.style.split("@media (prefers-color-scheme: dark)", 1)
        dark = dark.split("* { box-sizing", 1)[0]
        for token in ("--text-md", "--text-xl", "--space-2", "--space-4", "--bg", "--ink", "--accent", "--ok-tint", "--bad-tint", "--warn-tint"):
            self.assertIn(f"{token}:", root, token)
        # the dark scheme redefines the colours (not only the page background) and none of the scales
        for token in ("--bg", "--panel", "--ink", "--muted", "--line", "--accent", "--focus", "--ok-ink", "--warn-ink", "--bad-ink", "--info-ink"):
            self.assertIn(f"{token}:", dark, token)
        for token in ("--text-md", "--space-2"):
            self.assertNotIn(f"{token}:", dark, token)
        phone = re.search(r"@media \(max-width: 40em\) \{(.*?)\n\}", self.style, re.S)
        self.assertIsNotNone(phone)
        self.assertIn(".top nav", phone.group(1))
        self.assertIsNotNone(re.search(r":focus-visible[^{]*\{[^}]*outline: 3px solid var\(--focus\)", self.style))
        for status in ("running", "pr_ready", "pr_incomplete", "blocked", "failed", "needs_human_publish", "merged"):
            self.assertIn(f".chip.{status}", self.style, status)
        self.assertIn(".state.loading", self.style)
        self.assertIn(".state.error", self.style)
        # an interrupted job (its supervisor died) has the tone of a stopped one
        warn = re.search(r"([^{}]*)\{[^{}]*--chip-ink: var\(--warn-ink\)", self.style).group(1)
        self.assertIn(".chip.interrupted", warn)
        self.assertIn(".chip.stopped", warn)

    def test_a_hover_never_changes_the_layout(self):
        # On a touch screen a tap leaves the hover on until the next one: a rule for :hover that moved or resized
        # anything would send the click to whatever moved under the finger (a fold that never opens, a missed Stop).
        allowed = {"background", "background-color", "color", "border-color", "text-decoration-color", "box-shadow", "outline-color"}
        style = re.sub(r"/\*.*?\*/", "", self.style, flags=re.S)
        hovers = [(selector.strip(), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style) if ":hover" in selector]
        self.assertTrue(hovers)
        for selector, body in hovers:
            properties = {part.split(":", 1)[0].strip() for part in body.split(";") if part.strip()}
            self.assertLessEqual(properties, allowed, selector)

    def test_reduced_motion_stops_the_pseudo_elements_too(self):
        # The pulse of "worker running", the loading spinner and the chevrons are drawn on ::before.
        block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", self.style, re.S).group(1)
        rule = re.search(r"([^{}]+)\{([^{}]*)\}", block)
        selectors = {part.strip() for part in rule.group(1).split(",")}
        self.assertLessEqual({"*", "*::before", "*::after"}, selectors)
        self.assertIn("animation: none !important", rule.group(2))
        self.assertIn("transition: none !important", rule.group(2))

    def test_the_page_starts_with_a_loading_state_in_the_shared_shape(self):
        self.assertIn('class="state loading"', self.index)
        self.assertIn('aria-busy="true"', self.index)


NODE = shutil.which("node")

PURE = r"""
const app = require(process.argv[1]);
const out = {};
out.dur = [null, -1, 0, 5.4, 59.6, 60, 125, 3600, 3725, 90000, 200000].map(app.fmtDuration);
out.cost = [null, -1, 0, 1.234, 10].map(app.fmtCost);
out.time = ['2026-11-08T12:30:59.5Z', 'nope', null, '2026-11-08T12:30:00+02:00'].map(app.fmtTime);
out.age = [null, 5, 70].map(app.fmtAge);
out.since = [app.secondsSince('2026-11-08T12:00:00Z', Date.parse('2026-11-08T12:00:30Z')), app.secondsSince('x', 1), app.secondsSince('2030-01-01T00:00:00Z', 0)];
out.repo = ['org/name', 'org/na me', 'org/../x', '../x/y', 'a/.', 'org', 'org/name/extra', '', null, 'o/n;rm'].map(app.isRepo);
out.issue = [app.issueUrl('org/name', 7), app.issueUrl('org/name', 0), app.issueUrl('org/name', '7'), app.issueUrl('bad repo', 7), app.issueUrl('org/name', 7.5), app.issueUrl('org/name', 2 ** 31)];
out.pr = [
  app.prLink('https://github.com/org/name/pull/7', 'org/name'),
  app.prLink('https://github.com/org/name/pull/7'),
  app.prLink('https://github.com/org/name/pull/7', 'org/other'),
  app.prLink('https://evil.example/org/name/pull/7', 'org/name'),
  app.prLink('http://github.com/org/name/pull/7'),
  app.prLink('https://github.com/org/name/pull/7/files'),
  app.prLink('https://github.com/org/name/pull/7?x=1'),
  app.prLink('https://github.com.evil.example/org/name/pull/7'),
  app.prLink('javascript:alert(1)'),
  app.prLink('https://github.com/org/name/issues/7'),
  app.prLink(null),
];
out.token = ['abcdefgh12345678', '  Bearer abc.def-ghi_jkl  ', 'bearer\tabcdefgh', 'short', 'has space inside', 'x'.repeat(8193), 'abc.def?x=1', '', null, 'a b c d e f g h'].map(app.tokenFrom);
out.query = [
  app.buildQuery({}),
  app.buildQuery({ repo: 'org/name', outcome: 'merged', since: '2026-11-01', until: '2026-11-08', limit: 50 }),
  app.buildQuery({ repo: 'org/na me', outcome: 'exploded', since: '11/01/2026', until: 'x', limit: 0 }),
  app.buildQuery({ cursor: '1767225600.000000|org/name|r1|fix-a', limit: 200, run: 'r1' }),
  app.buildQuery({ cursor: 'bad cursor', limit: 201, run: '../x' }),
  app.buildQuery({ repo: 'org/name&x=1', outcome: 'merged' }),
];
out.filters = [
  app.cleanFilters(undefined),
  app.cleanFilters({ kind: 'jobs', repo: 'org/name', outcome: 'closed', since: '2026-01-01', until: 'bad', limit: '100' }),
  app.cleanFilters({ kind: 'x', limit: '7', outcome: 'nope', repo: '../..' }),
];
out.route = [
  app.parseRoute(''), app.parseRoute('#/active'), app.parseRoute('#/nonsense'), app.parseRoute(undefined),
  app.parseRoute('#/history'), app.parseRoute('#/history?kind=jobs&repo=org%2Fname&outcome=blocked&limit=50'),
  app.parseRoute('#/history?repo=%E0%A4%A&outcome=merged'),
  app.parseRoute('#/job/org/name/r1/fix-a'), app.parseRoute('#/job/org/name/r1'), app.parseRoute('#/job/org/name/../fix-a'),
  app.parseRoute('#/job/org/na me/r1/fix-a'), app.parseRoute('#/job/org/name/r1/fix-a/extra'),
];
out.hash = [app.jobHash('org/name', 'r1', 'fix-a'), app.jobHash('org/name', 'r 1', 'fix-a'), app.jobHash('org/name', 'r1', ''),
  app.historyHash({ kind: 'jobs', repo: 'org/name', outcome: 'merged', limit: 50 }), app.historyHash({}), app.historyHash({ repo: 'not a repo' })];
out.roundtrip = app.parseRoute(app.historyHash({ kind: 'jobs', repo: 'org/name', outcome: 'merged', since: '2026-01-01', until: '2026-02-01', limit: 100 }));
out.path = [app.jobPath('org/name', 'r1', 'fix-a'), app.jobPath('org/name', 'r1', '../x'), app.jobPath('org', 'r1', 'x')];
out.state = [
  app.repoState({ runs: [] }), app.repoState({}), app.repoState(null),
  app.repoState({ runs: [{ supervisor: { state: 'idle' } }, { supervisor: { state: 'running' } }] }),
  app.repoState({ runs: [{ supervisor: { state: 'stale' } }, { supervisor: { state: 'draining' } }] }),
  app.repoState({ runs: [{ supervisor: { state: 'Evil Value' } }] }),
];
out.attempts = app.groupAttempts([
  { at: '2026-11-08T10:00:00Z', status: 'running', phase: 'build', attempt: 1 },
  { at: '2026-11-08T10:05:00Z', status: 'running', phase: 'review', attempt: 1 },
  { at: '2026-11-08T10:09:00Z', status: 'failed', phase: 'review', attempt: 1 },
  { at: '2026-11-08T10:20:00Z', status: 'running', phase: 'build', attempt: 2 },
  null, 'x', { phase: '<b>', attempt: 'two' },
]);
console.log(JSON.stringify(out));
"""

DOM = r"""
const CANARY = '<script>alert(1)</script><img src=x onerror=alert(2)>';
class Text { constructor(text) { this.text = String(text); } }
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.className = ''; this.listeners = []; this.handlers = {}; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(type, handler) { this.listeners.push(type); (this.handlers[type] = this.handlers[type] || []).push(handler); }
  replaceChildren(...nodes) { this.children = []; nodes.forEach((n) => this.appendChild(typeof n === 'string' ? new Text(n) : n)); }
  get textContent() { return this.children.map((c) => (c instanceof Text ? c.text : c.textContent)).join(''); }
  set innerHTML(value) { throw new Error('innerHTML was set'); }
  set outerHTML(value) { throw new Error('outerHTML was set'); }
}
global.document = { createElement: (tag) => new El(tag), createTextNode: (text) => new Text(text) };
const app = require(process.argv[1]);
function walk(node, visit) { if (node == null) return; visit(node); if (node instanceof El) node.children.forEach((c) => walk(c, visit)); }
function summary(nodes) {
  const tags = new Set(); const hrefs = []; const attrNames = new Set(); const classes = new Set(); let text = '';
  for (const node of nodes) {
    walk(node, (n) => {
      if (n instanceof Text) { text += n.text; return; }
      tags.add(n.tag); if (n.className) classes.add(n.className);
      for (const k of Object.keys(n.attrs)) { attrNames.add(k); if (k === 'href') hrefs.push(n.attrs[k]); }
    });
  }
  return { tags: [...tags].sort(), hrefs, attrNames: [...attrNames].sort(), classes: [...classes].sort(), text };
}
const out = {};
const NOW = Date.parse('2026-11-08T12:00:30Z');
const job = {
  id: 'fix-a', issues: [1, 'javascript:alert(1)', 2.5], status: CANARY, phase: 'build', active_worker: true, attempt: 2,
  age_seconds: 640.2, status_age_seconds: 1, phase_age_seconds: 310.5, cost_usd: 1.3,
  pr: 'https://evil.example/org/name/pull/7', handoff_status: 'published', reason_code: null, blockers: 2,
  waits: [CANARY], owner: CANARY, next_action: CANARY,
  pr_observed: { handoff_status: 'open', observed_at: '2026-11-08T11:00:00Z', age_seconds: 3600, fresh: false },
};
const active = {
  generated_at: '2026-11-08T12:00:00Z', trigger_paused: true, truncated: true, pr_state: 'recorded',
  history: { segments: 1, bytes: 2048, max_bytes: 67108864, retention_days: 365, truncated: false },
  repos: [
    { repo: 'org/name', supervisor: null, jobs_by_status: { running: 1, [CANARY]: 3 }, runs_total: 1, runs: [
      { run: 'r1', planned: true, started: true, supervisor: { state: 'running' }, budget_usd: 25, planner_cost_usd: 0.4, cost_usd: 3.1, jobs_total: 1, jobs: [job] },
      { run: 'r2', supervisor: { state: CANARY }, budget_usd: null, cost_usd: 2, jobs_total: 0, jobs: [], error: CANARY },
    ] },
    { repo: CANARY, runs: [] },
    { repo: 'org/other', error: 'invalid configuration', runs: [] },
  ],
};
out.active = summary(app.renderActive(active, NOW));
const prJob = Object.assign({}, job, { id: 'fix-b', pr: 'https://github.com/org/name/pull/9', status: 'running' });
active.repos[0].runs[0].jobs = [prJob];
out.activeLinks = summary(app.renderActive(active, NOW)).hrefs;
out.empty = summary(app.renderActive({ repos: [] }, NOW)).text;
const run = { repo: 'org/name', run: 'r-old', issues: [1, 2], outcome: 'merged', purged_at: '2026-10-01T00:00:00Z', purge_reason: CANARY, jobs_total: 2, jobs_truncated: true,
  cost_usd: 4.5, duration_seconds: 7300, last_at: '2026-10-01T00:00:00Z',
  jobs: [{ job: 'fix-a', status: 'merged', outcome: 'merged', pr: 'https://github.com/org/name/pull/7' }, { job: '../evil', status: CANARY, pr: 'javascript:alert(1)' }] };
const handlers = { apply() {}, reset() {}, older() {}, newer() {} };
const filters = app.cleanFilters({ kind: 'runs', repo: 'org/name' });
out.history = summary(app.renderHistory({ runs: [run], ledger: { truncated: true }, next_cursor: '1.000000|org/name|r1' }, filters, { index: 1 }, ['org/name', CANARY], handlers));
out.historyJobs = summary(app.renderHistory({ jobs: [{ repo: 'org/name', run: 'r1', job: 'fix-a', status: 'merged', outcome: 'merged', issues: [3], pr: 'https://github.com/org/name/pull/8', cost_usd: 1, duration_seconds: 60, last_at: '2026-10-01T00:00:00Z' }], next_cursor: null }, app.cleanFilters({ kind: 'jobs' }), { index: 0 }, [], handlers));
out.historyEmpty = summary(app.renderHistory({ runs: [], next_cursor: null }, filters, { index: 0 }, [], handlers)).text;
out.filterInputs = [];
for (const node of app.renderHistory({ runs: [], next_cursor: null }, app.cleanFilters({ kind: 'runs', since: '2026-01-01' }), { index: 0 }, [], handlers)) {
  walk(node, (n) => { if (n instanceof El && n.tag === 'input') out.filterInputs.push(n.attrs); });
}
const detail = {
  source: 'state', repo: 'org/name', run: 'r1', job: 'fix-a', issues: [1], status: 'running', phase: 'review', attempt: 2, retries: 1,
  pr: 'https://github.com/org/name/pull/7', handoff_status: 'published', reason_code: null, commit: 'a'.repeat(40), cost_usd: 2.5,
  age_seconds: 900, phase_seconds: { build: 600, review: 300, [CANARY]: 5 }, next_action: CANARY, waits: [], blockers: 0,
  review: { passed: false, matches_head: false, findings: [CANARY, 'second'], findings_total: 2 },
  phase_events: [
    { at: '2026-11-08T10:00:00Z', status: 'running', phase: 'build', attempt: 1 },
    { at: '2026-11-08T10:10:00Z', status: 'running', phase: 'review', attempt: 1, previous_phase: 'build', previous_phase_seconds: 600 },
  ],
};
out.job = summary(app.renderJob(detail));
out.ledgerJob = summary(app.renderJob({ source: 'ledger', repo: 'org/name', run: 'r1', job: 'fix-a', issues: [1], status: 'merged', phase: 'publish', attempt: 1, pr: 'https://github.com/org/name/pull/7',
  history: { outcome: 'merged', purged: true, purge_reason: 'terminal', duration_seconds: 4000 },
  timeline: [{ at: '2026-11-08T10:00:00Z', status: 'running', phase: 'build', attempt: 1, cost_usd: 1 }] }));
const STATUSES = ['running', 'pr_ready', 'pr_incomplete', 'blocked', 'failed', 'needs_human_publish', 'merged'];
out.chips = STATUSES.map((status) => summary([app.chip(status, status === 'blocked' ? 'needs_review' : null)]));
out.chipOdd = [app.chip(null), app.chip(CANARY, CANARY), app.chip('running', 7)].map((node) => summary([node]));
out.stateKinds = ['empty', 'loading', 'error'].map((kind) => {
  const node = app.stateBlock(kind, 'Some text.');
  return { ...summary([node]), role: node.attrs.role, busy: node.attrs['aria-busy'] || null, className: node.className };
});
out.stateFails = [401, 500].map((status) => summary([app.failure({ status })]).classes);
out.failures = [401, 403, 503, 404, 400, 0, 500].map((status) => summary([app.failure({ status })]).text);
let threw = [];
for (const attempt of [() => app.h('div', { onclick: 'x' }), () => app.h('div', { style: 'x' }), () => app.h('a', { href: 'javascript:alert(1)' }), () => app.h('a', { href: '//evil.example/' }), () => app.h('a', { href: 'https://evil.example/' })]) {
  try { attempt(); threw.push(false); } catch (error) { threw.push(true); }
}
out.threw = threw;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed; the pure functions of app.js are then not exercised")
class ScriptTests(unittest.TestCase):
    """app.js under node: the pure functions, then every renderer on hostile data in a minimal fake DOM."""

    @classmethod
    def run_node(cls, source):
        done = subprocess.run([NODE, "-e", source, str(UI / "app.js")], capture_output=True, text=True, timeout=60, check=False)
        if done.returncode:
            raise AssertionError(done.stderr)
        return json.loads(done.stdout)

    @classmethod
    def setUpClass(cls):
        cls.pure = cls.run_node(PURE)
        cls.dom = cls.run_node(DOM)

    def test_durations_costs_times_and_ages(self):
        self.assertEqual(self.pure["dur"], ["—", "—", "0s", "5s", "1m 00s", "1m 00s", "2m 05s", "1h 00m", "1h 02m", "25h 00m", "2d 7h"])
        self.assertEqual(self.pure["cost"], ["—", "—", "$0.00", "$1.23", "$10.00"])
        self.assertEqual(self.pure["time"], ["2026-11-08 12:30Z", "—", "—", "2026-11-08 10:30Z"])
        self.assertEqual(self.pure["age"], ["—", "5s ago", "1m 10s ago"])
        self.assertEqual(self.pure["since"], [30, None, 0])

    def test_repository_names_and_issue_links(self):
        self.assertEqual(self.pure["repo"], [True, False, False, False, False, False, False, False, False, False])
        self.assertEqual(self.pure["issue"], ["https://github.com/org/name/issues/7", None, None, None, None, None])

    def test_a_pull_request_link_is_rebuilt_from_a_strictly_matched_github_url_only(self):
        good = {"url": "https://github.com/org/name/pull/7", "number": 7}
        self.assertEqual(self.pure["pr"], [good, good, None, None, None, None, None, None, None, None, None])

    def test_a_pasted_token_is_normalized_or_refused(self):
        self.assertEqual(self.pure["token"], ["abcdefgh12345678", "abc.def-ghi_jkl", "abcdefgh", None, None, None, None, None, None, None])

    def test_the_history_query_has_only_valid_values_in_a_fixed_order_encoded(self):
        self.assertEqual(
            self.pure["query"],
            [
                "",
                "?repo=org%2Fname&outcome=merged&since=2026-11-01&until=2026-11-08&limit=50",
                "",
                "?run=r1&limit=200&cursor=1767225600.000000%7Corg%2Fname%7Cr1%7Cfix-a",
                "",
                "?outcome=merged",
            ],
        )

    def test_filters_and_routes_drop_invalid_values(self):
        self.assertEqual(self.pure["filters"][0], {"kind": "runs", "limit": 25})
        self.assertEqual(self.pure["filters"][1], {"kind": "jobs", "limit": 100, "repo": "org/name", "outcome": "closed", "since": "2026-01-01"})
        self.assertEqual(self.pure["filters"][2], {"kind": "runs", "limit": 25})
        active = {"view": "active"}
        job = {"view": "job", "repo": "org/name", "run": "r1", "job": "fix-a"}
        self.assertEqual(
            self.pure["route"],
            [
                active,
                active,
                active,
                active,
                {"view": "history", "filters": {"kind": "runs", "limit": 25}},
                {"view": "history", "filters": {"kind": "jobs", "limit": 50, "repo": "org/name", "outcome": "blocked"}},
                {"view": "history", "filters": {"kind": "runs", "limit": 25, "outcome": "merged"}},
                job,
                active,
                active,
                active,
                active,
            ],
        )
        self.assertEqual(self.pure["roundtrip"], {"view": "history", "filters": {"kind": "jobs", "limit": 100, "repo": "org/name", "outcome": "merged", "since": "2026-01-01", "until": "2026-02-01"}})

    def test_hashes_and_api_paths_come_from_validated_parts(self):
        self.assertEqual(
            self.pure["hash"],
            ["#/job/org/name/r1/fix-a", None, None, "#/history?kind=jobs&repo=org%2Fname&outcome=merged&limit=50", "#/history?kind=runs&limit=25", "#/history?kind=runs&limit=25"],
        )
        self.assertEqual(self.pure["path"], ["/repos/org/name/runs/r1/jobs/fix-a", None, None])

    def test_the_supervisor_state_of_a_repository_is_its_most_active_run(self):
        self.assertEqual(self.pure["state"], ["idle", "idle", "idle", "running", "draining", "idle"])

    def test_attempts_are_grouped_and_junk_events_are_ignored(self):
        first, second, third = self.pure["attempts"]
        self.assertEqual((first["attempt"], first["phases"], first["status"], first["first"], first["last"]), (0, [], None, None, None))
        self.assertEqual((second["attempt"], second["phases"], second["status"]), (1, ["build", "review"], "failed"))
        self.assertEqual((third["attempt"], third["phases"], third["status"]), (2, ["build"], "running"))

    def test_the_date_filters_keep_their_attributes_and_the_set_value(self):
        inputs = {attrs["id"]: attrs for attrs in self.dom["filterInputs"]}
        # f-run (the run filter of the job list) joined the form; it is disabled while the form shows runs
        self.assertEqual(sorted(inputs), ["f-repo", "f-run", "f-since", "f-until"])
        self.assertEqual(inputs["f-since"], {"id": "f-since", "name": "f-since", "type": "date", "value": "2026-01-01"})
        self.assertEqual(inputs["f-until"], {"id": "f-until", "name": "f-until", "type": "date"})
        self.assertEqual(inputs["f-run"]["disabled"], "")
        self.assertNotIn("value", inputs["f-run"])

    def test_hostile_text_is_only_ever_text(self):
        canary = "<script>alert(1)</script><img src=x onerror=alert(2)>"
        for name in ("active", "history", "historyJobs", "job", "ledgerJob"):
            view = self.dom[name]
            with self.subTest(view=name):
                for tag in ("script", "img", "iframe", "style", "svg", "object", "embed", "link", "base", "meta"):
                    self.assertNotIn(tag, view["tags"])
                self.assertFalse([attr for attr in view["attrNames"] if attr.startswith("on") or attr in ("style", "src", "srcdoc")])
                self.assertFalse([cls for cls in view["classes"] if not re.fullmatch(r"[a-z0-9 _-]+", cls)])
        # agent- and user-authored strings are visible as text, never markup
        self.assertIn(canary, self.dom["active"]["text"])
        self.assertIn(canary, self.dom["job"]["text"])
        self.assertIn("alert(1)", self.dom["job"]["text"])

    def test_every_link_is_a_page_hash_or_a_rebuilt_github_url(self):
        allowed = ("#/", "https://github.com/")
        for name in ("active", "history", "historyJobs", "job", "ledgerJob"):
            for href in self.dom[name]["hrefs"]:
                self.assertTrue(href.startswith(allowed), (name, href))
                self.assertNotIn("evil", href)
        self.assertNotIn("https://evil.example/org/name/pull/7", self.dom["active"]["hrefs"])
        self.assertIn("https://github.com/org/name/issues/1", self.dom["active"]["hrefs"])
        self.assertIn("https://github.com/org/name/pull/9", self.dom["activeLinks"])
        self.assertIn("https://github.com/org/name/pull/7", self.dom["job"]["hrefs"])
        self.assertIn("#/job/org/name/r-old/fix-a", self.dom["history"]["hrefs"])
        self.assertFalse([href for href in self.dom["history"]["hrefs"] if "evil" in href or "javascript" in href])

    def test_the_views_show_the_states_the_issue_asks_for(self):
        text = self.dom["active"]["text"]
        for expected in ("Active", "org/name", "Run r1", "$3.10 of $25.00", "no budget set", "2 blockers", "10m 40s", "in phase 5m 11s", "PR open, seen 1h 00m ago (not confirmed by GitHub)", "paused", "cut at its limits", "KiB of 64 MiB"):
            self.assertIn(expected, text)
        self.assertIn("No repository is configured", self.dom["empty"])
        self.assertIn("Nothing matches these filters", self.dom["historyEmpty"])
        job = self.dom["job"]["text"]
        for expected in ("Phase timeline", "Attempts", "Review findings", "No passing review is recorded. 2 findings.", "aaaaaaaaaaaa", "Time spent per phase"):
            self.assertIn(expected, job)
        self.assertIn("the history ledger (the run was purged: terminal", self.dom["ledgerJob"]["text"])
        self.assertIn("meter", self.dom["active"]["tags"])
        self.assertIn("Sign in required", self.dom["failures"][0])
        self.assertIn("no Factory role", self.dom["failures"][1])
        self.assertEqual(len(set(self.dom["failures"])), 6)  # one message per status, one shared by the rest

    def test_every_status_has_a_chip_that_carries_its_text_and_a_reason_code_where_one_exists(self):
        statuses = ["running", "pr_ready", "pr_incomplete", "blocked", "failed", "needs_human_publish", "merged"]
        self.assertEqual(len(self.dom["chips"]), len(statuses))
        for status, chip in zip(statuses, self.dom["chips"]):
            with self.subTest(status=status):
                self.assertEqual(chip["classes"], sorted(["chip-dot", "chip-text", f"chip {status}"] + (["chip-reason mono"] if status == "blocked" else [])))
                self.assertTrue(chip["text"].startswith(status))  # the status is text, not only a colour
                self.assertEqual("needs_review" in chip["text"], status == "blocked")
        # a missing value, hostile text and a non-string reason code are still text (or nothing), never a class or markup
        missing, hostile, numeric = self.dom["chipOdd"]
        self.assertEqual(missing["text"], "—")
        self.assertIn("chip other", missing["classes"])
        self.assertIn("<script>alert(1)</script>", hostile["text"])
        self.assertIn("chip other", hostile["classes"])
        self.assertEqual(numeric["text"], "running")

    def test_empty_loading_and_error_states_share_one_shape_and_say_it_in_text(self):
        empty, loading, error = self.dom["stateKinds"]
        self.assertEqual((empty["className"], loading["className"], error["className"]), ("state empty", "state loading", "state error"))
        self.assertEqual((empty["role"], loading["role"], error["role"]), ("status", "status", "alert"))
        self.assertEqual((empty["busy"], loading["busy"], error["busy"]), (None, "true", None))
        for view, title in ((empty, "Nothing to show"), (loading, "Loading"), (error, "Something went wrong")):
            self.assertTrue(view["text"].startswith(title))
            self.assertIn("Some text.", view["text"])
        for classes in self.dom["stateFails"]:
            self.assertIn("state error", classes)

    def test_the_element_builder_refuses_handlers_styles_and_foreign_links(self):
        self.assertEqual(self.dom["threw"], [True, True, True, True, True])


LOGS = r"""
const out = {};
out.paths = [app.logsPath('org/name', 'r1', null), app.logsPath('org/name', 'r1', 'fix-a'), app.logsPath('org/name', '../r1', 'fix-a'), app.logsPath('org/name', 'r1', '../x'), app.logsPath('org', 'r1', null)];
out.files = [
  app.logFilePath('org/name', 'r1', null, 'plan.log', { tail: 65536 }),
  app.logFilePath('org/name', 'r1', null, 'state.json', {}),
  app.logFilePath('org/name', 'r1', 'fix-a', '1-build-0.jsonl', { offset: 10, limit: 20 }),
  app.logFilePath('org/name', 'r1', 'fix-a', '1-build-0.settings.json', {}),
  app.logFilePath('org/name', 'r1', 'fix-a', '../plan.log', {}),
  app.logFilePath('org/name', 'r1', 'fix-a', '1-build-0.stderr', { offset: -1, limit: 1.5, tail: 'x', before: 7 }),
  app.logFilePath('org/name', 'r1', null, 'planner.jsonl', null),
  app.logFilePath('org/name', 'r1', null, '1-build-0.jsonl', {}),
];
const headers = (o) => ({ get: (n) => (n in o ? o[n] : null) });
out.range = [
  app.logRange(headers({ 'X-Log-Size': '100', 'X-Log-Start': '10', 'X-Log-End': '50' })),
  app.logRange(headers({ 'X-Log-Size': '100', 'X-Log-Start': '60', 'X-Log-End': '50' })),
  app.logRange(headers({ 'X-Log-Size': 'x' })), app.logRange(null),
  app.logRange(headers({ 'X-Log-Size': '10', 'X-Log-Start': '0', 'X-Log-End': '11' })),
];
out.bytes = [null, -1, 0, 1023, 1024, 3 * 1048576].map(app.fmtBytes);
let later = []; let earlier = [];
for (let i = 0; i < 10; i++) { later = app.addChunk(later, { start: i, end: i + 1, text: String(i) }, 'later'); earlier = app.addChunk(earlier, { start: i, end: i + 1, text: String(i) }, 'earlier'); }
out.later = later.map((c) => c.start);
out.earlier = earlier.map((c) => c.start);
out.replace = app.addChunk(later, { start: 5, end: 6, text: 'x' }, 'replace').length;
const none = { earlier() {}, later() {}, tail() {}, close() {} };
out.list = summary(app.renderLogFiles('Job logs', { files: [{ name: '1-build-0.jsonl', size: 2048 }, { name: '1-build-0.settings.json', size: 5 }, { name: CANARY, size: 1 }, null, { name: '../x.jsonl' }] }, false, () => {}));
out.listRun = summary(app.renderLogFiles('Run logs', { files: [{ name: 'plan.log', size: 1 }, { name: '1-build-0.jsonl', size: 1 }] }, true, () => {}));
out.empty = summary(app.renderLogFiles('Job logs', {}, false, () => {}));
out.view = summary(app.renderLogView({ name: 'plan.log', size: 500, chunks: [{ start: 100, end: 200, text: `${CANARY}\nline2` }, { start: 200, end: 300, text: 'x' }] }, none));
out.viewEnd = summary(app.renderLogView({ name: 'a.log', size: 300, chunks: [{ start: 0, end: 300, text: 't' }] }, none));
out.viewEmpty = summary(app.renderLogView({ name: 'a.log', size: 0, chunks: [] }, none));
out.fail = [403, 401, 404, 503, 500, null].map((status) => summary([app.logsFailure(status === null ? null : { status })]).text);
out.failNotice = ((node) => ({ className: node.className, role: node.attrs.role, text: summary([node]).text }))(app.logsFailure({ status: 404 }));
const slim = (found) => ({ query: found.query, total: found.total, matches: found.matches.map((m) => [m.line, m.offset, m.window, m.text]), text: found.text });
const A = { start: 0, end: 15, text: 'alpha\nBeta one' };  // server-shaped: no terminating newline
const B = { start: 100, end: 120, text: 'gamma\n\u00fcn\u00ef beta\nlast' };
out.search = {
  insensitive: slim(app.searchLog([A, B], 'beta')),
  upper: slim(app.searchLog([A, B], 'BETA', { matchCase: false })),
  sensitiveLower: slim(app.searchLog([A, B], 'beta', { matchCase: true })),
  sensitiveUpper: slim(app.searchLog([A, B], 'Beta', { matchCase: true })),
  empty: slim(app.searchLog([A, B], '')),
  notAString: slim(app.searchLog([A, B], null)),
  none: slim(app.searchLog([A, B], 'zzz')),
  noWindows: slim(app.searchLog([], 'x')),
  junk: slim(app.searchLog([null, { text: 5 }, { start: 'x', text: '' }, { text: 'a\nb' }], '')),
  long: app.searchLog([A], 'a'.repeat(500)).query.length,
  blank: slim(app.searchLog([{ start: 0, end: 5, text: 'a\n' }, { start: 5, end: 7, text: 'c' }], '')),
  notes: [app.searchLog([A, B], 'beta'), app.searchLog([A, B], ''), app.searchLog([A, B], 'zzz'), app.searchLog([], 'x'), app.searchLog([], '')].map(app.logSearchNote),
};
let kept = [];
for (let i = 0; i < 10; i++) kept = app.addChunk(kept, { start: i * 10, end: i * 10 + 10, text: `w${i}` }, 'later');
out.search.bounded = [app.searchLog(kept, 'w0').matches.length, app.searchLog(kept, 'w1').matches.length, app.searchLog(kept, 'w9').matches.map((m) => [m.line, m.offset, m.window])];
out.search.shown = [
  app.logShown([A, B], { query: 'beta', filter: true }).visible,
  app.logShown([A, B], { query: 'beta', filter: false }).visible,
  app.logShown([A, B], { query: '', filter: true }).visible,
  app.logShown([A, B], { query: 'zzz', filter: true }).visible,
  app.logShown([A, B], { query: 'Beta', matchCase: true, filter: true }).visible,
];
out.search.wrapClass = [true, false, undefined].map(app.logTextClass);
console.log(JSON.stringify(out));
"""
LOGS_SCRIPT = "".join([DOM.split("const out = {};", 1)[0], LOGS])


LOGS_PANEL = r"""
const SECRET = 'Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD token=ghp_0123456789abcdefghijklmnopqrstuvwxyz';
const HOSTILE = `${CANARY}\n${SECRET}\nplain line\n<img onerror=alert(3)> again`;
const find = (nodes, test) => { const hit = []; for (const n of nodes) walk(n, (x) => { if (x instanceof El && test(x)) hit.push(x); }); return hit; };
const fire = (el, type, event) => el.handlers[type][0](event);
const mkView = (ui) => ({ name: '1-build-0.jsonl', size: 900, ui, chunks: [{ start: 0, end: 400, text: HOSTILE }, { start: 400, end: 900, text: `second ${CANARY}\n${SECRET}` }] });
const mount = (ui, clipboard) => {
  const view = mkView(ui);
  const nodes = app.renderLogView(view, { earlier() {}, later() {}, tail() {}, close() {}, clipboard });
  const results = find(nodes, (x) => x.className === 'logresults')[0];
  return { view, nodes, results, pre: () => find([results], (x) => x.tag === 'pre')[0], button: (label) => find(nodes, (x) => x.tag === 'button' && x.textContent === label)[0] };
};
const textOnly = (nodes) => {
  // every node under the panel is an element or a text node; no attribute value carries markup
  const bad = [];
  for (const n of nodes) walk(n, (x) => { if (x instanceof El) { for (const v of Object.values(x.attrs)) if (/[<>]/.test(v)) bad.push(v); } else if (!(x instanceof Text)) bad.push('other'); });
  return bad;
};
const out = {};
(async () => {
  const active = mount({ query: 'script', filter: true, matchCase: false, wrap: true });
  const pre = active.pre();
  out.filtered = { text: pre.textContent, kids: pre.children.map((c) => c instanceof Text), className: pre.className, bad: textOnly(active.nodes) };
  const secret = mount({ query: 'bearer', filter: true, matchCase: false, wrap: false });
  out.secret = { text: secret.pre().textContent, className: secret.pre().className, bad: textOnly(secret.nodes) };
  // typing, the filter and the case boxes repaint only the results
  const typing = mount({ query: '', filter: false, matchCase: false, wrap: true });
  const search = find(typing.nodes, (x) => x.tag === 'input' && x.attrs.id === 'log-search')[0];
  const [filterBox, caseBox] = find(typing.nodes, (x) => x.tag === 'input' && x.attrs.type === 'checkbox');
  const seen = [];
  const note = () => find([typing.results], (x) => x.attrs.role === 'status' || x.attrs.role === 'alert').map((x) => x.textContent).join('|');
  seen.push(typing.pre().textContent === HOSTILE + '\nsecond ' + CANARY + '\n' + SECRET);
  fire(search, 'input', { target: { value: 'PLAIN' } });
  seen.push(typing.pre().textContent, note());
  out.offset3 = Buffer.byteLength(CANARY) + 1 + Buffer.byteLength(SECRET) + 1;
  fire(filterBox, 'change', { target: { checked: true } });
  seen.push(typing.pre().textContent);
  fire(caseBox, 'change', { target: { checked: true } });
  seen.push(typing.pre().textContent, note());
  fire(search, 'input', { target: { value: 'plain' } });
  seen.push(typing.pre().textContent);
  fire(search, 'input', { target: { value: '' } });
  seen.push(typing.pre().textContent.split('\n').length);
  out.typing = seen;
  out.typingBad = textOnly(typing.nodes);
  // the wrap toggle changes the class of the <pre> and the pressed state of its button, nothing else
  const wrapped = mount({ query: 'plain', filter: true, matchCase: false, wrap: true });
  const before = { text: wrapped.pre().textContent, attrs: { ...wrapped.pre().attrs } };
  const pressed = [];
  const target = { setAttribute(k, v) { pressed.push([k, v]); } };
  const toggle = wrapped.button('Wrap long lines');
  fire(toggle, 'click', { target });
  const mid = wrapped.pre().className;
  fire(toggle, 'click', { target });
  out.wrap = { pressedInitially: toggle.attrs['aria-pressed'], mid, after: wrapped.pre().className, pressed, sameText: wrapped.pre().textContent === before.text, sameAttrs: JSON.stringify(wrapped.pre().attrs) === JSON.stringify(before.attrs) };
  // copy takes exactly the shown text
  const written = [];
  const clip = { writeText: (text) => { written.push(text); return Promise.resolve(); } };
  const copying = mount({ query: 'plain', filter: true, matchCase: false, wrap: true }, clip);
  await fire(copying.button('Copy visible range'), 'click', {});
  const filteredCopy = [written.slice(), copying.pre().textContent];
  const whole = mount({ query: 'plain', filter: false, matchCase: false, wrap: true }, clip);
  await fire(whole.button('Copy visible range'), 'click', {});
  const none = mount({ query: 'nothing-like-this', filter: true, matchCase: false, wrap: true }, clip);
  await fire(none.button('Copy visible range'), 'click', {});
  const refused = mount({ query: '', filter: false, matchCase: false, wrap: true }, { writeText: () => Promise.reject(new Error('denied: ' + SECRET)) });
  await fire(refused.button('Copy visible range'), 'click', {});
  const thrown = mount({ query: '', filter: false, matchCase: false, wrap: true }, { writeText: () => { throw new Error('sync'); } });
  await fire(thrown.button('Copy visible range'), 'click', {});
  const missing = mount({ query: '', filter: false, matchCase: false, wrap: true }, {});
  await fire(missing.button('Copy visible range'), 'click', {});
  const textOfNote = (m) => find(m.nodes, (x) => x.tag === 'span' && x.attrs.role === 'status').map((x) => x.textContent).join('');
  out.copy = {
    filtered: filteredCopy,
    filteredNote: textOfNote(copying),
    wholeWritten: written[1] === whole.pre().textContent,
    wholeNote: textOfNote(whole),
    nothingWritten: written.length,
    noneNote: textOfNote(none),
    refused: textOfNote(refused),
    thrown: textOfNote(thrown),
    missing: textOfNote(missing),
    direct: [await app.copyLogText('x', clip), await app.copyLogText('', clip), await app.copyLogText('x', null)],
  };
  console.log(JSON.stringify(out));
})();
"""
LOGS_PANEL_SCRIPT = "".join([DOM.split("const out = {};", 1)[0], LOGS_PANEL])


@unittest.skipUnless(NODE, "node is not installed; the log panel of app.js is then not exercised")
class LogPanelTests(unittest.TestCase):
    """The pure parts of the Logs panel: paths from the server's allow-list, ranges, and hostile text shown as text."""

    @classmethod
    def setUpClass(cls):
        cls.out = ScriptTests.run_node(LOGS_SCRIPT)

    def test_paths_are_built_only_from_valid_names(self):
        run, job = "/repos/org/name/runs/r1", "/repos/org/name/runs/r1/jobs/fix-a"
        self.assertEqual(self.out["paths"], [f"{run}/logs", f"{job}/logs", None, None, None])
        self.assertEqual(
            self.out["files"],
            [f"{run}/logs/plan.log?tail=65536", None, f"{job}/logs/1-build-0.jsonl?offset=10&limit=20", None, None, f"{job}/logs/1-build-0.stderr?before=7", f"{run}/logs/planner.jsonl", None],
        )

    def test_the_range_comes_from_the_headers_or_not_at_all(self):
        self.assertEqual(self.out["range"], [{"size": 100, "start": 10, "end": 50}, None, None, None, None])
        self.assertEqual(self.out["bytes"], ["—", "—", "0 B", "1023 B", "1.0 KiB", "3.0 MiB"])

    def test_loaded_windows_are_bounded_on_the_far_side(self):
        self.assertEqual(self.out["later"], [2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(self.out["earlier"], [9, 8, 7, 6, 5, 4, 3, 2])
        self.assertEqual(self.out["replace"], 1)

    def test_a_listing_keeps_only_allow_listed_names_and_makes_no_link(self):
        listing = self.out["list"]
        self.assertIn("1-build-0.jsonl", listing["text"])
        self.assertIn("2.0 KiB", listing["text"])
        for refused in ("settings", "script", "../x"):
            self.assertNotIn(refused, listing["text"])
        self.assertEqual((listing["hrefs"], listing["tags"]), ([], ["button", "h4", "li", "ul"]))
        self.assertNotIn("1-build-0.jsonl", self.out["listRun"]["text"])
        self.assertIn("plan.log", self.out["listRun"]["text"])
        self.assertIn("No log files yet", self.out["empty"]["text"])

    def test_log_text_is_one_text_node_in_a_pre(self):
        view = self.out["view"]
        self.assertIn("<script>alert(1)</script><img src=x onerror=alert(2)>", view["text"])
        self.assertEqual(view["tags"], ["button", "div", "h4", "input", "label", "p", "pre", "span"])
        self.assertEqual(view["hrefs"], [])
        self.assertLessEqual(
            set(view["attrNames"]),
            {"aria-label", "aria-pressed", "autocomplete", "id", "maxlength", "name", "placeholder", "role", "spellcheck", "tabindex", "type", "value"},
        )
        for expected in ("Showing bytes 100 to 300 of 500", "Load earlier", "Load later", "Show the end again", "Close"):
            self.assertIn(expected, view["text"])
        end = self.out["viewEnd"]["text"]
        self.assertNotIn("Load earlier", end)
        self.assertNotIn("Load later", end)
        self.assertIn("Nothing to show", self.out["viewEmpty"]["text"])

    def test_a_viewer_gets_a_clear_403_state(self):
        forbidden, unsigned, missing, busy, other, unknown = self.out["fail"]
        self.assertIn("operators only", forbidden)
        self.assertIn("Sign in required", unsigned)
        self.assertIn("removed with the run", missing)
        self.assertIn("busy", busy)
        self.assertEqual(other, unknown)

    def test_a_purged_run_or_job_still_shows_the_removed_with_the_run_notice(self):
        notice = self.out["failNotice"]
        self.assertEqual((notice["className"], notice["role"]), ("state empty", "status"))
        self.assertIn("removed with the run", notice["text"])
        script = (UI / "app.js").read_text(encoding="utf-8")
        panel = script[script.index("function showLogs(current)"):]
        panel = panel[: panel.index("function loadView")]
        # the listing (job and run) and every file read (in the panels) end in the same notice, so a 404 on either reaches it
        self.assertEqual(panel.count("logsFailure(error)"), 1)
        self.assertIn("api(logsPath(current.repo, current.run, current.job))", panel)
        self.assertIn("api(path, { text: true, signal })", panel)
        panels = script[script.index("function createLogPanels(options)"):]
        panels = panels[: panels.index("The page (browser only)")]
        self.assertEqual(panels.count("logsFailure(error)"), 1)

    def test_search_matches_only_inside_the_loaded_windows_with_line_and_byte_positions(self):
        found = self.out["search"]
        # window A is bytes 0..15 and window B starts at byte 100; "\u00fc" and "\u00ef" are two bytes each
        beta = {"query": "beta", "total": 5, "matches": [[2, 6, 0, "Beta one"], [4, 106, 1, "\u00fcn\u00ef beta"]], "text": "Beta one\n\u00fcn\u00ef beta"}
        self.assertEqual(found["insensitive"], beta)
        self.assertEqual(found["upper"], {**beta, "query": "BETA"})
        self.assertEqual(found["sensitiveLower"]["matches"], [[4, 106, 1, "\u00fcn\u00ef beta"]])
        self.assertEqual(found["sensitiveUpper"]["matches"], [[2, 6, 0, "Beta one"]])
        # A window's text has no terminating newline: a trailing newline is a real blank last line (windows of "a\n\nc").
        self.assertEqual(found["blank"]["matches"], [[1, 0, 0, "a"], [2, 2, 0, ""], [3, 5, 1, "c"]])
        self.assertEqual(found["blank"]["total"], 3)
        # a window that ended with a newline adds no blank line at the join, and the last line needs no newline
        everything = [[1, 0, 0, "alpha"], [2, 6, 0, "Beta one"], [3, 100, 1, "gamma"], [4, 106, 1, "\u00fcn\u00ef beta"], [5, 117, 1, "last"]]
        self.assertEqual(found["empty"]["matches"], everything)
        self.assertEqual(found["notAString"]["matches"], everything)
        self.assertEqual(found["empty"]["total"], 5)
        self.assertEqual(found["none"], {"query": "zzz", "total": 5, "matches": [], "text": ""})
        self.assertEqual(found["noWindows"], {"query": "x", "total": 0, "matches": [], "text": ""})
        self.assertEqual(found["junk"]["matches"], [[1, 0, 1, "5"], [2, 0, 3, "a"], [3, 2, 3, "b"]])
        self.assertEqual(found["long"], 200)
        # a window dropped by the LOG_KEEP bound is no longer searched
        self.assertEqual(found["bounded"], [0, 0, [[8, 90, 7]]])

    def test_the_search_says_what_it_found_or_that_nothing_matched(self):
        notes = self.out["search"]["notes"]
        self.assertEqual(notes[0], "2 of 5 lines match, at line 2 (byte 6), 4 (byte 106).")
        self.assertEqual(notes[1], "5 lines loaded.")
        self.assertIn("No match in the loaded range", notes[2])
        self.assertIn("load earlier or later", notes[2])
        self.assertEqual(notes[3], "No lines are loaded to search.")
        self.assertEqual(notes[4], "No lines loaded.")

    def test_the_filter_shows_only_the_matching_lines_and_an_empty_query_everything(self):
        self.assertEqual(
            self.out["search"]["shown"],
            ["Beta one\n\u00fcn\u00ef beta", "alpha\nBeta one\ngamma\n\u00fcn\u00ef beta\nlast", "alpha\nBeta one\ngamma\n\u00fcn\u00ef beta\nlast", "", "Beta one"],
        )
        self.assertEqual(self.out["search"]["wrapClass"], ["logtext wrap", "logtext", "logtext wrap"])

    def test_the_panel_asks_for_logs_through_the_one_fetch_and_never_renders_markup(self):
        script = (UI / "app.js").read_text(encoding="utf-8")
        self.assertEqual(script.count("window.fetch("), 2)  # the API call (logs included) and the public config route
        self.assertIn("asText ? 'text/plain' : 'application/json'", script)
        self.assertIn("response.text()", script)
        self.assertIsNone(re.search(r"([A-Za-z_][\w.]*) && \1[.\[]", script), "an a && a.b chain: use optional chaining")
        # wrapping is a class on the <pre>; copying goes through the clipboard API with fixed sentences
        self.assertIn("pre.className = `${logTextClass(ui.wrap)}", script)
        self.assertIn("navigator.clipboard", script)
        self.assertIn("clipboard.writeText(text)", script)
        self.assertIn("white-space: pre-wrap", (UI / "style.css").read_text(encoding="utf-8"))
        self.assertIn(".logtext.wrap", (UI / "style.css").read_text(encoding="utf-8"))


@unittest.skipUnless(NODE, "node is not installed; the log panel controls of app.js are then not exercised")
class LogPanelControlsTests(unittest.TestCase):
    """The panel renderer with search, filter, wrap and copy active, on hostile and secret-shaped text, in a fake DOM that throws on innerHTML."""

    @classmethod
    def setUpClass(cls):
        cls.out = ScriptTests.run_node(LOGS_PANEL_SCRIPT)

    def test_hostile_and_secret_shaped_text_reaches_the_dom_only_as_text_nodes(self):
        markup = "<script>alert(1)</script><img src=x onerror=alert(2)>"
        secret = "Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD token=ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        filtered = self.out["filtered"]
        self.assertEqual(filtered["text"], f"{markup}\nsecond {markup}")
        self.assertEqual(filtered["kids"], [True])  # one text node, nothing else, inside the <pre>
        self.assertEqual(filtered["className"], "logtext wrap")
        self.assertEqual(self.out["secret"], {"text": f"{secret}\n{secret}", "className": "logtext", "bad": []})
        self.assertEqual(filtered["bad"], [])
        self.assertEqual(self.out["typingBad"], [])

    def test_typing_and_the_filter_and_case_boxes_repaint_the_results_only(self):
        whole = self.out["typing"][1]
        self.assertTrue(self.out["typing"][0])
        self.assertIn("plain line", whole)
        self.assertIn(f"1 of 6 lines match, at line 3 (byte {self.out['offset3']})", self.out["typing"][2])
        self.assertEqual(self.out["typing"][3], "plain line")  # the filter is on, matching ignores case
        self.assertEqual(self.out["typing"][4], "")  # match case: "PLAIN" matches nothing
        self.assertIn("Nothing to show", self.out["typing"][5])
        self.assertIn("No match in the loaded range", self.out["typing"][5])
        self.assertEqual(self.out["typing"][6], "plain line")
        self.assertEqual(self.out["typing"][7], 6)  # an empty query shows every line again

    def test_the_wrap_toggle_changes_only_the_class_of_the_pre(self):
        wrap = self.out["wrap"]
        self.assertEqual((wrap["pressedInitially"], wrap["mid"], wrap["after"]), ("true", "logtext", "logtext wrap"))
        self.assertEqual(wrap["pressed"], [["aria-pressed", "false"], ["aria-pressed", "true"]])
        self.assertTrue(wrap["sameText"])
        self.assertTrue(wrap["sameAttrs"])

    def test_copy_takes_exactly_the_visible_text_and_answers_in_fixed_words(self):
        copy = self.out["copy"]
        self.assertEqual(copy["filtered"], [["plain line"], "plain line"])
        self.assertEqual(copy["filteredNote"], "Copied the visible text.")
        self.assertTrue(copy["wholeWritten"])
        self.assertEqual(copy["wholeNote"], "Copied the visible text.")
        self.assertEqual(copy["nothingWritten"], 2)  # the empty filtered view wrote nothing
        self.assertEqual(copy["noneNote"], "Nothing to copy.")
        failed = "Copy failed: the browser did not allow it."
        self.assertEqual((copy["refused"], copy["thrown"], copy["missing"]), (failed, failed, failed))
        self.assertEqual(copy["direct"], ["Copied the visible text.", "Nothing to copy.", failed])
        self.assertNotIn("sk-ant", json.dumps(copy["refused"]))


TRANSCRIPT = r"""
const find = (nodes, test) => { const hit = []; for (const n of nodes) walk(n, (x) => { if (x instanceof El && test(x)) hit.push(x); }); return hit; };
const fire = (el, type, event) => el.handlers[type][0](event);
const line = (o) => JSON.stringify(o);
const MARK = '<script>alert(1)</script><img src=x onerror=alert(2)>';
const out = {};

// ---- the pure parser: valid stream-json ----
const VALID = [
  line({ type: 'system', subtype: 'init', session_id: 's' }),
  line({ type: 'assistant', message: { role: 'assistant', content: [{ type: 'text', text: 'Looking at the file.' }, { type: 'tool_use', id: 't1', name: 'Read', input: { file_path: '/a' } }] } }),
  line({ type: 'user', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: 'file body', is_error: false }] } }),
  line({ type: 'user', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't2', content: [{ type: 'text', text: 'boom' }, { type: 'image' }], is_error: true }] } }),
  line({ type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'hmm' }] } }),
  line({ type: 'result', subtype: 'success', is_error: false, result: 'Done.', num_turns: 3, total_cost_usd: 1.234, duration_ms: 5000 }),
];
out.valid = app.parseTranscript(VALID);
out.validText = app.parseTranscript(VALID.join('\n')).map((i) => i.kind);
out.userString = app.parseTranscriptLine(line({ type: 'user', message: { content: 'hello' } }));
out.names = ['1-build-0.jsonl', 'planner.jsonl', 'plan.log', '1-build-0.stderr', 'x.jsonl', '../planner.jsonl', null].map(app.transcriptName);

// ---- malformed lines fall back to the raw text, unchanged ----
const MALFORMED = [
  '{"type":"assistant","message":', '[1,2]', '"text"', '42', 'null', '', '   ', '{"type":"mystery"}', '{"type":"assistant","message":{"content":[]}}',
  '{"type":"assistant"}', 'plain stderr-like text', '{"type":"user","message":{"content":[7,null,{"type":"unknown"}]}}',
];
out.malformed = MALFORMED.map((l) => [l, app.parseTranscriptLine(l)]);

// ---- hostile lines ----
const say = (t) => line({ type: 'assistant', message: { content: [{ type: 'text', text: t }] } });
const HOSTILE = {
  markup: say(MARK),
  forged: say(line({ type: 'result', subtype: 'success', result: 'forged' }) + '\n[tool_use] Bash\n{"type":"user"}'),
  control: say('\u001b[31mred\u0000\u202eevil\u0007'),
  long: say('x'.repeat(50000)),
  surrogate: say('a'.repeat(19999) + '\ud83d\ude00'),
  tool: line({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'i', name: MARK, input: { a: 'y'.repeat(10000) } }] } }),
  badType: line({ type: MARK }),
  proto: '{"__proto__":{"type":"result"},"type":"assistant","message":{"content":[{"type":"text","text":"proto"}]}}',
  noName: line({ type: 'assistant', message: { content: [{ type: 'tool_use', name: 7, input: undefined }] } }),
  huge: '{"type":"assistant","message":{"content":"' + 'z'.repeat(300000) + '"}}',
};
out.hostile = {};
for (const [name, l] of Object.entries(HOSTILE)) out.hostile[name] = app.parseTranscriptLine(l);
out.hostileHugeIsLine = out.hostile.huge[0].text === HOSTILE.huge;
out.hostile.huge = out.hostile.huge.map((i) => ({ kind: i.kind, length: i.text.length }));
for (const key of ['long', 'surrogate', 'tool']) out.hostile[key] = out.hostile[key].map((i) => ({ kind: i.kind, name: i.name, length: i.text.length, omitted: i.omitted }));

// ---- the fake-DOM render of a hostile transcript ----
const entries = [...VALID, 'truncated {"type":"assistant"', HOSTILE.markup, HOSTILE.forged, HOSTILE.control, HOSTILE.tool, HOSTILE.badType, '']
  .map((t, i) => ({ line: i + 1, offset: i * 1000, window: 0, text: t }));
const box = app.renderTranscript(entries, {}, { class: 'logtext transcript', tabindex: '0' });
const sum = summary([box]);
const bad = [];
walk(box, (x) => { if (x instanceof El) { for (const v of Object.values(x.attrs)) if (/[<>]/.test(v)) bad.push(v); } else if (!(x instanceof Text)) bad.push('other'); });
out.render = {
  tags: sum.tags, attrNames: sum.attrNames, hrefs: sum.hrefs, bad, text: sum.text,
  boxClass: box.className,
  folds: find([box], (x) => x.tag === 'details').map((x) => [x.className, x.attrs.open === undefined, x.listeners]),
  summaries: find([box], (x) => x.tag === 'summary').map((x) => x.textContent),
  raws: find([box], (x) => x.className === 'turn turn-raw').map((x) => x.textContent),
  result: find([box], (x) => x.className === 'turn turn-result').map((x) => x.textContent),
  childKinds: find([box], (x) => x.tag === 'details').map((x) => x.children.map((c) => c.tag)),
};
out.noTurns = [app.renderTranscript(MALFORMED.map((t, i) => ({ offset: i, text: t })), {}, {}), app.renderTranscript([], {}, {}), app.renderTranscript(null, null, {})];

// ---- the panel ----
const CHUNK = { start: 0, end: 5000, text: VALID.join('\n') };
const mount = (name, ui, chunks) => {
  const view = { name, size: 5000, ui, chunks: chunks || [CHUNK] };
  const nodes = app.renderLogView(view, { earlier() {}, later() {}, tail() {}, close() {} });
  const results = find(nodes, (x) => x.className === 'logresults')[0];
  const state = () => ({
    pre: find([results], (x) => x.tag === 'pre').map((x) => [x.className, x.textContent]),
    box: find([results], (x) => x.tag === 'div' && x.className.indexOf('transcript') >= 0).map((x) => x.className),
    folds: find([results], (x) => x.tag === 'details'),
  });
  const buttons = (label) => find(nodes, (x) => x.tag === 'button' && x.textContent === label);
  const search = find(nodes, (x) => x.tag === 'input' && x.attrs.id === 'log-search')[0];
  return { nodes, results, state, buttons, search };
};
const fresh = () => ({ query: '', filter: false, matchCase: false, wrap: true });
const jsonl = mount('1-build-0.jsonl', fresh());
const first = jsonl.state();
out.panel = { pre: first.pre, box: first.box, folds: first.folds.length, rawButtons: jsonl.buttons('Raw lines').length, note: summary(jsonl.results.children).text.slice(0, 120) };
// a fold the reader opened stays open when the panel is painted again
fire(first.folds[0], 'toggle', { target: { open: true } });
fire(jsonl.search, 'input', { target: { value: '' } });
out.kept = jsonl.state().folds.map((x) => (x.attrs.open === undefined ? 'closed' : 'open'));
fire(jsonl.state().folds[0], 'toggle', { target: { open: false } });
fire(jsonl.search, 'input', { target: { value: '' } });
out.closedAgain = jsonl.state().folds.map((x) => (x.attrs.open === undefined ? 'closed' : 'open'));
// Raw lines shows the plain text, and back
const pressed = [];
const target = { setAttribute(k, v) { pressed.push([k, v]); } };
fire(jsonl.buttons('Raw lines')[0], 'click', { target });
out.rawView = jsonl.state();
out.rawView.folds = out.rawView.folds.length;
fire(jsonl.buttons('Raw lines')[0], 'click', { target });
out.backView = jsonl.state().box;
out.pressed = pressed.slice();
// wrap keeps the transcript class
fire(jsonl.buttons('Wrap long lines')[0], 'click', { target });
out.wrapOff = jsonl.state().box;
fire(jsonl.buttons('Wrap long lines')[0], 'click', { target });
out.wrapOn = jsonl.state().box;
// the filter shows the turns of the matching lines only
const filtered = mount('1-build-0.jsonl', { query: 'hmm', filter: true, matchCase: false, wrap: true });
out.filtered = filtered.state().folds.map((x) => x.children[0].textContent);
// planner.jsonl is a transcript too
out.planner = mount('planner.jsonl', fresh()).state().box.length;
// plain logs stay plain text, even when a line is JSON
out.plain = ['plan.log', 'supervisor.log', '1-build-0.stderr', 'planner.stderr'].map((name) => { const m = mount(name, fresh()); const s = m.state(); return [name, s.pre.map((p) => p[0]), s.pre[0][1] === CHUNK.text, s.box.length, m.buttons('Raw lines').length]; });
// a .jsonl with nothing to parse is the plain text as before
const none = mount('1-build-0.jsonl', fresh(), [{ start: 0, end: 20, text: 'not json\n{"torn":' }]).state();
out.noneParsed = [none.pre, none.box];
console.log(JSON.stringify(out));
"""
TRANSCRIPT_SCRIPT = "".join([DOM.split("const out = {};", 1)[0], TRANSCRIPT])


@unittest.skipUnless(NODE, "node is not installed; the transcript parser of app.js is then not exercised")
class TranscriptTests(unittest.TestCase):
    """The pure transcript parser on valid, malformed and hostile lines, and its render in a fake DOM that throws on innerHTML."""

    MARK = "<script>alert(1)</script><img src=x onerror=alert(2)>"

    @classmethod
    def setUpClass(cls):
        cls.out = ScriptTests.run_node(TRANSCRIPT_SCRIPT)

    def test_valid_stream_json_gives_turns_of_the_right_kinds(self):
        valid = self.out["valid"]
        self.assertEqual([item["kind"] for item in valid], ["system", "text", "tool_use", "tool_result", "tool_result", "thinking", "result"])
        self.assertEqual([item["line"] for item in valid], [1, 2, 2, 3, 4, 5, 6])
        self.assertEqual(self.out["validText"], [item["kind"] for item in valid])  # a whole text is split into lines
        self.assertEqual(valid[0]["text"], "init")
        self.assertEqual(valid[1], {"kind": "text", "role": "assistant", "text": "Looking at the file.", "omitted": 0, "line": 2})
        self.assertEqual(valid[2], {"kind": "tool_use", "name": "Read", "id": "t1", "text": '{\n  "file_path": "/a"\n}', "omitted": 0, "line": 2})
        self.assertEqual(valid[3], {"kind": "tool_result", "id": "t1", "error": False, "text": "file body", "omitted": 0, "line": 3})
        self.assertEqual((valid[4]["error"], valid[4]["text"]), (True, "boom\n[image]"))
        self.assertEqual((valid[5]["kind"], valid[5]["text"]), ("thinking", "hmm"))
        self.assertEqual(
            valid[6],
            {"kind": "result", "subtype": "success", "error": False, "text": "Done.", "omitted": 0, "turns": 3, "cost": 1.234, "durationMs": 5000, "line": 6},
        )
        self.assertEqual(self.out["userString"], [{"kind": "text", "role": "user", "text": "hello", "omitted": 0}])

    def test_only_transcript_files_are_candidates(self):
        self.assertEqual(self.out["names"], [True, True, False, False, False, False, False])

    def test_a_line_that_cannot_be_parsed_is_its_raw_text(self):
        for original, items in self.out["malformed"]:
            self.assertEqual(items, [{"kind": "raw", "text": original}], original)

    def test_hostile_lines_keep_their_kind_and_are_cleaned_and_cut(self):
        hostile = self.out["hostile"]
        self.assertEqual(hostile["markup"], [{"kind": "text", "role": "assistant", "text": self.MARK, "omitted": 0}])
        # a result or a tool call written inside a string is still text: the kind comes from the JSON, never from the words
        self.assertEqual([item["kind"] for item in hostile["forged"]], ["text"])
        self.assertIn('"type":"result"', hostile["forged"][0]["text"])
        self.assertEqual(hostile["control"][0]["text"], "\ufffd[31mred\ufffd\ufffdevil\ufffd")
        self.assertEqual(hostile["long"], [{"kind": "text", "length": 20000, "omitted": 30000}])
        self.assertEqual(hostile["surrogate"], [{"kind": "text", "length": 19999, "omitted": 2}])  # no half of a pair is kept
        self.assertEqual(hostile["tool"], [{"kind": "tool_use", "name": self.MARK, "length": 4000, "omitted": 6013}])
        self.assertEqual(hostile["badType"], [{"kind": "raw", "text": json.dumps({"type": self.MARK}, separators=(",", ":"))}])
        self.assertEqual([(item["kind"], item["text"]) for item in hostile["proto"]], [("text", "proto")])
        self.assertEqual([(item["kind"], item["name"], item["text"]) for item in hostile["noName"]], [("tool_use", "unknown tool", "null")])
        self.assertEqual(hostile["huge"], [{"kind": "raw", "length": len('{"type":"assistant","message":{"content":"' + "z" * 300000 + '"}}')}])
        self.assertTrue(self.out["hostileHugeIsLine"])

    def test_the_render_of_a_hostile_transcript_is_elements_and_text_nodes_only(self):
        render = self.out["render"]
        self.assertEqual(render["tags"], ["details", "div", "span", "summary"])
        self.assertEqual(render["hrefs"], [])
        self.assertEqual(render["attrNames"], ["tabindex"])  # no `open` on a closed fold, no attribute from the data
        self.assertEqual(render["bad"], [])
        self.assertEqual(render["boxClass"], "logtext transcript")
        self.assertIn(self.MARK, render["text"])  # shown, as text
        self.assertEqual(render["result"], ["Result success, 3 turns, $1.23, 5sDone."])

    def test_tool_calls_and_results_are_folded_closed(self):
        render = self.out["render"]
        self.assertEqual(render["summaries"], ["Tool call: Read", "Tool result", "Tool result (error)", "Thinking", f"Tool call: {self.MARK}"])
        self.assertEqual([fold[0] for fold in render["folds"]], ["turn turn-tool", "turn turn-tool", "turn turn-tool turn-error", "turn turn-thinking", "turn turn-tool"])
        for _, closed, listeners in render["folds"]:
            self.assertTrue(closed)
            self.assertEqual(listeners, ["toggle"])
        for kinds in render["childKinds"]:
            self.assertEqual(kinds, ["summary", "div"])

    def test_a_malformed_line_is_shown_as_its_raw_text(self):
        raws = self.out["render"]["raws"]
        self.assertEqual(raws, ['truncated {"type":"assistant"', json.dumps({"type": self.MARK}, separators=(",", ":"))])  # the empty line adds nothing
        self.assertEqual(self.out["noTurns"], [None, None, None])  # no turn at all: the caller shows the raw text

    def test_the_panel_shows_turns_with_raw_lines_one_click_away(self):
        panel = self.out["panel"]
        self.assertEqual((panel["pre"], panel["box"], panel["folds"], panel["rawButtons"]), ([], ["logtext wrap transcript"], 4, 1))
        self.assertIn("tool calls and results are folded", panel["note"])
        raw = self.out["rawView"]
        self.assertEqual((raw["box"], raw["folds"]), ([], 0))
        self.assertEqual([entry[0] for entry in raw["pre"]], ["logtext wrap"])
        self.assertEqual(raw["pre"][0][1].count("\n"), 5)  # the lines as the server sent them
        self.assertEqual(self.out["backView"], ["logtext wrap transcript"])
        self.assertEqual(self.out["pressed"], [["aria-pressed", "true"], ["aria-pressed", "false"]])
        self.assertEqual(self.out["wrapOff"], ["logtext transcript"])
        self.assertEqual(self.out["wrapOn"], ["logtext wrap transcript"])

    def test_a_fold_the_reader_opened_stays_open_when_the_panel_is_painted_again(self):
        self.assertEqual(self.out["kept"], ["open", "closed", "closed", "closed"])
        self.assertEqual(self.out["closedAgain"], ["closed"] * 4)

    def test_search_and_filter_still_work_on_the_lines_of_a_transcript(self):
        self.assertEqual(self.out["filtered"], ["Thinking"])
        self.assertEqual(self.out["planner"], 1)

    def test_logs_that_are_not_transcripts_stay_plain_text(self):
        for name, classes, same_text, boxes, raw_buttons in self.out["plain"]:
            self.assertEqual((classes, same_text, boxes, raw_buttons), (["logtext wrap"], True, 0, 0), name)
        pre, box = self.out["noneParsed"]
        self.assertEqual((pre, box), ([["logtext wrap", 'not json\n{"torn":']], []))


FOLLOW = r"""
const find = (root, test) => { const hit = []; walk(root, (x) => { if (x instanceof El && test(x)) hit.push(x); }); return hit; };
const flush = () => new Promise((resolve) => setImmediate(resolve));
const ans = (text, start, end, size) => ({ text, range: { size, start, end }, more: { before: start > 0, after: end < size } });
const headers = (o) => ({ get: (n) => (n in o ? o[n] : null) });
const out = {};

// ---- pure helpers ----
const at = (o) => ({ scrollTop: o[0], scrollHeight: o[1], clientHeight: o[2] });
out.bottom = [at([900, 1000, 100]), at([897, 1000, 100]), at([500, 1000, 100]), {}, null].map(app.isAtBottom);
const D = (input) => { const d = app.followDecision(input); return [d.poll, d.state]; };
out.decide = {
  following: D({ follow: true, hidden: false, atBottom: true }),
  hidden: D({ follow: true, hidden: true, atBottom: true }),
  scrolled: D({ follow: true, hidden: false, atBottom: false }),
  resumedShown: D({ follow: true, hidden: false, atBottom: true }),
  resumedBottom: D({ follow: true, hidden: false, atBottom: true }),
  hiddenAndScrolled: D({ follow: true, hidden: true, atBottom: false }),
  off: D({ follow: false, hidden: false, atBottom: true }),
  junk: [D(null), D({}), D({ follow: 'yes', hidden: false, atBottom: true })],
  notes: ['following', 'hidden', 'scrolled'].map((state) => app.followDecision({ follow: true, hidden: state === 'hidden', atBottom: state !== 'scrolled' }).note),
};
out.query = [app.nextFollowQuery([]), app.nextFollowQuery(null), app.nextFollowQuery([{ start: 0, end: 10 }, { start: 10, end: 1234 }]), app.nextFollowQuery([{ end: -1 }]), app.nextFollowQuery([{ end: 1.5 }])];
out.followPath = [
  app.logFilePath('org/name', 'r1', null, 'plan.log', app.nextFollowQuery([{ start: 0, end: 1234 }])),
  app.logFilePath('org/name', 'r1', 'fix-a', '1-build-0.jsonl', app.nextFollowQuery([{ start: 0, end: 77 }])),
  app.logFilePath('org/name', 'r1', 'fix-a', '1-build-0.jsonl', app.nextFollowQuery([])),
];
const A = { start: 0, end: 10, size: 10, text: 'a', more: { before: false, after: false } };
const grown = app.followChunks([A], { start: 10, end: 20, size: 20, text: 'b', more: { before: true, after: false } });
const idle = app.followChunks([A], { start: 10, end: 10, size: 10, text: '', more: { before: false, after: false } });
out.chunks = {
  grown: [grown.chunks.map((c) => [c.start, c.end]), grown.grew],
  idle: [idle.chunks.map((c) => [c.start, c.end]), idle.grew],
  shrank: [app.followChunks([A], { start: 4, end: 4, size: 4, text: '', more: {} }), app.followChunks([A], { start: 0, end: 5, size: 5, text: 'x', more: {} })],
  first: app.followChunks([], { start: 0, end: 3, size: 3, text: 'abc', more: {} }).grew,
};
// small follow answers that continue the last window are merged into it, so the loaded tail is never pushed out
let kept = [{ start: 0, end: 10, size: 10, text: 'tail', more: { before: false, after: true } }];
for (let i = 0; i < 12; i++) kept = (app.followChunks(kept, { start: 10 + i, end: 11 + i, size: 11 + i, text: `n${i}`, more: { before: true, after: false } }) || { chunks: [] }).chunks;
out.chunks.merged = { starts: kept.map((c) => [c.start, c.end, c.size]), text: kept.map((c) => c.text), more: kept.map((c) => c.more) };
// a window that spans the byte budget is not merged into: a new one starts and only then does the far one go
let bounded = [];
for (let i = 0; i < 12; i++) bounded = (app.followChunks(bounded, { start: i * 65536, end: (i + 1) * 65536, size: (i + 1) * 65536, text: String(i), more: {} }) || { chunks: [] }).chunks;
out.chunks.bounded = bounded.map((c) => c.start / 65536);
let idles = [];
for (let i = 0; i < 20; i++) idles = (app.followChunks(idles.length ? idles : [A], { start: 10, end: 10, size: 10, text: '', more: {} })).chunks;
out.chunks.idleKeeps = idles.length;
// an answer past the last end is a gap (the last line was unfinished): read the last window again from its start
const gap = app.followChunks([A], { start: 15, end: 20, size: 20, text: 'z', more: {} });
out.chunks.gap = [gap.resync, gap.grew, gap.chunks.map((c) => [c.start, c.end])];
out.chunks.gapEmpty = app.followChunks([A], { start: 12, end: 12, size: 12, text: '', more: {} }).resync;
out.chunks.behind = app.followChunks([{ ...A, start: 5, end: 10 }], { start: 7, end: 12, size: 12, text: 'x', more: {} });
out.resync = {
  query: [app.resyncQuery([{ start: 0, end: 5 }, { start: 5, end: 10 }]), app.resyncQuery([]), app.resyncQuery(null)],
  path: app.logFilePath('org/name', 'r1', null, 'plan.log', app.resyncQuery([{ start: 5, end: 10 }])),
  replaced: (() => { const r = app.resyncChunks([A, { ...A, start: 10, end: 14, size: 14, text: 'p' }], { start: 10, end: 20, size: 20, text: 'pq', more: {} }); return [r.chunks.map((c) => [c.start, c.end, c.text]), r.grew]; })(),
  moved: [app.resyncChunks([A], { start: 4, end: 20, size: 20, text: 'x', more: {} }), app.resyncChunks([A], { start: 0, end: 4, size: -1, text: '', more: {} }), app.resyncChunks([], { start: 0, end: 1, size: 1, text: 'x', more: {} })],
};
// a window over LOG_CHUNK that ends inside an unfinished line: the resync asks for all of it plus LOG_CHUNK, never less
const bigWindow = { start: 0, end: 140000, size: 400000, text: 'l'.repeat(139999) + '\npart', more: { before: null, after: true } };
const bigGap = app.followChunks([bigWindow], { start: 140010, end: 140020, size: 400000, text: 'next', more: {} });
const bigShort = app.resyncChunks([bigWindow], { start: 0, end: 100000, size: 400000, text: 'l'.repeat(99999), more: {} });
const bigFull = app.resyncChunks([bigWindow], { start: 0, end: 200000, size: 400000, text: 'l'.repeat(199999) + '\nnext', more: {} });
out.bigResync = {
  gap: [bigGap.resync, bigGap.chunks.length, bigGap.chunks[0].end],
  query: app.resyncQuery([bigWindow]),
  huge: app.resyncQuery([{ start: 0, end: 300000 }]),
  shorter: bigShort,
  full: [bigFull.chunks.map((c) => [c.start, c.end]), bigFull.grew, bigFull.previous.end],
};
out.delay = [app.followDelay(3000, true, false), app.followDelay(3000, true, true), app.followDelay(3000, false, false), app.followDelay(6000, false, false), app.followDelay(15000, false, false), app.followDelay(NaN, false, null)];

// ---- the more-before / more-after headers ----
const M = (o, range) => app.logMore(headers(o), range);
const R = { size: 300, start: 100, end: 200 };
out.more = {
  both: M({ 'X-Log-More-Before': 'true', 'X-Log-More-After': 'true' }, R),
  neither: M({ 'X-Log-More-Before': 'false', 'X-Log-More-After': 'false' }, { size: 10, start: 0, end: 10 }),
  malformed: ['yes', 'TRUE', '1', '', ' true', 'true ', 'false\n', 'null', 'tru'].map((v) => M({ 'X-Log-More-Before': v, 'X-Log-More-After': v }, R)),
  missing: [M({}, R), app.logMore(null, R), app.logMore(undefined, undefined)],
  contradicts: [M({ 'X-Log-More-Before': 'true', 'X-Log-More-After': 'false' }, { size: 300, start: 0, end: 100 }), M({ 'X-Log-More-Before': 'false', 'X-Log-More-After': 'true' }, { size: 300, start: 100, end: 300 })],
  noRange: M({ 'X-Log-More-Before': 'true', 'X-Log-More-After': 'true' }, null),
  of: [app.logMoreOf([{ more: { before: false, after: true } }, { more: { before: true, after: false } }]), app.logMoreOf([{ more: { before: 'yes', after: 1 } }]), app.logMoreOf([]), app.logMoreOf(null), app.logMoreOf([{}])],
};
const text = (nodes) => summary(nodes).text;
const view = (more) => ({ name: 'plan.log', size: 300, chunks: [{ start: 100, end: 200, text: 'x', more }] });
const hnd = { earlier() {}, later() {}, tail() {}, close() {} };
out.moreText = [{ before: true, after: true }, { before: false, after: false }, { before: null, after: null }, undefined].map((more) => {
  const t = text(app.renderLogView(view(more), hnd));
  return t.slice(t.indexOf('More text before'), t.indexOf('Load earlier') > 0 ? t.indexOf('Load earlier') : undefined).split('Load')[0];
});

// ---- two panels, each with its own range, controls, live region, timer and abort handling ----
const RUN = '/repos/org/name/runs/r1/logs/';
const JOB = '/repos/org/name/runs/r1/jobs/fix-a/logs/';
const calls = []; const hold = new Set(); const unexpected = [];
const table = {
  [`${RUN}plan.log?tail=65536`]: ans('p1\np2', 100, 200, 300),
  [`${RUN}plan.log?offset=200&limit=65536`]: ans('p3', 200, 300, 300),
  [`${RUN}plan.log?offset=300&limit=65536`]: ans('', 300, 300, 300),
  [`${JOB}1-build-0.jsonl?tail=65536`]: ans('j1\nj2', 600, 1000, 1000),
  [`${JOB}1-build-0.jsonl?tail=65536&before=600`]: ans('j0', 0, 600, 1000),
  [`${JOB}1-build-0.jsonl?offset=1000&limit=65536`]: ans('n1\nn2', 1000, 1030, 1030),
  [`${JOB}1-build-0.jsonl?offset=1030&limit=65536`]: ans('', 1030, 1030, 1030),
  [`${RUN}supervisor.log?tail=65536`]: ans('s', 0, 1, 1),
  [`${RUN}supervisor.log?offset=1&limit=65536`]: ans('', 1, 1, 1),
  [`${JOB}1-build-0.stderr?tail=65536`]: ans('e', 0, 1, 1),
  [`${JOB}1-build-0.stderr?offset=1&limit=65536`]: ans('', 1, 1, 1),
  [`${JOB}1-build-0.jsonl?offset=50&limit=65536`]: ans('', 50, 50, 50),
};
const lastCall = (path) => calls.filter((c) => c.path === path).pop();
const clock = { hidden: false, fresh: true, timers: [] };
const container = new El('div');
const panels = app.createLogPanels({
  container, repo: 'org/name', run: 'r1', job: 'fix-a',
  read: (path, signal) => {
    calls.push({ path, signal });
    if (hold.has(path)) return new Promise(() => {});
    if (!(path in table)) { unexpected.push(path); return Promise.reject(Object.assign(new Error('x'), { status: 404 })); }
    return Promise.resolve(table[path]);
  },
  hidden: () => clock.hidden, fresh: () => clock.fresh,
  setTimer: (fn, ms) => { clock.timers.push({ fn, ms, off: false }); return clock.timers.length - 1; },
  clearTimer: (id) => { clock.timers[id].off = true; },
});
const fire = async () => { for (const t of clock.timers.filter((x) => !x.off)) { t.off = true; t.fn(); } await flush(); await flush(); };
const click = (panel, label) => find(panel.el, (x) => x.tag === 'button' && x.textContent === label)[0].handlers.click[0]({});
const chunksOf = (panel) => JSON.stringify(panel.view.chunks);
const liveOf = (panel) => find(panel.el, (x) => x.attrs['aria-live'] === 'polite')[0].textContent;
const statusOf = (panel) => panel.status.textContent;
const scroll = (panel, box) => find(panel.el, (x) => x.tag === 'pre')[0].handlers.scroll[0]({ target: at(box) });
const paths = (from) => calls.slice(from).map((c) => c.path);

const plan = panels.open('run', 'plan.log');
const job = panels.open('job', '1-build-0.jsonl');
await flush();
out.opened = { count: panels.panels().length, paths: paths(0), sections: find(container, (x) => x.tag === 'section').length, same: panels.open('run', 'plan.log') === plan };
const ids = [];
for (const panel of [plan, job]) find(panel.el, (x) => x.attrs.id).forEach((x) => ids.push(x.attrs.id));
out.controls = [plan, job].map((panel) => ({
  live: find(panel.el, (x) => x.attrs['aria-live'] === 'polite').length,
  checkbox: find(panel.el, (x) => x.tag === 'input' && x.attrs.type === 'checkbox' && x.handlers.change).length >= 1,
  buttons: find(panel.el, (x) => x.tag === 'button').length,
  pre: find(panel.el, (x) => x.tag === 'pre').map((x) => x.attrs.tabindex),
  region: [panel.el.tag, panel.el.attrs['aria-labelledby']],
  text: panel.el.textContent,
}));
out.uniqueIds = ids.length === new Set(ids).size;
const planFirst = chunksOf(plan);
const jobFirst = chunksOf(job);
click(plan, 'Load later');
await flush();
out.laterInPlan = { plan: plan.view.chunks.map((c) => [c.start, c.end]), jobSame: chunksOf(job) === jobFirst, jobSize: job.view.size, path: paths(2) };
const planSecond = chunksOf(plan);
click(job, 'Load earlier');
await flush();
out.earlierInJob = { job: job.view.chunks.map((c) => [c.start, c.end]), planSame: chunksOf(plan) === planSecond, planFirstDiffers: chunksOf(plan) !== planFirst, path: paths(3) };

// follow: only the tail past the last end is read, and new lines are announced in that panel only
const before = calls.length;
await fire();
out.follow = { paths: paths(before), jobEnds: job.view.chunks.map((c) => c.end), planEnds: plan.view.chunks.map((c) => c.end), jobLive: liveOf(job), planLive: liveOf(plan), shown: job.el.textContent.includes('n2') };
out.followKeys = calls.map((c) => c.path.split('?')[1] || '').filter(Boolean).map((q) => q.split('&').map((pair) => pair.split('=')[0]).join(','));
out.status = [statusOf(job)];

// hidden tab: no request; shown again: polling resumes at once
clock.hidden = true;
panels.wake();
const hiddenFrom = calls.length;
await fire();
out.hidden = { status: statusOf(job), calls: calls.length - hiddenFrom };
clock.hidden = false;
panels.wake();
await fire();
out.shown = { status: statusOf(job), paths: paths(hiddenFrom) };

// scrolled up in one panel pauses that panel only, and scrolling back to the bottom resumes it
scroll(job, [0, 1000, 100]);
const upFrom = calls.length;
await fire();
out.scrolledUp = { status: statusOf(job), paths: paths(upFrom) };
scroll(job, [900, 1000, 100]);
await fire();
out.scrolledBack = { status: statusOf(job), paths: paths(upFrom) };

// the switch
const toggle = find(job.el, (x) => x.tag === 'input' && x.attrs.type === 'checkbox')[0];
toggle.handlers.change[0]({ target: { checked: false } });
const offFrom = calls.length;
await fire();
out.off = { status: statusOf(job), paths: paths(offFrom) };
toggle.handlers.change[0]({ target: { checked: true } });
await fire();
out.on = { status: statusOf(job), paths: paths(offFrom) };

// own abort handling: a read of one panel is left alone by the reads of the other, and cancelled only by a newer read of its own panel
hold.add(`${JOB}1-build-0.jsonl?offset=1030&limit=65536`);
await fire();
const slow = lastCall(`${JOB}1-build-0.jsonl?offset=1030&limit=65536`);
table[`${RUN}plan.log?tail=65536`] = ans('p1\np2', 100, 200, 300);
click(plan, 'Show the end again');
await flush();
const planCall = lastCall(`${RUN}plan.log?tail=65536`);
out.abort = { slowPath: slow.path, planPath: planCall.path, distinct: slow.signal !== planCall.signal, slowAborted: slow.signal.aborted, planAborted: planCall.signal.aborted, shared: calls.filter((c) => c.signal === undefined || c.signal === null).length };
table[`${JOB}1-build-0.jsonl?tail=65536`] = ans('j2\nn1\nn2', 700, 1030, 1030);
click(job, 'Show the end again');
await flush();
out.abort.sameAborted = slow.signal.aborted;
out.abort.planStill = planCall.signal.aborted;
hold.clear();

// a file shorter than what was loaded is read again from its tail
table[`${JOB}1-build-0.jsonl?offset=1030&limit=65536`] = ans('', 50, 50, 50);
table[`${JOB}1-build-0.jsonl?tail=65536`] = ans('only', 0, 50, 50);
const shrinkFrom = calls.length;
await fire();
out.shrank = { paths: paths(shrinkFrom).filter((path) => path.startsWith(JOB)), chunks: job.view.chunks.map((c) => [c.start, c.end]) };

// at most four panels, valid names only
panels.open('run', 'supervisor.log');
panels.open('job', '1-build-0.stderr');
const fifth = panels.open('job', '1-review-0.jsonl');
out.limit = { count: panels.panels().length, fifth, note: container.children[0].textContent, invalid: [panels.open('run', '../x'), panels.open('job', 'plan.log'), panels.open('job', 'x')] };
await flush();
const supervisor = panels.panels().filter((p) => p.name === 'supervisor.log')[0];
click(supervisor, 'Close');
out.closed = { count: panels.panels().length, keys: panels.panels().map((p) => p.key), sections: find(container, (x) => x.tag === 'section').length };
await fire();

// leaving the page stops everything
hold.add(`${JOB}1-build-0.jsonl?offset=50&limit=65536`);
await fire();
const pending = lastCall(`${JOB}1-build-0.jsonl?offset=50&limit=65536`);
const lastCount = calls.length;
panels.dispose();
await fire();
out.disposed = { aborted: pending.signal.aborted, noMoreCalls: calls.length === lastCount, opened: panels.open('run', 'plan.log') };
out.unexpected = unexpected;

// a window that ended inside a line still being written: the next answer starts past it, so the window is read again from its start
{
  const gapCalls = [];
  const gapTable = {
    [`${RUN}plan.log?tail=65536`]: ans('a\nb\npar', 0, 7, 7),
    [`${RUN}plan.log?offset=7&limit=65536`]: ans('next', 9, 14, 14),
    [`${RUN}plan.log?offset=0&limit=65543`]: ans('a\nb\npart\nnext', 0, 14, 14),
    [`${RUN}plan.log?offset=14&limit=65536`]: ans('', 14, 14, 14),
  };
  const timers = [];
  const box = new El('div');
  const gapPanels = app.createLogPanels({
    container: box, repo: 'org/name', run: 'r1', job: 'fix-a',
    read: (path) => { gapCalls.push(path); return Promise.resolve(gapTable[path] || ans('', 0, 0, 0)); },
    hidden: () => false, fresh: () => true,
    setTimer: (fn, ms) => { timers.push({ fn, ms, off: false }); return timers.length - 1; },
    clearTimer: (id) => { timers[id].off = true; },
  });
  const gapPanel = gapPanels.open('run', 'plan.log');
  await flush();
  for (let i = 0; i < 2; i++) { for (const t of timers.filter((x) => !x.off)) { t.off = true; t.fn(); } await flush(); await flush(); }
  out.gapPanel = {
    paths: gapCalls,
    chunks: gapPanel.view.chunks.map((c) => [c.start, c.end, c.text]),
    shown: gapPanel.el.textContent.includes('part') && gapPanel.el.textContent.includes('next'),
    live: find(gapPanel.el, (x) => x.attrs['aria-live'] === 'polite')[0].textContent,
  };
  gapPanels.dispose();
}
console.log(JSON.stringify(out));
"""
FOLLOW_SCRIPT = "".join([DOM.split("const out = {};", 1)[0], "(async () => {\n", FOLLOW, "\n})();\n"])


@unittest.skipUnless(NODE, "node is not installed; follow mode and the log panels of app.js are then not exercised")
class LogFollowTests(unittest.TestCase):
    """Follow mode (pure decisions and the poll), the more-before/after headers, and several panels open together."""

    @classmethod
    def setUpClass(cls):
        cls.out = ScriptTests.run_node(FOLLOW_SCRIPT)

    def test_follow_and_pause_decisions_cover_the_bottom_a_hidden_tab_scrolling_up_and_resuming(self):
        decide = self.out["decide"]
        self.assertEqual(decide["following"], [True, "following"])
        self.assertEqual(decide["hidden"], [False, "hidden"])
        self.assertEqual(decide["scrolled"], [False, "scrolled"])
        self.assertEqual(decide["resumedShown"], [True, "following"])
        self.assertEqual(decide["resumedBottom"], [True, "following"])
        self.assertEqual(decide["hiddenAndScrolled"], [False, "hidden"])
        self.assertEqual(decide["off"], [False, "off"])
        self.assertEqual(decide["junk"], [[False, "off"]] * 3)
        self.assertIn("hidden", decide["notes"][1])
        self.assertIn("scrolled up", decide["notes"][2])
        # at the bottom within a few pixels; a box without numbers counts as at the bottom
        self.assertEqual(self.out["bottom"], [True, True, False, True, True])

    def test_the_follow_query_is_the_tail_past_the_last_end_with_the_existing_keys_only(self):
        tail = {"tail": 65536}
        self.assertEqual(self.out["query"], [tail, tail, {"offset": 1234, "limit": 65536}, tail, tail])
        run, job = "/repos/org/name/runs/r1", "/repos/org/name/runs/r1/jobs/fix-a"
        self.assertEqual(
            self.out["followPath"],
            [f"{run}/logs/plan.log?offset=1234&limit=65536", f"{job}/logs/1-build-0.jsonl?offset=77&limit=65536", f"{job}/logs/1-build-0.jsonl?tail=65536"],
        )
        for keys in self.out["followKeys"]:
            self.assertLessEqual(set(keys.split(",")), {"offset", "limit", "tail", "before"})

    def test_followed_text_is_appended_after_the_last_end_and_an_idle_log_pushes_nothing_out(self):
        chunks = self.out["chunks"]
        self.assertEqual(chunks["grown"], [[[0, 20]], True])  # merged into the window it continues
        self.assertEqual(chunks["idle"], [[[0, 10]], False])
        self.assertEqual(chunks["shrank"], [None, None])
        self.assertTrue(chunks["first"])
        # small follow answers merge into the last window: the loaded tail stays and is not pushed out by eight polls
        merged = chunks["merged"]
        self.assertEqual(merged["starts"], [[0, 22, 22]])
        self.assertEqual(merged["text"], ["tail\n" + "\n".join(f"n{i}" for i in range(12))])
        self.assertEqual(merged["more"], [{"before": False, "after": False}])
        # only windows that reached the byte budget start a new one, and then the far one goes
        self.assertEqual(chunks["bounded"], [4, 5, 6, 7, 8, 9, 10, 11])
        self.assertEqual(chunks["idleKeeps"], 1)
        self.assertEqual(self.out["delay"], [3000, 250, 6000, 12000, 15000, 6000])

    def test_a_follow_answer_past_the_last_end_reads_the_last_window_again_instead_of_skipping_text(self):
        self.assertEqual(self.out["chunks"]["gap"], [True, False, [[0, 10]]])
        self.assertTrue(self.out["chunks"]["gapEmpty"])
        self.assertIsNone(self.out["chunks"]["behind"])
        resync = self.out["resync"]
        # the answer must cover the whole window again plus one LOG_CHUNK of new text (here 5 + 65536 bytes)
        self.assertEqual(resync["query"], [{"offset": 5, "limit": 65541}, {"tail": 65536}, {"tail": 65536}])
        self.assertEqual(resync["path"], "/repos/org/name/runs/r1/logs/plan.log?offset=5&limit=65541")
        big = self.out["bigResync"]
        self.assertEqual(big["gap"], [True, 1, 140000])  # the window is kept whole: a gap never splits it
        self.assertEqual(big["query"], {"offset": 0, "limit": 140000 + 65536})
        self.assertEqual(big["huge"], {"offset": 0, "limit": 256 * 1024})  # capped at the server's MAX_LIMIT
        self.assertIsNone(big["shorter"])  # an answer that ends before the window's end never shrinks it
        self.assertEqual(big["full"], [[[0, 200000]], True, 140000])  # the answer replaces the window and grows it
        self.assertEqual(resync["replaced"], [[[0, 10, "a"], [10, 20, "pq"]], True])
        self.assertEqual(resync["moved"], [None, None, None])
        panel = self.out["gapPanel"]
        base = "/repos/org/name/runs/r1/logs/plan.log"
        self.assertEqual(
            panel["paths"],
            [f"{base}?tail=65536", f"{base}?offset=7&limit=65536", f"{base}?offset=0&limit=65543", f"{base}?offset=14&limit=65536"],
        )
        self.assertEqual(panel["chunks"], [[0, 14, "a\nb\npart\nnext"]])
        self.assertTrue(panel["shown"])
        self.assertIn("2 new lines in plan.log", panel["live"])

    def test_the_more_before_and_after_headers_are_read_strictly(self):
        more = self.out["more"]
        self.assertEqual(more["both"], {"before": True, "after": True})
        self.assertEqual(more["neither"], {"before": False, "after": False})
        # a malformed value, or one that contradicts the range of the same answer, is unknown and not trusted
        self.assertEqual(more["malformed"], [{"before": None, "after": None}] * 9)
        self.assertEqual(more["missing"], [{"before": None, "after": None}] * 3)
        self.assertEqual(more["contradicts"], [{"before": None, "after": None}] * 2)
        self.assertEqual(more["noRange"], {"before": None, "after": None})
        self.assertEqual(more["of"], [{"before": False, "after": False}, {"before": None, "after": None}, {"before": None, "after": None}, {"before": None, "after": None}, {"before": None, "after": None}])

    def test_the_panel_shows_whether_more_text_exists_before_and_after(self):
        yes, no, unknown, missing = self.out["moreText"]
        self.assertIn("More text before this range: yes. More text after it: yes.", yes)
        self.assertIn("More text before this range: no. More text after it: no.", no)
        self.assertIn("More text before this range: unknown. More text after it: unknown.", unknown)
        self.assertIn("unknown", missing)

    def test_two_panels_open_together_each_with_its_own_range_controls_and_live_region(self):
        opened = self.out["opened"]
        run, job = "/repos/org/name/runs/r1/logs", "/repos/org/name/runs/r1/jobs/fix-a/logs"
        self.assertEqual((opened["count"], opened["sections"], opened["same"]), (2, 2, True))
        self.assertEqual(opened["paths"], [f"{run}/plan.log?tail=65536", f"{job}/1-build-0.jsonl?tail=65536"])
        for controls in self.out["controls"]:
            self.assertEqual(controls["live"], 1)
            self.assertTrue(controls["checkbox"])
            self.assertGreater(controls["buttons"], 0)
            self.assertEqual(controls["pre"], ["0"])  # the text is reachable by keyboard
            self.assertEqual(controls["region"][0], "section")
            self.assertTrue(controls["region"][1].startswith("log-title-"))
            self.assertIn("Follow new lines", controls["text"])
        self.assertIn("plan.log", self.out["controls"][0]["text"])
        self.assertIn("1-build-0.jsonl", self.out["controls"][1]["text"])
        self.assertTrue(self.out["uniqueIds"])
        # loading later text in one panel leaves the other one's windows and range alone, and so does loading earlier text
        later = self.out["laterInPlan"]
        self.assertEqual(later["plan"], [[100, 200], [200, 300]])
        self.assertTrue(later["jobSame"])
        self.assertEqual(later["jobSize"], 1000)
        self.assertEqual(later["path"], [f"{run}/plan.log?offset=200&limit=65536"])
        earlier = self.out["earlierInJob"]
        self.assertEqual(earlier["job"], [[0, 600], [600, 1000]])
        self.assertTrue(earlier["planSame"])
        self.assertTrue(earlier["planFirstDiffers"])
        self.assertEqual(earlier["path"], [f"{job}/1-build-0.jsonl?tail=65536&before=600"])
        self.assertEqual(self.out["unexpected"], [])

    def test_following_appends_after_the_last_end_and_announces_the_new_lines_in_that_panel(self):
        follow = self.out["follow"]
        job = "/repos/org/name/runs/r1/jobs/fix-a/logs"
        self.assertIn(f"{job}/1-build-0.jsonl?offset=1000&limit=65536", follow["paths"])
        self.assertEqual(follow["jobEnds"], [600, 1030])  # the answer continues the last window and is merged into it
        self.assertEqual(follow["planEnds"], [200, 300])
        self.assertIn("2 new lines in 1-build-0.jsonl", follow["jobLive"])
        self.assertEqual(follow["planLive"], "")
        self.assertTrue(follow["shown"])

    def test_polling_pauses_while_the_tab_is_hidden_or_scrolled_up_and_resumes_afterwards(self):
        job = "/repos/org/name/runs/r1/jobs/fix-a/logs/1-build-0.jsonl?offset=1030&limit=65536"
        self.assertIn("Following: new lines", self.out["status"][0])
        self.assertEqual(self.out["hidden"]["calls"], 0)
        self.assertIn("hidden", self.out["hidden"]["status"])
        self.assertIn("Following:", self.out["shown"]["status"])
        self.assertEqual(self.out["shown"]["paths"].count(job), 1)
        # scrolled up: the job panel stops polling, the other panel does not
        up = self.out["scrolledUp"]
        self.assertIn("scrolled up", up["status"])
        self.assertNotIn(job, up["paths"])
        self.assertTrue(any("plan.log" in path for path in up["paths"]))
        back = self.out["scrolledBack"]
        self.assertIn("Following:", back["status"])
        self.assertIn(job, back["paths"])
        self.assertNotIn(job, self.out["off"]["paths"])
        self.assertIn("Not following", self.out["off"]["status"])
        self.assertIn(job, self.out["on"]["paths"])

    def test_a_log_read_is_cancelled_only_by_a_newer_read_of_its_own_panel(self):
        abort = self.out["abort"]
        self.assertTrue(abort["distinct"])
        self.assertFalse(abort["slowAborted"])  # the other panel's read did not cancel it
        self.assertFalse(abort["planAborted"])
        self.assertTrue(abort["sameAborted"])  # a newer read of its own panel did
        self.assertFalse(abort["planStill"])
        self.assertEqual(abort["shared"], 0)  # every read carries a signal of its own

    def test_the_page_gives_log_reads_their_own_signal_and_never_the_shared_controller(self):
        script = (UI / "app.js").read_text(encoding="utf-8")
        fetch = script[script.index("function fetchJson("):]
        fetch = fetch[: fetch.index("return window.fetch(")]
        self.assertLess(fetch.index("signal = own"), fetch.index("state.controller.abort()"))
        self.assertIn("read: (path, signal) => api(path, { text: true, signal })", script)
        self.assertIn("fetchJson(path, token, body, accept, own)", script)
        panels = script[script.index("function createLogPanels(options)"):]
        panels = panels[: panels.index("The page (browser only)")]
        self.assertNotIn("state.controller", panels)
        self.assertIn("new AbortController()", panels)

    def test_a_shorter_file_is_read_again_from_its_tail_and_the_panels_are_bounded_and_closed_cleanly(self):
        job = "/repos/org/name/runs/r1/jobs/fix-a/logs/1-build-0.jsonl"
        self.assertEqual(self.out["shrank"]["paths"], [f"{job}?offset=1030&limit=65536", f"{job}?tail=65536"])
        self.assertEqual(self.out["shrank"]["chunks"], [[0, 50]])
        limit = self.out["limit"]
        self.assertEqual(limit["count"], 4)
        self.assertIsNone(limit["fifth"])
        self.assertIn("At most 4 logs", limit["note"])
        self.assertEqual(limit["invalid"], [None, None, None])
        self.assertEqual(self.out["closed"]["count"], 3)
        self.assertEqual(self.out["closed"]["sections"], 3)
        self.assertNotIn("run-supervisor.log", self.out["closed"]["keys"])
        disposed = self.out["disposed"]
        self.assertTrue(disposed["aborted"])
        self.assertTrue(disposed["noMoreCalls"])
        self.assertIsNone(disposed["opened"])

    def test_the_style_sheet_lays_the_panels_out_side_by_side_with_a_text_only_live_region(self):
        style = (UI / "style.css").read_text(encoding="utf-8")
        for selector in (".logpanels", ".logpanel", ".logfollow", ".sr-only"):
            self.assertIn(selector, style)
        self.assertIn("grid-template-columns: repeat(auto-fit", style)


OVERVIEW = r"""
const NOW = Date.parse('2026-11-08T12:00:30Z');
const out = {};
const PR = (n) => `https://github.com/org/name/pull/${n}`;
out.cards = [
  app.repoCardFigures({
    repo: 'org/name', jobs_by_status: { running: 2, blocked: 1 }, runs_total: 1,
    runs: [
      { cost_usd: 3, jobs_total: 8, jobs: [
        { pr: PR(7), status: 'running' }, { pr: PR(7), status: 'blocked' },
        { pr: PR(8), status: 'pr_ready', pr_observed: { handoff_status: 'merged' } },
        { pr: PR(9), status: 'running', handoff_status: 'closed' },
        { pr: 'https://github.com/other/name/pull/10' }, { pr: 'javascript:alert(1)' }, null, 'x',
      ] },
      { cost_usd: 2.5 }, { cost_usd: -1 }, { cost_usd: '5' }, { cost_usd: NaN }, { cost_usd: Infinity }, null, 7,
    ],
  }, { state: 'ok', rate: 0.1234, counts: { counted: 40 } }),
  app.repoCardFigures(null, null),
  app.repoCardFigures({}, undefined),
  app.repoCardFigures({ repo: 'org/name', jobs_by_status: { running: '5', blocked: -1 }, runs: 'nope' }, 'x'),
  app.repoCardFigures({ repo: 'org/name', runs_total: 3, jobs_by_status: {}, runs: [{ cost_usd: 1, jobs_total: 5, jobs: [] }] }, { state: 'checking' }),
  app.repoCardFigures({ repo: 'org/name', jobs_by_status: { running: 1.5, blocked: NaN }, runs: [{ cost_usd: 1e308 }, { cost_usd: 1e308 }] }, {}),
  app.repoCardFigures({ repo: '<b>x</b>/y', jobs_by_status: [1], runs: [{ jobs: [{ pr: PR(1) }] }] }, null),
  app.repoCardFigures({ repo: 'org/name', jobs_by_status: { '__proto__': { running: 9 } }, runs: [] }, null),
];
out.rates = [
  null, 'x', [], {}, { state: 'checking' }, { state: 'unavailable', reason: 'refresh_failed' },
  { state: 'ok', rate: null, sample: 'below_minimum' }, { state: 'ok', rate: 1.5 }, { state: 'ok', rate: NaN }, { state: 'ok', rate: -0.1 },
  { state: 'stale', rate: 0.25, counts: { counted: 'x' } }, { state: 'ok', rate: 0, counts: { counted: 12 } },
  { state: CANARY, rate: 0.5 }, { state: 'ok', rate: '0.5' },
].map((value) => app.falseIncompleteFigure(value).text);
out.listed = [
  app.listedFalseIncomplete({ repos: [{ repo: 'a/b', false_incomplete: { state: 'checking' } }, { repo: 'a/b', false_incomplete: 1 }] }, 'a/b'),
  app.listedFalseIncomplete({ repos: [{ repo: 'a/c' }] }, 'a/c'),
  app.listedFalseIncomplete({ repos: 'x' }, 'a/b'), app.listedFalseIncomplete(null, 'a/b'), app.listedFalseIncomplete({ repos: [null, 5] }, 'a/b'),
];
const T = (at, status, phase, attempt, previous, seconds) => ({ at, status, phase, attempt, previous_phase: previous, previous_phase_seconds: seconds });
const rows = (events, totals) => app.stageTimeline(events, totals).map((r) => [r.stage, r.seconds, r.seen]);
out.totals = rows(undefined, { plan: 30, build: 600, review: -5, verify: NaN, publish: '9', [CANARY]: 5 });
out.nothing = [rows(undefined, null), rows({}, []), rows('x', 'y'), rows([null, 'x', 5, {}, { phase: CANARY, previous_phase: CANARY, previous_phase_seconds: 5 }], null)];
const first = T('2026-11-08T10:10:00Z', 'running', 'review', 1, 'build', 600);
out.reported = rows([
  first, T('2026-11-08T10:00:00Z', 'running', 'build', 1, 'plan', 60), { ...first },
  T('2026-11-08T10:15:00Z', 'running', 'verify', 1, 'review', 300), T('2026-11-08T10:20:00Z', 'running', 'build', 2, 'verify', 'x'),
  T('2026-11-08T10:30:00Z', 'running', 'build', 2, 'build', 100), T(null, 'running', 'plan', 1, 'preflight', 5),
  null, 'x', 5, { phase: CANARY, previous_phase: CANARY, previous_phase_seconds: 5 },
], { review: 310 });
out.derived = rows([
  { at: '2026-11-08T10:20:00Z', status: 'pr_ready', phase: 'verify', attempt: 1 },
  { at: '2026-11-08T10:00:00Z', status: 'running', phase: 'plan', attempt: 1 },
  { at: '2026-11-08T10:00:00Z', status: 'running', phase: 'plan', attempt: 1 },
  { at: '2026-11-08T10:02:00Z', status: 'running', phase: 'build', attempt: 1 },
  { at: '2026-11-08T10:12:00Z', status: 'running', phase: 'review', attempt: 1 },
  { at: 'not a time', status: 'running', phase: 'publish', attempt: 1 },
  { at: '2026-11-08T10:15:00Z', status: 'running', phase: 'verify', attempt: 1 },
], null);
out.later = [rows([T('2026-11-08T10:00:00Z', 'running', 'review', 1, 'build', null)], null), rows(undefined, { verify: 5 })];
out.huge = rows([T('2026-11-08T10:00:00Z', 'running', 'build', 1, 'plan', 1e308), T('2026-11-08T10:01:00Z', 'running', 'review', 1, 'plan', 1e308)], null);
const GOOD = { repo: 'org/name', run: 'r1', job: 'fix-a', pr: PR(7), issues: [1, 2] };
out.trace = {
  good: app.traceLinks(GOOD),
  badJob: app.traceLinks({ ...GOOD, job: '../x', issues: [] }),
  badRun: app.traceLinks({ ...GOOD, run: 'r 1', pr: null, issues: [3] }),
  badPr: app.traceLinks({ ...GOOD, pr: 'https://evil.example/org/name/pull/7', issues: [0, '7', 2.5, null, 3] }),
  otherPr: app.traceLinks({ ...GOOD, pr: 'https://github.com/other/name/pull/7', issues: [] }),
  badRepo: app.traceLinks({ ...GOOD, repo: 'bad repo' }),
  notStrings: [null, 12345, ['fix-a'], { toString() { return 'fix-a'; } }, undefined].map((job) => app.traceLinks({ ...GOOD, job, issues: [] })).concat([null, 7, ['r1']].map((run) => app.traceLinks({ ...GOOD, run, issues: [] }))),
  none: [app.traceLinks(null), app.traceLinks({ repo: 'org/name', run: 'r1', job: 'fix-a' }), app.traceLinks({ ...GOOD, pr: '', issues: 'x' })],
  many: app.traceLinks({ ...GOOD, pr: null, issues: Array.from({ length: 12 }, (_, i) => i + 1) }).length,
};
const dash = { generated_at: '2026-11-08T12:00:00Z', repos: [
  { repo: 'org/name', jobs_by_status: { running: 1, blocked: 2 }, runs_total: 1, runs: [
    { run: 'r1', supervisor: { state: 'running' }, cost_usd: 3.5, budget_usd: 25, jobs_total: 2, jobs: [
      { id: 'fix-a', issues: [1, 'javascript:alert(1)'], status: 'running', pr: PR(9), phase: 'build', attempt: 1 },
      { id: '../evil', issues: [2], status: CANARY, pr: 'https://evil.example/x', phase: 'build' },
    ] },
  ] },
  { repo: 'org/other', error: 'invalid configuration', runs: [] },
  { repo: CANARY, runs: [] },
] };
const listing = { repos: [
  { repo: 'org/name', false_incomplete: { state: 'ok', rate: 0.05, counts: { counted: 20 } } },
  { repo: 'org/other', false_incomplete: { state: 'checking' } },
] };
out.view = summary(app.renderActive(dash, NOW, undefined, listing));
out.noListing = summary(app.renderActive(dash, NOW, undefined, null)).text;
out.hostileListing = summary(app.renderActive(dash, NOW, undefined, { repos: [{ repo: 'org/name', false_incomplete: { state: CANARY, rate: CANARY } }] })).text;
out.noRepos = summary(app.renderActive({ repos: [] }, NOW)).tags;
const detail = {
  source: 'state', repo: 'org/name', run: 'r1', job: 'fix-a', issues: [1], status: 'running', phase: 'review', attempt: 1, retries: 0,
  pr: PR(7), phase_seconds: { plan: 60, build: 600, review: 300 },
  phase_events: [T('2026-11-08T10:00:00Z', 'running', 'build', 1, 'plan', 60)],
};
out.job = summary(app.renderJob(detail)).text;
out.jobEmpty = summary(app.renderJob({ ...detail, phase_seconds: {}, phase_events: [] })).text;
out.ledger = summary(app.renderJob({ source: 'ledger', repo: 'org/name', run: 'r1', job: 'fix-a', issues: [1], status: 'merged', history: { outcome: 'merged' },
  timeline: [{ at: '2026-11-08T10:00:00Z', status: 'running', phase: 'build', attempt: 1 }] })).text;
console.log(JSON.stringify(out));
"""
OVERVIEW_SCRIPT = "".join([DOM.split("const out = {};", 1)[0], OVERVIEW])


@unittest.skipUnless(NODE, "node is not installed; the overview helpers of app.js are then not exercised")
class OverviewTests(unittest.TestCase):
    """Repository cards, the stage timeline and the trace links: pure helpers on hostile data, then the rendered views."""

    @classmethod
    def setUpClass(cls):
        cls.out = ScriptTests.run_node(OVERVIEW_SCRIPT)

    def test_card_figures_count_only_what_is_valid_and_leave_the_rest_unknown(self):
        full, null, empty, junk, partial, huge, markup, proto = self.out["cards"]
        unavailable = {"kind": "unavailable", "rate": None, "text": "not available"}
        self.assertEqual(full, {"openPrs": 1, "running": 2, "blocked": 1, "spend": 5.5, "partial": False, "falseIncomplete": {"kind": "ok", "rate": 0.1234, "text": "12.3% of 40 pull requests"}})
        nothing = {"openPrs": None, "running": None, "blocked": None, "spend": None, "partial": False, "falseIncomplete": unavailable}
        self.assertEqual(null, nothing)
        self.assertEqual(empty, nothing)
        self.assertEqual(junk, {**nothing, "openPrs": 0})  # a string or negative count is not a count
        self.assertEqual(partial, {"openPrs": 0, "running": 0, "blocked": 0, "spend": 1, "partial": True, "falseIncomplete": {"kind": "checking", "rate": None, "text": "checking"}})
        self.assertEqual(huge, {**nothing, "openPrs": 0})  # a sum that overflows is unknown, not Infinity
        self.assertEqual(markup, nothing)  # a repository name that is not valid yields no pull request count
        self.assertEqual((proto["running"], proto["blocked"]), (0, 0))  # only own keys count

    def test_the_false_incomplete_rate_is_shown_only_when_it_is_a_rate(self):
        none = "not available"
        few = "too few pull requests for a rate"
        self.assertEqual(
            self.out["rates"],
            [none, none, none, none, "checking", none, few, few, few, few, "25.0% (stale)", "0.0% of 12 pull requests", none, few],
        )
        found, missing, rows, no_listing, junk = self.out["listed"]
        self.assertEqual(found, {"state": "checking"})
        self.assertEqual((missing, rows, no_listing, junk), (None, None, None, None))

    def test_stage_durations_prefer_recorded_totals_then_reported_then_timestamps(self):
        def row(stage, seconds, seen):
            return [stage, seconds, seen]

        names = ["plan", "build", "review", "verify", "publish"]
        self.assertEqual(self.out["totals"], [row("plan", 30, True), row("build", 600, True), row("review", None, False), row("verify", None, False), row("publish", None, False)])
        blank = [row(name, None, False) for name in names]
        self.assertEqual(self.out["nothing"], [blank, blank, blank, blank])
        # unordered and duplicated events: a time the event reports counts once, a bad one is ignored, a recorded total wins
        self.assertEqual(self.out["reported"], [row("plan", 60, True), row("build", 700, True), row("review", 310, True), row("verify", None, True), row("publish", None, False)])
        # events with no reported time (a purged run): the gap from a running event to the next one by timestamp, a bad timestamp is skipped
        self.assertEqual(self.out["derived"], [row("plan", 120, True), row("build", 600, True), row("review", 180, True), row("verify", 300, True), row("publish", None, True)])
        # a job is only created after its run was planned, so a stage before the furthest one named counts as reached
        self.assertEqual(self.out["later"][0], [row("plan", None, True), row("build", None, True), row("review", None, True), row("verify", None, False), row("publish", None, False)])
        self.assertEqual(self.out["later"][1], [row("plan", None, True), row("build", None, True), row("review", None, True), row("verify", 5, True), row("publish", None, False)])
        self.assertEqual(self.out["huge"][0], row("plan", None, True))

    def test_trace_links_are_built_only_from_valid_parts(self):
        trace = self.out["trace"]
        job = "#/job/org/name/r1/fix-a"
        self.assertEqual(
            trace["good"],
            [
                {"subject": "pr", "label": "PR #7", "url": "https://github.com/org/name/pull/7", "job": job, "logs": job},
                {"subject": "issue", "label": "#1", "url": "https://github.com/org/name/issues/1", "job": job, "logs": job},
                {"subject": "issue", "label": "#2", "url": "https://github.com/org/name/issues/2", "job": job, "logs": job},
            ],
        )
        self.assertEqual(trace["badJob"], [{"subject": "pr", "label": "PR #7", "url": "https://github.com/org/name/pull/7", "job": None, "logs": None}])
        self.assertEqual(trace["badRun"], [{"subject": "issue", "label": "#3", "url": "https://github.com/org/name/issues/3", "job": None, "logs": None}])
        bad = trace["badPr"]
        self.assertEqual(bad[0], {"subject": "pr", "label": "PR", "url": None, "job": job, "logs": job})
        self.assertEqual([(entry["label"], entry["url"]) for entry in bad[1:]], [("issue", None)] * 4 + [("#3", "https://github.com/org/name/issues/3")])
        self.assertEqual(trace["otherPr"], [{"subject": "pr", "label": "PR", "url": None, "job": job, "logs": job}])
        self.assertEqual([(entry["url"], entry["job"], entry["logs"]) for entry in trace["badRepo"]], [(None, None, None)] * 3)
        # A job or run id that is not a string is never a link part, even when its text would pass the pattern.
        for case in trace["notStrings"]:
            self.assertEqual([(entry["job"], entry["logs"]) for entry in case], [(None, None)], case)
        self.assertEqual(trace["none"], [[], [], []])
        self.assertEqual(trace["many"], 10)

    def test_the_cards_and_links_are_text_and_page_or_github_links_only(self):
        view = self.out["view"]
        for tag in ("script", "img", "iframe", "style", "svg", "object", "embed", "link", "base", "meta"):
            self.assertNotIn(tag, view["tags"])
        self.assertFalse([attr for attr in view["attrNames"] if attr.startswith("on") or attr in ("style", "src", "srcdoc")])
        self.assertFalse([cls for cls in view["classes"] if not re.fullmatch(r"[a-z0-9 _-]+", cls)])
        for href in view["hrefs"]:
            self.assertTrue(href.startswith(("#/", "https://github.com/")), href)
            self.assertNotIn("evil", href)
            self.assertNotIn("javascript", href)
        for expected in ("Open PRs1", "Running jobs1", "Blocked jobs2", "Spend$3.50", "False-incomplete rate5.0% of 20 pull requests", "invalid configuration"):
            self.assertIn(expected, view["text"])
        self.assertGreaterEqual(view["hrefs"].count("#/job/org/name/r1/fix-a"), 6)
        self.assertIn("https://github.com/org/name/pull/9", view["hrefs"])
        self.assertIn("https://github.com/org/name/issues/2", view["hrefs"])
        # what cannot be built is plain text, and a hostile repository name gets no card
        self.assertIn("PR (no valid link) → job (no valid link), logs (no valid link)", view["text"])
        self.assertIn("issue (no valid link) → job, logs", view["text"])
        self.assertNotIn("Overview of <script>", view["text"])

    def test_the_false_incomplete_rate_is_plain_not_available_text_without_the_listing_or_with_a_hostile_one(self):
        self.assertIn("False-incomplete ratenot available", self.out["noListing"])
        self.assertIn("False-incomplete ratenot available", self.out["hostileListing"])
        self.assertNotIn("<script>alert(1)</script>False", self.out["hostileListing"])
        self.assertNotIn("section", self.out["noRepos"])  # no repository, no overview

    def test_the_job_page_shows_five_stages_with_their_durations(self):
        for expected in ("Stages", "plan 1m 00s", "build 10m 00s", "review 5m 00s", "verify not reached", "publish not reached"):
            self.assertIn(expected, self.out["job"])
        self.assertIn("No stage was recorded yet.", self.out["jobEmpty"])
        self.assertIn("build no duration recorded", self.out["ledger"])
        self.assertIn("plan no duration recorded", self.out["ledger"])

    def test_the_script_reads_the_false_incomplete_rate_through_the_one_api_seam(self):
        script = (UI / "app.js").read_text(encoding="utf-8")
        self.assertEqual(script.count("api('/repos')"), 1)
        self.assertEqual(script.count("window.fetch("), 2)


if __name__ == "__main__":
    unittest.main()
