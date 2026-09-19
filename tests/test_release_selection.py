"""Unit tests for the release-selection logic embedded in scripts/install.sh.

The Python between the BEGIN/END release-selection sentinels is extracted
verbatim and run as a subprocess with a canned GitHub releases JSON on
stdin, exactly how install.sh runs it.

The selection sits in the shared approved-release block, which get.sh,
scripts/install.sh, scripts/uninstall.sh and scripts/restore.sh each carry
verbatim (all four are self-contained curl|bash scripts); SharedCopies fails
when the copies differ, and the shell functions of the block are run under
bash with stubbed commands."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from release_fixtures import marker, release

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"
UNINSTALL_SH = ROOT / "scripts" / "uninstall.sh"
RESTORE_SH = ROOT / "scripts" / "restore.sh"
GET_SH = ROOT / "get.sh"
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
TRACKED = ROOT / ".github" / "tracked-versions.json"
REPO = "truenas-community-sysexts/memryx-mx3-support"


def selection_snippet():
    text = INSTALL_SH.read_text()
    begin = text.index("# BEGIN release-selection")
    end = text.index("# END release-selection")
    return text[begin:end]


def shared_block(path):
    text = path.read_text()
    return text[text.index("# BEGIN approved-release"):
                text.index("# END approved-release")]


def run_block(commands, env=None, path=INSTALL_SH):
    """Run the shared block from `path`, then `commands`, under bash."""
    script = f"REPO={REPO}\n{shared_block(path)}\n{commands}\n"
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=dict(os.environ, **(env or {})))


def train_key(version):
    """truenas_train_key from the shared block, under bash."""
    p = run_block(f'truenas_train_key "{version}"')
    return p.stdout.strip() if p.returncode == 0 else None


def run_selection_raw(text, version, train=None, issues=None, mode="install"):
    # The train is what the scripts derive from the version, unless a test
    # gives one.
    if train is None:
        train = train_key(version) or ""
    env = {"MODE": mode, "VERSION": version, "TRAIN": train,
           "REPO": REPO, "PATH": "/usr/bin:/bin"}
    with tempfile.TemporaryDirectory() as d:
        if issues is not None:
            path = Path(d, "issues.json")
            path.write_text(issues if isinstance(issues, str)
                            else json.dumps(issues))
            env["ISSUES_FILE"] = str(path)
        return subprocess.run(
            ["python3", "-c", selection_snippet()],
            input=text, capture_output=True, text=True, env=env)


def run_selection(releases, version, train=None, issues=None, mode="install"):
    return run_selection_raw(json.dumps(releases), version, train, issues, mode)


K95 = "6.12.95-production+truenas"
K99 = "6.12.99-production+truenas"
K105 = "6.12.105-production+truenas"
K42 = "6.18.42-production+truenas"
STABLE = "25.10.7"
BETA = "26.0.0-BETA.3"


def stable_build(run, version=STABLE, **kw):
    return release(f"v{version}-memryx2.1-r{run}", version, kver=K105,
                   published=f"2026-09-{run:02d}T00:00:00Z", **kw)


def preview_build(run, version=BETA, **kw):
    return release(f"v{version}-memryx2.1-r{run}", version, "Halfmoon",
                   kver=K42, published=f"2026-09-{run:02d}T00:00:00Z",
                   prerelease=True, **kw)


RELEASES = [
    release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K95),
    release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K99),
    release("v25.10.5-memryx2.1-r11", "25.10.5", kver=K99, prerelease=True),
    # A preview build signed off by its preview hardware test: per-train
    # approval installs nothing unverified, on preview boxes too.
    release("v26.0.0-BETA.2-memryx2.1-r9", "26.0.0-BETA.2", "Halfmoon",
            kver=K42, prerelease=True, verified=["26"]),
]


class VersionMatch(unittest.TestCase):
    """The existing exact-version selection, unchanged by per-train approval."""

    def test_matches_the_boxs_exact_truenas_version(self):
        p = run_selection(RELEASES, "25.10.4")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")

    def test_another_point_release_is_not_a_match(self):
        # memryx is not kernel-keyed: a build serves the version it names.
        p = run_selection(RELEASES, "25.10.2")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("No approved stable release found for TrueNAS version "
                      "25.10.2.", p.stderr)

    def test_stable_box_never_gets_prerelease(self):
        p = run_selection(RELEASES, "25.10.5")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("No approved stable release found", p.stderr)

    def test_beta_tag_is_never_grandfathered_even_when_not_prerelease(self):
        # A mispublished full BETA/RC release must not count as "promoted
        # before per-train sign-off": it would then install on every train.
        # Only a verified-train marker approves a preview build.
        rels = [release("v26.0.0-RC.1-memryx2.1-r44", "26.0.0-RC.1", "Halfmoon",
                        kver=K42, prerelease=False)]
        for version in ("26.0.0-RC.1", "26.0.0"):
            p = run_selection(rels, version)
            self.assertNotEqual(p.returncode, 0, version)
        rels[0]["body"] += f"\n\n{marker('26')}\n"
        p = run_selection(rels, "26.0.0-RC.1")
        self.assertEqual(p.returncode, 0, p.stderr)
        # ... and a stable box on the same train still does not take it.
        p = run_selection(rels, "26.0.0")
        self.assertNotEqual(p.returncode, 0)

    def test_preview_box_gets_a_signed_off_prerelease(self):
        p = run_selection(RELEASES, "26.0.0-BETA.2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v26.0.0-BETA.2-memryx2.1-r9")

    def test_newest_of_several_builds_for_one_version_wins(self):
        rels = [release("v25.10.4-memryx2.1-r5", "25.10.4", kver=K99,
                        published="2026-06-12T00:00:00Z"),
                release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K99,
                        published="2026-06-14T00:00:00Z")]
        p = run_selection(rels, "25.10.4")
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")

    def test_no_match_lists_available_releases(self):
        p = run_selection(RELEASES, "25.10.9")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("v25.10.4-memryx2.1-r6", p.stderr)

    def test_draft_ignored(self):
        rels = RELEASES + [release("v25.10.9-memryx2.1-r99", "25.10.9",
                                   kver=K105, draft=True)]
        p = run_selection(rels, "25.10.9")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("v25.10.9-memryx2.1-r99", p.stderr)


class Pagination(unittest.TestCase):
    def test_concatenated_pages_are_merged(self):
        page1 = [release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K99)]
        page2 = [release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K95)]
        text = json.dumps(page1) + "\n" + json.dumps(page2) + "\n"
        p = run_selection_raw(text, "25.10.3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.3-memryx2.1-r10")

    def test_api_error_object_on_any_page_is_reported(self):
        text = json.dumps([]) + "\n" + json.dumps(
            {"message": "API rate limit exceeded for 1.2.3.4"}) + "\n"
        p = run_selection_raw(text, STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rate limit", p.stderr)

    def test_empty_input_is_a_parse_error(self):
        p = run_selection_raw("", STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Failed to parse", p.stderr)


class TemplateContract(unittest.TestCase):
    def test_header_matches_the_actual_build_yml_notes_header(self):
        # built_train() reads the TrueNAS version out of the notes header, so
        # rewording it in build.yml must break CI rather than silently make
        # every release trainless (which would strand every script-only run).
        rows = [line for line in BUILD_YML.read_text().splitlines()
                if "Sysext for TrueNAS SCALE" in line]
        self.assertEqual(len(rows), 1, rows)
        body = (rows[0].strip().replace("${TRUENAS_VERSION}", "25.10.9")
                .replace("${TRAIN_NAME}", "Goldeye"))
        rel = dict(release("v25.10.9-memryx2.1-r1", "25.10.9"), body=body)
        p = run_selection([rel], "25.10.9", mode="scripts")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.9-memryx2.1-r1")


class SharedCopies(unittest.TestCase):
    def test_block_is_identical_in_every_self_contained_script(self):
        block = shared_block(INSTALL_SH)
        for path in (GET_SH, UNINSTALL_SH, RESTORE_SH):
            self.assertEqual(shared_block(path), block, path.name)

    def test_selection_snippet_lives_in_the_shared_block(self):
        self.assertIn(selection_snippet(), shared_block(INSTALL_SH))


class Approval(unittest.TestCase):
    """The approved-for-train filter added to the version-matched selection."""

    def test_marker_for_the_train_is_approved_on_a_preview_box(self):
        p = run_selection([preview_build(15, verified=["26"]),
                           preview_build(13)], BETA)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v26.0.0-BETA.3-memryx2.1-r15")

    def test_marker_for_the_train_is_approved_on_a_stable_box(self):
        p = run_selection([stable_build(14, verified=["25.10"])], STABLE)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.7-memryx2.1-r14")

    def test_marker_for_another_train_only_is_rejected(self):
        # A 25.10 sign-off says nothing about 26, and the reverse.
        p = run_selection([stable_build(14, verified=["26"])], STABLE)
        self.assertNotEqual(p.returncode, 0)
        p = run_selection([preview_build(15, verified=["25.10"])], BETA)
        self.assertNotEqual(p.returncode, 0)

    def test_newest_approved_wins_over_a_newer_one_for_another_train(self):
        rels = [stable_build(16, verified=["26"]),
                stable_build(14, verified=["25.10"])]
        p = run_selection(rels, STABLE)
        self.assertEqual(p.stdout, "v25.10.7-memryx2.1-r14")

    def test_grandfathered_full_release_is_approved_for_every_train(self):
        # Promoted before per-train sign-off: no marker, not a prerelease.
        # Only the box on its own TrueNAS version installs it, but the
        # approval gate is what is under test here (see ScriptsOnly for the
        # cross-train rule).
        rel = release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K99)
        p = run_selection([rel], "25.10.4")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")
        p = run_selection([rel], "25.10.4", train="26", mode="scripts")
        self.assertEqual(p.returncode, 3)

    def test_prerelease_without_marker_is_rejected_on_a_stable_box(self):
        p = run_selection([stable_build(14, prerelease=True)], STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_prerelease_without_marker_is_rejected_on_a_preview_box(self):
        # The owner decision behind per-train approval: a beta box no longer
        # installs an unverified beta build straight away.
        p = run_selection([preview_build(15), preview_build(13)], BETA)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        rels = [dict(r, body=r["body"].replace(f"\n\n{marker('26')}\n", ""))
                for r in RELEASES]
        p = run_selection(rels, "26.0.0-BETA.2")
        self.assertNotEqual(p.returncode, 0)

    def test_signed_off_preview_prerelease_is_approved(self):
        p = run_selection([preview_build(13, verified=["26"]),
                           preview_build(15)], BETA)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v26.0.0-BETA.3-memryx2.1-r13")

    def test_stable_box_still_refuses_a_signed_off_preview_build(self):
        # The channel gate stays: a 26 stable box does not take the beta
        # build, even though train 26 approved it.
        p = run_selection([preview_build(15, verified=["26"])], "26.0.0",
                          mode="scripts")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("No stable release built for TrueNAS train 26", p.stderr)

    def test_draft_with_marker_is_rejected(self):
        p = run_selection([stable_build(14, verified=["25.10"], draft=True)],
                          STABLE)
        self.assertNotEqual(p.returncode, 0)

    def test_marker_must_start_a_line(self):
        # A changelog line quoting a marker is not an approval: promote.yml
        # writes the marker on a line of its own.
        rel = stable_build(14, prerelease=True)
        rel["body"] += "\n## Changelog\n* ci: add <!-- verified-train: 25.10 --> (#1)\n"
        p = run_selection([rel], STABLE)
        self.assertNotEqual(p.returncode, 0)

    def test_marker_spacing_and_crlf_are_tolerated(self):
        rel = preview_build(15)
        rel["body"] += "\r\n\r\n<!--verified-train:26-->\r\n"
        p = run_selection([rel], BETA)
        self.assertEqual(p.stdout, "v26.0.0-BETA.3-memryx2.1-r15")

    def test_marker_does_not_match_a_longer_or_shorter_train_key(self):
        for other in ("25.1", "25.100", "2"):
            p = run_selection([stable_build(14, verified=[other])], STABLE)
            self.assertNotEqual(p.returncode, 0, other)


class ScriptsOnly(unittest.TestCase):
    """Mode "scripts" (--check, --help, uninstall, a user's own image): the
    approved build for this TrueNAS version, else the newest approved release
    built for the box's own train, never one built for another train, not
    even a grandfathered one (its --check and restore.sh know another
    install)."""

    GRANDFATHERED_2510 = [
        release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K95,
                published="2026-06-26T00:00:00Z"),
        release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K99,
                published="2026-06-14T00:00:00Z")]

    def scripts(self, rels, version, **kw):
        return run_selection(rels, version, mode="scripts", **kw)

    def test_26_box_with_only_grandfathered_2510_releases_refuses(self):
        rels = self.GRANDFATHERED_2510 + [preview_build(15)]
        for version in (BETA, "26.0.1"):
            p = self.scripts(rels, version)
            self.assertEqual(p.returncode, 3, (version, p.stdout))
            self.assertEqual(p.stdout, "")
            self.assertIn("release built for TrueNAS train 26 is approved yet",
                          p.stderr)
            self.assertIn("--release=TAG", p.stderr)

    def test_26_box_names_the_waiting_test_for_its_train(self):
        issues = [{"number": 25,
                   "title": "Preview hardware test | v26.0.0-BETA.3-memryx2.1-r15",
                   "body": "<!-- release-tag: v26.0.0-BETA.3-memryx2.1-r15 -->",
                   "labels": [{"name": "preview-hardware-test"}],
                   "html_url": "https://example.test/issues/25"}]
        p = self.scripts(self.GRANDFATHERED_2510 + [preview_build(15)], BETA,
                         issues=issues)
        self.assertIn("  v26.0.0-BETA.3-memryx2.1-r15 (prerelease)", p.stderr)
        self.assertIn("#25 Preview hardware test", p.stderr)

    def test_26_box_with_a_26_approved_release_uses_it(self):
        rels = self.GRANDFATHERED_2510 + [preview_build(15, verified=["26"])]
        # On its own TrueNAS version, and on a later one of the same train.
        for version in (BETA, "26.0.0-BETA.4"):
            p = self.scripts(rels, version)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "v26.0.0-BETA.3-memryx2.1-r15")

    def test_2510_box_falls_back_to_the_newest_approved_of_its_train(self):
        rels = self.GRANDFATHERED_2510 + [stable_build(14, prerelease=True),
                                          preview_build(15, verified=["26"])]
        # Its own version's approved build if there is one, else the newest
        # approved release of 25.10 (grandfathered ones included).
        p = self.scripts(rels, "25.10.4")
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")
        p = self.scripts(rels, STABLE)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.3-memryx2.1-r10")

    def test_same_gates_as_install(self):
        # Unverified builds of the train never serve, and a stable box
        # still refuses preview builds.
        p = self.scripts([stable_build(14, prerelease=True)], STABLE)
        self.assertEqual(p.returncode, 3)
        p = self.scripts([preview_build(15, verified=["26"])], "26.0.1")
        self.assertEqual(p.returncode, 3)

    def test_train_is_read_from_the_tag_without_a_header(self):
        rel = dict(release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K99),
                   body="")
        p = self.scripts([rel], STABLE)
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")
        p = self.scripts([rel], "26.0.1")
        self.assertEqual(p.returncode, 3)


class NoCandidate(unittest.TestCase):
    ISSUES = [
        {"number": 22, "title": "Hardware test: MemryX MX3 driver SDK 2.1 | "
         "TrueNAS 25.10.7 (kernel 6.12.105) | v25.10.7-memryx2.1-r14",
         "body": "<!-- release-tag: v25.10.7-memryx2.1-r14 -->",
         "labels": [{"name": "hardware-test"}],
         "html_url": "https://github.com/x/y/issues/22"},
        {"number": 40, "title": "Some pull request touching r14",
         "body": "<!-- release-tag: v25.10.7-memryx2.1-r14 -->",
         "labels": [{"name": "hardware-test"}], "pull_request": {},
         "html_url": "https://github.com/x/y/pull/40"},
        {"number": 41, "title": "Unrelated", "body": "",
         "labels": [], "html_url": "https://github.com/x/y/issues/41"},
    ]

    def test_exit_code_three_means_nothing_approved(self):
        p = run_selection([stable_build(14, prerelease=True)], STABLE)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(p.stdout, "")

    def test_api_errors_are_exit_one(self):
        p = run_selection_raw(json.dumps({"message": "Not Found"}), STABLE)
        self.assertEqual(p.returncode, 1)

    def test_message_explains_the_approval_rule(self):
        p = run_selection([stable_build(14, prerelease=True)], STABLE)
        self.assertIn("No approved stable release found for TrueNAS version "
                      "25.10.7.", p.stderr)
        self.assertIn("Only a release approved for TrueNAS train 25.10 is installed",
                      p.stderr)

    def test_names_the_open_hardware_test_waiting(self):
        rels = [stable_build(14, prerelease=True), stable_build(12, prerelease=True)]
        p = run_selection(rels, STABLE, issues=self.ISSUES)
        self.assertEqual(p.returncode, 3)
        self.assertIn("  v25.10.7-memryx2.1-r14 (prerelease)", p.stderr)
        self.assertIn("  #22 Hardware test: MemryX MX3 driver", p.stderr)
        self.assertIn("https://github.com/x/y/issues/22", p.stderr)
        self.assertNotIn("#40", p.stderr)  # a pull request, not an issue
        self.assertNotIn("#41", p.stderr)

    def test_matches_an_issue_by_title_when_it_has_no_marker(self):
        issues = [dict(self.ISSUES[0], body="")]
        p = run_selection([stable_build(14, prerelease=True)], STABLE,
                          issues=issues)
        self.assertIn("#22", p.stderr)

    def test_says_when_no_issue_is_open_for_the_waiting_builds(self):
        p = run_selection([preview_build(15), preview_build(13)], BETA,
                          issues=self.ISSUES)
        self.assertIn("awaiting its preview hardware", p.stderr)
        self.assertIn("  v26.0.0-BETA.3-memryx2.1-r15 (prerelease)", p.stderr)
        self.assertIn("No hardware-test issue is open for these builds yet.",
                      p.stderr)

    def test_says_when_no_build_exists_to_test(self):
        p = run_selection([stable_build(14, verified=["25.10"])], "25.10.9",
                          issues=[])
        self.assertIn("No build for TrueNAS 25.10.9 is waiting for a hardware "
                      "test, so no hardware-test issue", p.stderr)
        self.assertIn("exists for it yet.", p.stderr)

    def test_pending_hint_excludes_preview_builds_on_a_stable_box(self):
        # A preview build is never promoted, so a stable box must not be
        # promised an install "once promoted".
        p = run_selection([preview_build(15)], "26.0.0-BETA.3", train="26",
                          mode="scripts")
        self.assertNotEqual(p.returncode, 0)
        p = run_selection([preview_build(15)], "26.0.0", mode="scripts")
        self.assertNotIn("awaiting hardware-test", p.stderr)

    def test_without_the_issue_list_it_links_the_open_tests(self):
        for issues in (None, "not json", {"message": "API rate limit exceeded"}):
            p = run_selection([preview_build(15)], BETA, issues=issues)
            self.assertIn(f"https://github.com/{REPO}/issues?q=is%3Aissue+is%3Aopen"
                          "+label%3Apreview-hardware-test", p.stderr, issues)
        p = run_selection([stable_build(14, prerelease=True)], STABLE)
        self.assertIn("label%3Ahardware-test", p.stderr)

    def test_waiting_builds_are_newest_first_and_capped(self):
        rels = [stable_build(n, prerelease=True) for n in range(1, 9)]
        p = run_selection(rels, STABLE)
        waiting = p.stderr[p.stderr.index("Builds waiting"):
                           p.stderr.index("Otherwise")]
        tags = [ln.split()[0] for ln in waiting.splitlines()[1:]
                if ln.startswith("  v")]
        self.assertEqual(tags, [f"v25.10.7-memryx2.1-r{n}"
                                for n in (8, 7, 6, 5, 4)])

    def test_approved_builds_for_another_train_are_listed_as_waiting(self):
        p = run_selection([stable_build(14, verified=["26"])], STABLE)
        self.assertIn("  v25.10.7-memryx2.1-r14\n", p.stderr)


class TrainKey(unittest.TestCase):
    def test_before_26_the_train_is_major_minor(self):
        self.assertEqual(train_key("25.10.7"), "25.10")
        self.assertEqual(train_key("25.10.4"), "25.10")
        self.assertEqual(train_key("25.10.3.1"), "25.10")
        self.assertEqual(train_key("25.04.2.6"), "25.04")
        self.assertEqual(train_key("25.10-RC.1"), "25.10")

    def test_from_26_the_train_is_the_major(self):
        self.assertEqual(train_key("26.0.0-BETA.3"), "26")
        self.assertEqual(train_key("26.0.0-RC.1"), "26")
        self.assertEqual(train_key("26.1.2"), "26")
        self.assertEqual(train_key("27.0.0-BETA.1"), "27")

    def test_garbage_has_no_train(self):
        for v in ("", "abc", "25", "25.", "x25.10"):
            self.assertIsNone(train_key(v), v)

    def test_tracked_versions_have_a_train(self):
        tracked = json.loads(TRACKED.read_text())
        self.assertEqual(train_key(tracked["truenas"]["version"]), "25.10")
        self.assertEqual(train_key(tracked["truenas_preview"]["version"]), "26")


STUB = """
curl() {{
    local url="${{*: -1}}" n
    echo "$url" >> "{d}/calls"
    case "$url" in
        *"/issues?"*) cat "{d}/issues.json" 2>/dev/null || return 7 ;;
        *) n="${{url##*page=}}"; cat "{d}/page$n.json" 2>/dev/null || echo '[]' ;;
    esac
}}
midclt() {{ echo '{{"version": "{version}"}}'; }}
"""


class ApprovedReleaseTag(unittest.TestCase):
    """approved_release_tag: the shell side of the shared block."""

    def run_tag(self, pages, version, issues=None, args=""):
        with tempfile.TemporaryDirectory() as d:
            for i, page in enumerate(pages, 1):
                Path(d, f"page{i}.json").write_text(json.dumps(page))
            if issues is not None:
                Path(d, "issues.json").write_text(json.dumps(issues))
            p = run_block(STUB.format(d=d, version=version)
                          + f"approved_release_tag {args}")
            calls = Path(d, "calls").read_text().split() if Path(d, "calls").exists() else []
            return p, calls

    def test_fetch_loop_reads_every_page(self):
        page1 = [stable_build(1, prerelease=True) for _ in range(100)]
        page2 = [release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K95)]
        p, calls = self.run_tag([page1, page2], "25.10.3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "v25.10.3-memryx2.1-r10")
        self.assertEqual([c.rsplit("page=", 1)[1] for c in calls], ["1", "2"])
        self.assertIn("Found release: v25.10.3-memryx2.1-r10 (approved for "
                      "TrueNAS train 25.10)", p.stderr)

    def test_success_does_not_query_issues(self):
        p, calls = self.run_tag([[release("v25.10.3-memryx2.1-r10", "25.10.3",
                                          kver=K95)]], "25.10.3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(any("/issues?" in c for c in calls), calls)

    def test_nothing_approved_names_the_waiting_issue(self):
        p, calls = self.run_tag([[stable_build(14, prerelease=True)]], STABLE,
                                issues=NoCandidate.ISSUES)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        self.assertIn("#22 Hardware test", p.stderr)
        self.assertEqual(p.stderr.count("No approved stable release found"), 1)
        self.assertTrue(any("/issues?state=open" in c for c in calls), calls)

    def test_issue_lookup_failure_still_explains(self):
        p, _ = self.run_tag([[stable_build(14, prerelease=True)]], STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("label%3Ahardware-test", p.stderr)

    def test_scripts_only_mode_takes_the_trains_release(self):
        rels = [release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K95)]
        p, _ = self.run_tag([rels], STABLE, args="--scripts-only")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "v25.10.3-memryx2.1-r10")
        self.assertIn("Detected TrueNAS version: 25.10.7 (train 25.10)", p.stderr)
        self.assertIn("Searching for an approved release of this train", p.stderr)

    def test_api_failure_is_an_error_not_a_fallback(self):
        p = run_block("""
