"""Unit tests for the hardware-test issue build.yml opens for each build.

A human follows the issue body by hand, so every command in it must work as
written. The github-script block is extracted from build.yml and run under
node with a mocked GitHub client, exactly the code the workflow executes;
the rendered body is then checked against install.sh's flags, the assets
the release step uploads, and the markers promote.yml parses. The publish
step that classifies the build (stable or preview) runs under bash with a
stub `gh`."""
import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
INSTALL_SH = ROOT / "scripts" / "install.sh"

STEP_NAME = "- name: Create hardware-test issue for auto-build"
PUBLISH_STEP = "- name: Publish the draft as a prerelease (hardware-test gate)"


def step_lines(name=STEP_NAME):
    lines = BUILD_YML.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == name)
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= indent),
               len(lines))
    return lines[start:end]


def block_after(lines, key):
    at = next(i for i, line in enumerate(lines) if line.strip() == key)
    return textwrap.dedent("\n".join(lines[at + 1:]))


def issue_script():
    return block_after(step_lines(), "script: |")


HARNESS = """
console.log = (...a) => process.stderr.write(a.join(' ') + '\\n');
const created = [];
const github = { rest: { issues: {
  createLabel: async () => ({}),
  listForRepo: async () => ({ data: JSON.parse(process.env.OPEN_TITLES || '[]')
    .map((title) => ({ title })) }),
  create: async (args) => { created.push(args); },
} } };
const context = { repo: { owner: process.env.OWNER, repo: process.env.REPO } };
(async () => {
%s
})().then(() => process.stdout.write(JSON.stringify(created[0] ?? null)),
          (e) => { process.stderr.write(String(e)); process.exit(1); });
"""


