"""The tier-1 verification tools in the container image (issue #40).

There is no docker daemon in the unit-test environment, so these tests read the files that
define the image and keep them from drifting apart: the Dockerfile (what is installed, how
the downloads are pinned and verified), image/smoke.sh (what is proven about it as the
worker user), image/README.md and docs/verification-tiers.md (what is said about it). The
build itself and the smoke run need a docker daemon: run ``docker build`` and ``image/smoke.sh`` by hand.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
SMOKE = (ROOT / "image" / "smoke.sh").read_text(encoding="utf-8")
IMAGE_README = (ROOT / "image" / "README.md").read_text(encoding="utf-8")
TIERS_DOC = ROOT / "docs" / "verification-tiers.md"

# Debian packages in the runtime stage's apt-get install line.
APT_TOOLS = ("jq", "make", "shellcheck")
# Release downloads: tool -> (host the download comes from, upper-case ARG prefix).
RELEASE_TOOLS = {
    "terraform": ("https://releases.hashicorp.com/terraform/", "TERRAFORM"),
}
TIER1 = (*APT_TOOLS, *RELEASE_TOOLS)
# What must never be in the supervisor image: a daemon, a runtime for privileged
# containers, a scanner with its own vulnerability database.
FORBIDDEN = ("docker", "dockerd", "trivy")


def stage(name):
    """The text of one Dockerfile stage, from its FROM line to the next FROM."""
    match = re.search(rf"^FROM \S+ AS {re.escape(name)}\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"no Dockerfile stage named {name}")
    return match.group(1)


def code_lines(text):
    """The lines of a Dockerfile or shell script that are not comments."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def arg(name):
    match = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE, re.MULTILINE)
    if match is None:
        raise AssertionError(f"no ARG {name}= in the Dockerfile")
    return match.group(1)