curl() { return 7; }
midclt() { echo '{"version": "26.0.0-BETA.3"}'; }
approved_release_tag
""")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        self.assertIn("Failed to query GitHub releases", p.stderr)

    def test_unreadable_version_is_an_error(self):
        p = run_block("""
midclt() { return 1; }
approved_release_tag
""")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Failed to detect TrueNAS version", p.stderr)

    def test_version_without_a_train_is_an_error(self):
        p = run_block("""
midclt() { echo '{"version": "MASTER"}'; }
approved_release_tag
""")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("cannot derive a TrueNAS train from version 'MASTER'",
                      p.stderr)


class SnippetBashSafety(unittest.TestCase):
    def test_snippet_survives_double_quote_expansion(self):
        # The snippet lives inside a double-quoted bash string; bash rewrites
        # $..., backticks, and backslash-before-special before Python ever
        # runs. The extraction test runs the raw text, so any such character
        # would make production execute different code than the tests.
        snip = selection_snippet()
        self.assertNotIn("$", snip)
        self.assertNotIn('"', snip)
        self.assertNotIn(chr(96), snip)  # backtick
        for i, ch in enumerate(snip):
            if ch == "\\":
                self.assertNotIn(snip[i + 1], "$\"\\\n" + chr(96),
                                 f"bash-active backslash escape at offset {i}")


if __name__ == "__main__":
    unittest.main()