def render_issue(tag, version, train, kver, sdk="2.1", preview=False,
                 owner="truenas-community-sysexts", repo="memryx-mx3-support",
                 open_titles=()):
    """The issues.create() arguments build.yml's step produces, or None when
    it skips creating one because an issue in open_titles names the tag."""
    env = {**os.environ,
           "RELEASE_TAG": tag, "TRUENAS_VERSION": version, "TRAIN_NAME": train,
           "REAL_KVER": kver, "MEMRYX_SDK": sdk,
           "IS_PREVIEW": "true" if preview else "false",
           "OWNER": owner, "REPO": repo,
           "OPEN_TITLES": json.dumps(list(open_titles))}
    p = subprocess.run(["node", "-e", HARNESS % issue_script()],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise AssertionError(f"node failed: {p.stderr}")
    return json.loads(p.stdout)


def release_assets():
    """Basenames of the files the release step uploads."""
    text = BUILD_YML.read_text()
    block = text[text.index("body_path: release-notes.md"):]
    block = block[block.index("files: |") + len("files: |"):block.index("draft: true")]
    return {Path(line.strip()).name for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")}


def install_flags():
    """Options install.sh's argument parser accepts."""
    return set(re.findall(r"^\s+(--[a-z-]+)(?:=\*)?\)", INSTALL_SH.read_text(), re.MULTILINE))


STABLE = dict(tag="k6.12.105-memryx2.1-r50", version="25.10.7", train="Goldeye",
              kver="6.12.105-production+truenas")
PREVIEW = dict(tag="k6.18.52-memryx2.1-r51", version="27.0.0-RC.1", train="Halfmoon",
               kver="6.18.52-production+truenas", preview=True)


class Markers(unittest.TestCase):
    # promote.yml's patterns, verbatim.
    TAG_RE = re.compile(r"<!--\s*release-tag:\s*(\S+?)\s*-->")
    PREVIEW_RE = re.compile(r"<!--\s*preview-build:\s*(\S+?)\s*-->")

    def test_stable_markers(self):
        issue = render_issue(**STABLE)
        self.assertIn(f"<!-- release-tag: {STABLE['tag']} -->", issue["body"])
        self.assertEqual(self.TAG_RE.search(issue["body"]).group(1), STABLE["tag"])
        self.assertEqual(self.PREVIEW_RE.search(issue["body"]).group(1), "false")
        self.assertEqual(issue["labels"], ["hardware-test"])
        self.assertIn(STABLE["tag"], issue["title"])

    def test_preview_markers(self):
        issue = render_issue(**PREVIEW)
        self.assertEqual(self.TAG_RE.search(issue["body"]).group(1), PREVIEW["tag"])
        self.assertEqual(self.PREVIEW_RE.search(issue["body"]).group(1), "true")
        self.assertEqual(issue["labels"], ["preview-hardware-test"])
        self.assertIn(PREVIEW["tag"], issue["title"])

    def test_channel_comes_from_the_publish_step(self):
        # k-tags carry no BETA marker: the issue takes the publish step's
        # classification instead of re-deriving it.
        issue = render_issue(**{**STABLE, "preview": True})
        self.assertEqual(issue["labels"], ["preview-hardware-test"])
        env = next(line for line in step_lines() if line.strip().startswith("IS_PREVIEW:"))
        self.assertIn("steps.publish.outputs.is_preview", env)

    def test_release_tag_env_matches_the_release_step(self):
        # The issue must name the tag the release step actually created.
        text = BUILD_YML.read_text()
        tag_name = re.search(r"^\s+tag_name: (.+)$", text, re.MULTILINE).group(1)
        env_tag = next(line.split(":", 1)[1].strip() for line in step_lines()
                       if line.strip().startswith("RELEASE_TAG:"))
        self.assertEqual(env_tag, tag_name)
        self.assertTrue(tag_name.startswith("k${{ needs.build.outputs.short_kver }}-memryx"),
                        tag_name)


class PublishStep(unittest.TestCase):
    """The publish step classifies the build once; its is_preview output
    labels the issue."""

    def classify(self, version):
        script = block_after(step_lines(PUBLISH_STEP), "run: |")
        with tempfile.TemporaryDirectory() as d:
            gh = Path(d, "gh")
            gh.write_text("#!/usr/bin/env bash\necho \"gh $*\" >> \"$GH_LOG\"\n")
            gh.chmod(0o755)
            out = Path(d, "out")
            env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}",
                       GITHUB_OUTPUT=str(out), GH_LOG=str(Path(d, "log")),
                       REPO="o/r", REL_ID="7", TRUENAS_VERSION=version)
            p = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                               capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            calls = Path(d, "log").read_text()
            outputs = dict(line.split("=", 1) for line in out.read_text().split())
        self.assertIn("-F draft=false -F prerelease=true", calls)
        return outputs["is_preview"]

    def test_beta_and_rc_versions_are_preview(self):
        for version in ("26.0.0-BETA.3", "27.0.0-RC.1", "26.0.0-rc1"):
            self.assertEqual(self.classify(version), "true", version)

    def test_stable_versions_are_not(self):
        for version in ("25.10.7", "25.10.3.1", "27.0.0"):
            self.assertEqual(self.classify(version), "false", version)


class Title(unittest.TestCase):
    # What to test | on what | which build. The step skips creating an issue
    # when an open one's title includes the tag, so the tag must appear in
    # the title verbatim; it goes last.
    def test_stable_title(self):
        self.assertEqual(render_issue(**STABLE)["title"],
                         "Hardware test: MemryX MX3 driver SDK 2.1 | "
                         "TrueNAS 25.10.7 (kernel 6.12.105) | k6.12.105-memryx2.1-r50")

    def test_preview_title(self):
        self.assertEqual(render_issue(**PREVIEW)["title"],
                         "Preview hardware test: MemryX MX3 driver SDK 2.1 | "
                         "TrueNAS 27.0.0-RC.1 (kernel 6.18.52) | k6.18.52-memryx2.1-r51")

    def test_unknown_kernel_is_left_out(self):
        for params, prefix in ((STABLE, "Hardware test"), (PREVIEW, "Preview hardware test")):
            title = render_issue(**{**params, "kver": ""})["title"]
            self.assertEqual(title, f"{prefix}: MemryX MX3 driver SDK 2.1 | "
                                    f"TrueNAS {params['version']} | {params['tag']}")

    def test_open_issue_with_this_title_blocks_a_duplicate(self):
        # The step's own title must satisfy its duplicate check.
        for params in (STABLE, PREVIEW):
            title = render_issue(**params)["title"]
            self.assertTrue(title.endswith(f" | {params['tag']}"), title)
            self.assertIsNone(render_issue(**params, open_titles=[title]))