def readme_section(heading):
    """The body of one ``##`` section of image/README.md."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", IMAGE_README, re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"no section {heading!r} in image/README.md")
    return match.group(1)


class PinTests(unittest.TestCase):
    def test_each_release_tool_has_a_version_and_a_sha256_pin(self):
        for tool, (_host, prefix) in RELEASE_TOOLS.items():
            with self.subTest(tool=tool):
                self.assertRegex(arg(f"{prefix}_VERSION"), r"^\d+\.\d+\.\d+$")
                self.assertRegex(arg(f"{prefix}_SHA256"), r"^[0-9a-f]{64}$")

    def test_the_stage_downloads_from_the_upstream_release_and_verifies_the_hash(self):
        for tool, (host, prefix) in RELEASE_TOOLS.items():
            with self.subTest(tool=tool):
                body = stage(tool)
                self.assertIn(f"FROM fetch AS {tool}\n", DOCKERFILE)
                self.assertIn(f'"{host}', body)
                self.assertIn(f"${{{prefix}_VERSION}}", body)
                self.assertIn(f"echo \"${{{prefix}_SHA256}}  /tmp/", body)
                self.assertIn("sha256sum --check --strict -", body)
                # curl fails the build on an HTTP error, and the hash is checked before use
                self.assertIn("--fail", body)
                self.assertLess(body.index("sha256sum --check"), body.index("install -D -m 0755"))

    def test_the_stage_fails_the_build_when_the_binary_reports_another_version(self):
        for tool, (_host, prefix) in RELEASE_TOOLS.items():
            with self.subTest(tool=tool):
                body = stage(tool)
                self.assertRegex(body, rf"/out/{tool} (-version|version) \| sed -n 1p\)\" = ")
                self.assertIn(f"${{{prefix}_VERSION}}", body.split("sed -n 1p")[-1])

    def test_the_downloads_are_fetched_in_the_download_stage_only(self):
        # curl and unzip exist only in the stage that never reaches the final image
        runtime = DOCKERFILE.split("FROM base AS runtime", 1)[1]
        self.assertNotIn("unzip", runtime)
        self.assertIn("ca-certificates curl unzip", stage("fetch"))
        for tool in RELEASE_TOOLS:
            self.assertNotIn(f"releases/download/v${{{RELEASE_TOOLS[tool][1]}_VERSION}}", runtime)


class DockerfileTests(unittest.TestCase):
    def test_apt_tools_are_installed_in_the_runtime_stage_and_recorded(self):
        runtime = DOCKERFILE.split("FROM base AS runtime", 1)[1]
        install = re.search(r"apt-get install -y --no-install-recommends \\\n(.*?);", runtime, re.DOTALL)
        self.assertIsNotNone(install)
        packages = install.group(1).replace("\\", " ").split()
        record = re.search(r"dpkg-query -W -f '[^']*' \\\n(.*?);", runtime, re.DOTALL)
        self.assertIsNotNone(record)
        recorded = record.group(1).replace("\\", " ").split()
        for tool in APT_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, packages)
                self.assertIn(tool, recorded)

    def test_release_tools_are_copied_into_the_final_image_without_chmod(self):
        # --chmod on COPY also changes the mode of a parent directory it creates (BuildKit 0.23)
        for tool in RELEASE_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(f"COPY --from={tool} /out/{tool} /usr/local/bin/{tool}\n", DOCKERFILE)
        for line in code_lines(DOCKERFILE):
            if "--from=terraform" in line:
                self.assertNotIn("--chmod", line)

    def test_every_tool_is_recorded_in_versions_txt(self):
        record = DOCKERFILE.split("/usr/share/factory-image/versions.txt", 1)[1]
        for tool in TIER1:
            with self.subTest(tool=tool):
                self.assertRegex(record, rf'echo "{tool}: \$\(')

    def test_terraform_cannot_call_home(self):
        runtime = DOCKERFILE.split("FROM base AS runtime", 1)[1]
        self.assertRegex(runtime, r"(?m)^\s+CHECKPOINT_DISABLE=1 \\$")
        self.assertIn("CHECKPOINT_DISABLE=1 /out/terraform version", stage("terraform"))

    def test_no_daemon_or_privileged_tooling_is_installed(self):
        for line in code_lines(DOCKERFILE):
            if "mirror/docker/library" in line:  # the base image is mirrored from the Docker library
                continue
            for word in FORBIDDEN:
                with self.subTest(word=word, line=line.strip()[:60]):
                    self.assertIsNone(re.search(rf"(?<![\w-]){word}(?![\w-])", line, re.IGNORECASE), line)

    def test_the_header_lists_the_new_pinned_download(self):
        header = DOCKERFILE.split("\nARG BASE_IMAGE=", 1)[0]
        self.assertIn("TERRAFORM_VERSION, TERRAFORM_SHA256", header)

    def test_actionlint_is_left_out_of_the_image_until_a_clean_release_exists(self):
        # the CI image job's Trivy report listed fixable HIGH findings in the Go standard
        # library of actionlint 1.7.12 (the newest release); a tool that cannot pass the
        # blocking scan is left out, so no stage, COPY, pin or smoke check may bring it back
        # without this test, the README section and the Trivy table being revisited together
        for line in code_lines(DOCKERFILE):
            self.assertIsNone(re.search(r"actionlint", line, re.IGNORECASE), line)
        for line in code_lines(SMOKE):
            self.assertIsNone(re.search(r"actionlint", line, re.IGNORECASE), line)


class SmokeTests(unittest.TestCase):
    def test_smoke_reads_the_pinned_versions_from_the_dockerfile(self):
        self.assertIn("'s/^ARG TERRAFORM_VERSION=//p'", SMOKE)
        self.assertIn('"Terraform v$terraform_want"', SMOKE)

    def test_each_tool_is_run_as_the_worker_user(self):
        # the multi-line check calls continue on the next line, so join each logical command
        logical = SMOKE.replace("\\\n\t", " ")
        worker_commands = [line for line in logical.splitlines() if "as_worker" in line]
        for tool in TIER1:
            with self.subTest(tool=tool):
                self.assertTrue(
                    any(re.search(rf"(?<![\w-]){tool}(?![\w-])", line) for line in worker_commands),
                    f"smoke.sh does not run {tool} through as_worker",
                )

    def test_failing_inputs_are_checked_not_only_passing_ones(self):
        logical = SMOKE.replace("\\\n\t", " ")
        for tool in ("shellcheck", "terraform fmt"):
            with self.subTest(tool=tool):
                self.assertTrue(
                    any(tool in line and "&& !" in line and "as_worker" in line for line in logical.splitlines()),
                    f"no reject check for {tool}",
                )
        self.assertTrue(any("jq -e" in line and "&& !" in line for line in logical.splitlines()))

    def test_smoke_guards_that_docker_dockerd_and_trivy_are_absent(self):
        logical = SMOKE.replace("\\\n\t", " ")
        guard = [line for line in logical.splitlines() if "no docker, dockerd or trivy in the image" in line]
        self.assertEqual(len(guard), 1)
        for word in FORBIDDEN:
            self.assertIn(f"! command -v {word}", guard[0])

    def test_smoke_checks_the_checkpoint_switch(self):
        self.assertIn('[ "$CHECKPOINT_DISABLE" = 1 ]', SMOKE)


class DocumentationTests(unittest.TestCase):
    def test_the_image_readme_lists_each_tool_with_its_source_and_reason(self):
        table = readme_section("What is in the image")
        for tool in TIER1:
            with self.subTest(tool=tool):
                rows = [
                    line
                    for line in table.splitlines()
                    if line.startswith("|") and re.search(rf"`[^`]*(?<![\w-]){tool}(?![\w-])[^`]*`", line.split("|")[1])
                ]
                self.assertTrue(rows, f"no table row naming {tool}")
        for prefix in ("TERRAFORM",):
            self.assertIn(f"{prefix}_VERSION", IMAGE_README)
            self.assertIn(f"{prefix}_SHA256", IMAGE_README)

    def test_the_image_readme_states_the_pinned_versions_that_the_dockerfile_has(self):
        pinned = readme_section("What \"pinned\" means")
        self.assertIn(arg("TERRAFORM_VERSION"), IMAGE_README)
        for prefix in ("TERRAFORM",):
            self.assertIn(f"`{prefix}_VERSION`", pinned)

    def test_the_image_readme_lists_what_is_deliberately_absent(self):
        absent = readme_section("Deliberately not in the image")
        for word in ("docker", "dockerd", "trivy", "browsers", "pytest", "runner base image"):
            with self.subTest(word=word):
                self.assertIn(word, absent.lower())
        self.assertIn("Broad tools, not broad privileges", IMAGE_README)

    def test_the_image_readme_says_why_actionlint_is_absent(self):
        absent = readme_section("Deliberately not in the image")
        row = [line for line in absent.splitlines() if line.startswith("| `actionlint`")]
        self.assertEqual(len(row), 1)
        self.assertIn("Go 1.26.1", row[0])
        self.assertIn("Trivy", row[0])
        self.assertIn("fixable", row[0])
        self.assertIn("CI", row[0])

    def test_the_image_readme_says_what_to_do_when_a_tool_fails_the_scan(self):
        bumping = readme_section("Bumping a pin")
        self.assertIn("terraform", bumping)
        self.assertRegex(bumping, r"(?i)no suppression|never a suppression|not a suppression|never suppress")

    def test_the_tier_document_specifies_all_four_tiers_and_the_not_planned_list(self):
        text = TIERS_DOC.read_text(encoding="utf-8")
        for needle in (
            "Broad tools, not broad privileges",
            "## Tier 1",
            "## Tier 2",
            "## Tier 3",
            "## Tier 4",
            "## Explicitly not planned",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        # tier 2 is implemented by the verification-environment issues, which the document names
        for number in ("#37", "#41", "#42", "#43", "#44", "#45"):
            self.assertIn(number, text)
        # the safe reading of "no required checks"
        self.assertRegex(text, r"(?i)no required checks")
        self.assertRegex(text, r"(?i)unproven")

    def test_the_tier_document_is_linked_from_the_readme_and_the_operations_guide(self):
        for name in ("README.md", "docs/operations.md"):
            with self.subTest(file=name):
                self.assertIn("verification-tiers.md", (ROOT / name).read_text(encoding="utf-8"))

    def test_the_tier_document_does_not_promise_docker(self):
        text = TIERS_DOC.read_text(encoding="utf-8")
        section = text.split("## Explicitly not planned", 1)[1]
        for word in ("Docker", "Docker-in-Docker", "privileged"):
            self.assertIn(word, section)


if __name__ == "__main__":
    unittest.main()