class Commands(unittest.TestCase):
    def test_downloads_are_release_assets(self):
        body = render_issue(**STABLE)["body"]
        loop = re.search(r"^for f in (.+?); do curl .*\$BASE/\$f", body, re.MULTILINE)
        self.assertIsNotNone(loop, body)
        files = set(loop.group(1).split())
        self.assertLessEqual({"install.sh", "memryx-lib.sh", "memryx.raw"}, files)
        self.assertLessEqual(files, release_assets())
        self.assertIn(f"BASE=https://github.com/truenas-community-sysexts/memryx-mx3-support"
                      f"/releases/download/{STABLE['tag']}", body)

    def test_install_flags_exist(self):
        flags = install_flags()
        for params in (STABLE, PREVIEW):
            body = render_issue(**params)["body"]
            used = set(re.findall(r"(--[a-z][a-z-]*)", body))
            self.assertTrue(used, body)
            self.assertLessEqual(used, flags)

    def test_kernel_precheck_names_the_target_kernel(self):
        for params in (STABLE, PREVIEW):
            body = render_issue(**params)["body"]
            self.assertRegex(body, rf"(?m)^uname -r +# must print {re.escape(params['kver'])}$")

    def test_requirements_name_the_kernel_and_the_train(self):
        # The installer serves the build to every version of the train on
        # that kernel and refuses it on any other kernel (install.sh's image
        # kernel check).
        for params in (STABLE, PREVIEW):
            body = render_issue(**params)["body"]
            reqs = body[body.index("### Requirements"):body.index("### 1. Pre-checks")]
            self.assertIn(f"any {params['train']} version running kernel `{params['kver']}`", reqs)
            self.assertIn("install.sh refuses this image on any other kernel", reqs)
            self.assertNotIn("installs anyway", reqs)
        self.assertIn("was not built for the running kernel", INSTALL_SH.read_text())

    def test_upgrade_note_quotes_what_the_scripts_print(self):
        for params in (STABLE, PREVIEW):
            body = render_issue(**params)["body"]
            self.assertIn("Upgrading over an earlier build for a different kernel", body)
            self.assertIn("`PREINIT logged an error this boot` with a "
                          "`Kernel version mismatch` message", body)
        self.assertIn("PREINIT logged an error this boot", INSTALL_SH.read_text())
        self.assertIn("Kernel version mismatch",
                      (ROOT / "scripts" / "memryx-preinit.sh").read_text())

    def test_sign_off_approves_the_build_for_its_train_and_kernel(self):
        for params in (STABLE, PREVIEW):
            body = render_issue(**params)["body"]
            works = next(ln for ln in body.splitlines() if ln.startswith("- Works:"))
            self.assertIn("promotes", works)
            self.assertIn("approves it for this TrueNAS train", works)
            self.assertIn(f"{params['train']} systems running kernel `{params['kver']}`", works)
            self.assertIn("newest signed-off build on any train", works)
        preview = render_issue(**PREVIEW)["body"]
        self.assertIn("stable boxes never install a preview build", preview)

    def test_broken_stable_build_is_deleted_so_its_kernel_can_rebuild(self):
        # An unpromoted build counts as pending coverage for its kernel in
        # check-releases, which holds back a rebuild.
        body = render_issue(**STABLE)["body"]
        broken = next(ln for ln in body.splitlines() if ln.startswith("- Broken:"))
        self.assertIn("delete the release", broken)
        self.assertIn("pending coverage for its kernel", broken)


if __name__ == "__main__":
    unittest.main()
