"""Unit tests for the release-selection logic embedded in scripts/install.sh.

The Python between the BEGIN/END release-selection sentinels is extracted
verbatim and run as a subprocess with a canned GitHub releases JSON on
stdin, exactly how install.sh runs it.

The selection sits in the shared approved-release block, which get.sh,
scripts/install.sh, scripts/uninstall.sh and scripts/restore.sh each carry
verbatim (all four are self-contained curl|bash scripts); SharedCopies fails
when the copies differ, and the shell functions of the block are run under
bash with stubbed commands."""
import functools
import json
import os
import re
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


@functools.lru_cache(maxsize=None)
def train_key(version):
    """truenas_train_key from the shared block, under bash."""
    p = run_block(f'truenas_train_key "{version}"')
    return p.stdout.strip() if p.returncode == 0 else None


def run_selection_raw(text, version, kver, train=None, issues=None,
                      mode="install"):
    # The train is what the scripts derive from the version, unless a test
    # gives one.
    if train is None:
        train = train_key(version) or ""
    env = {"MODE": mode, "VERSION": version, "KVER": kver, "TRAIN": train,
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


def run_selection(releases, version, kver, train=None, issues=None,
                  mode="install"):
    return run_selection_raw(json.dumps(releases), version, kver, train,
                             issues, mode)


K15 = "6.12.15-production+truenas"
K33 = "6.12.33-production+truenas"
K91 = "6.12.91-production+truenas"
K95 = "6.12.95-production+truenas"
K99 = "6.12.99-production+truenas"
K105 = "6.12.105-production+truenas"
K23 = "6.18.23-production+truenas"
K42 = "6.18.42-production+truenas"
STABLE = ("25.10.7", K105)
BETA = ("26.0.0-BETA.3", K42)


def short(kver):
    return kver.split("-")[0]


def stable_build(run, version="25.10.7", kver=K105, **kw):
    return release(f"k{short(kver)}-memryx2.1-r{run}", version, kver=kver,
                   published=f"2026-09-{run:02d}T00:00:00Z", **kw)


def preview_build(run, version="26.0.0-BETA.3", kver=K42, **kw):
    return release(f"k{short(kver)}-memryx2.1-r{run}", version, "Halfmoon",
                   kver=kver, published=f"2026-09-{run:02d}T00:00:00Z",
                   prerelease=True, **kw)


RELEASES = [
    release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K33),
    release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K91),
    release("v25.10.5-memryx2.1-r11", "25.10.5", kver=K95, prerelease=True),
    # A preview build signed off by its preview hardware test: per-train
    # approval installs nothing unverified, on preview boxes too.
    release("v26.0.0-BETA.2-memryx2.1-r9", "26.0.0-BETA.2", "Halfmoon",
            kver=K23, prerelease=True, verified=["26"]),
    # Published before the Target kernel row existed (legacy fixture).
    release("v25.04.1-memryx2.0-r5", "25.04.1", "Fangtooth"),
]


class KernelMatch(unittest.TestCase):
    def test_unbuilt_point_release_matches_by_kernel(self):
        # 25.10.2 never had a build of its own; it ships the 25.10.3 kernel.
        p = run_selection(RELEASES, "25.10.2", K33)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.3-memryx2.1-r10")

    def test_the_version_a_build_names_still_matches(self):
        p = run_selection(RELEASES, "25.10.4", K91)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")

    def test_stable_box_never_gets_prerelease(self):
        p = run_selection(RELEASES, "25.10.5", K95)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("No approved stable release found for kernel " + K95,
                      p.stderr)

    def test_stable_box_refuses_beta_tag_even_when_not_prerelease(self):
        # The prerelease flag can be mispublished (a manual edit, say). The
        # tag is the second lock on the stable channel.
        rels = [release("v26.0.0-RC.1-memryx2.1-r44", "26.0.0-RC.1",
                        kver=K95, prerelease=False, verified=["25.10"])]
        p = run_selection(rels, "25.10.5", K95)
        self.assertNotEqual(p.returncode, 0)

    def test_stable_box_refuses_ktagged_preview_via_body_header(self):
        # Kernel-keyed tags carry no BETA marker, so the second lock must
        # read the notes header instead of the tag.
        rels = [release("k6.12.95-memryx2.1-r44", "26.0.0-RC.1", kver=K95,
                        prerelease=False, verified=["25.10"])]
        p = run_selection(rels, "25.10.5", K95)
        self.assertNotEqual(p.returncode, 0)

    def test_preview_box_gets_a_signed_off_prerelease(self):
        p = run_selection(RELEASES, "26.0.0-BETA.2", K23)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v26.0.0-BETA.2-memryx2.1-r9")

    def test_ktag_matches_when_body_row_is_lost(self):
        # check-releases' coverage gate counts a k-tag as covering its kernel
        # even when the body lost the Target kernel row; the installer must
        # agree, or the gate skips builds for a kernel the installer then
        # refuses to serve.
        rel = dict(release("k6.12.91-memryx2.1-r50", "25.10.4"), body="")
        p = run_selection([rel], "25.10.4", K91)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "k6.12.91-memryx2.1-r50")

    def test_ktag_fallback_never_matches_a_different_short_kernel(self):
        rel = dict(release("k6.12.9-memryx2.1-r50", "25.10.4"), body="")
        p = run_selection([rel], "25.10.4", K91)
        self.assertNotEqual(p.returncode, 0)

    def test_body_row_overrides_ktag_on_mismatch(self):
        # A mispublished release whose body advertises a different kernel
        # than its tag must not be served: the body row is written from
        # REAL_KVER at build time and stays authoritative.
        rel = release("k6.12.91-memryx2.1-r50", "25.10.3", kver=K33)
        p = run_selection([rel], "25.10.4", K91)
        self.assertNotEqual(p.returncode, 0)

    def test_version_fallback_for_legacy_release(self):
        p = run_selection(RELEASES, "25.04.1", K15)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.04.1-memryx2.0-r5")
        self.assertIn("matched by TrueNAS version", p.stderr)

    def test_fallback_never_picks_wrong_advertised_kernel(self):
        # A release advertising a DIFFERENT kernel must not win on version
        # prefix: a module built for another kernel cannot load.
        rels = [release("v25.10.9-memryx2.1-r50", "25.10.9", kver=K91)]
        p = run_selection(rels, "25.10.9", K99)
        self.assertNotEqual(p.returncode, 0)

    def test_newest_of_several_builds_for_one_kernel_wins(self):
        rels = [release("v25.10.4-memryx2.1-r5", "25.10.4", kver=K91,
                        published="2026-06-12T00:00:00Z"),
                release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K91,
                        published="2026-06-14T00:00:00Z"),
                stable_build(20, "25.10.4", K91, verified=["25.10"])]
        p = run_selection(rels, "25.10.4", K91)
        self.assertEqual(p.stdout, "k6.12.91-memryx2.1-r20")

    def test_no_match_lists_available_releases(self):
        p = run_selection(RELEASES, "25.10.9", K99)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(f"  v25.10.4-memryx2.1-r6 ({K91})", p.stderr)
        self.assertIn("  v25.04.1-memryx2.0-r5 (no kernel recorded)", p.stderr)

    def test_draft_ignored(self):
        rels = RELEASES + [release("v25.10.9-memryx2.1-r99", "25.10.9",
                                   kver=K99, draft=True)]
        p = run_selection(rels, "25.10.9", K99)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("v25.10.9-memryx2.1-r99", p.stderr)


class TrainGuard(unittest.TestCase):
    # The sysext ships userspace (libmemx, mx_accl, mxa_manager) staged on a
    # runner matched to a train's base system, so a kernel match from another
    # train must never be served.

    def test_same_train_other_version_matches(self):
        rels = [release("v25.10.3.1-memryx2.1-r41", "25.10.3.1", kver=K33)]
        p = run_selection(rels, "25.10.2", K33)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.3.1-memryx2.1-r41")

    def test_cross_train_kernel_match_refused(self):
        rels = [release("v25.04.2-memryx2.1-r42", "25.04.2", "Fangtooth",
                        kver=K33)]
        p = run_selection(rels, "25.10.2", K33)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("WARNING: v25.04.2-memryx2.1-r42 matches kernel " + K33,
                      p.stderr)
        self.assertIn("different TrueNAS train", p.stderr)

    def test_scripts_mode_does_not_warn(self):
        rels = [release("v25.04.2-memryx2.1-r42", "25.04.2", "Fangtooth",
                        kver=K33)]
        p = run_selection(rels, "25.10.2", K33, mode="scripts")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("different TrueNAS train", p.stderr)

    def test_ktag_without_a_header_passes_the_guard(self):
        rel = stable_build(43, "25.10.3", K33)
        rel["body"] = ("| Field | Value |\n| --- | --- |\n"
                       f"| Target kernel | `{K33}` |\n")
        p = run_selection([rel], "25.10.2", K33)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "k6.12.33-memryx2.1-r43")

    def test_vtag_without_a_header_takes_its_train_from_the_tag(self):
        rel = release("v25.04.2-memryx2.1-r42", "25.04.2", kver=K33)
        rel["body"] = f"| Target kernel | `{K33}` |\n"
        p = run_selection([rel], "25.10.2", K33)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("different TrueNAS train", p.stderr)


class TrainGuardKey(unittest.TestCase):
    # All of 26.x is one train, so 26.0 and 26.1 do not split; 25.04, 25.10
    # and 26 do.

    def test_26_0_and_26_1_are_the_same_train(self):
        rels = [release("k6.18.42-memryx2.1-r60", "26.1.0", "Halfmoon",
                        kver=K42, verified=["26"])]
        p = run_selection(rels, "26.0.0", K42)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "k6.18.42-memryx2.1-r60")
        self.assertNotIn("different TrueNAS train", p.stderr)

    def test_beta_and_final_of_26_are_the_same_train(self):
        rels = [release("k6.18.42-memryx2.1-r60", "26.0.1", "Halfmoon",
                        kver=K42, verified=["26"])]
        p = run_selection(rels, *BETA)
        self.assertEqual(p.stdout, "k6.18.42-memryx2.1-r60")

    def test_25_10_and_26_are_different_trains(self):
        # Grandfathered (approved for every train) and on the box's kernel:
        # only the train guard can refuse it.
        rels = [release("k6.18.42-memryx2.1-r61", "25.10.9", kver=K42)]
        p = run_selection(rels, "26.0.0", K42)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("different TrueNAS train", p.stderr)
        rels = [release("k6.18.42-memryx2.1-r61", "26.0.0", "Halfmoon",
                        kver=K42)]
        p = run_selection(rels, "25.10.9", K42)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("different TrueNAS train", p.stderr)

    def test_25_04_and_25_10_stay_different_trains(self):
        rels = [release("v25.04.2-memryx2.1-r42", "25.04.2", "Fangtooth",
                        kver=K105)]
        p = run_selection(rels, *STABLE)
        self.assertIn("different TrueNAS train", p.stderr)


class KernelStaysPrimary(unittest.TestCase):
    def test_approved_release_for_another_kernel_is_rejected(self):
        rels = [preview_build(38, "26.0.0-BETA.2", K23, verified=["26"])]
        p = run_selection(rels, *BETA)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_grandfathered_release_for_another_kernel_is_rejected(self):
        rels = [release("v25.10.5-memryx2.1-r40", "25.10.5", kver=K95)]
        p = run_selection(rels, *STABLE)
        self.assertNotEqual(p.returncode, 0)


class Pagination(unittest.TestCase):
    def test_concatenated_pages_are_merged(self):
        page1 = [release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K91)]
        page2 = [release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K33)]
        text = json.dumps(page1) + "\n" + json.dumps(page2) + "\n"
        p = run_selection_raw(text, "25.10.3", K33)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.3-memryx2.1-r10")

    def test_api_error_object_on_any_page_is_reported(self):
        text = json.dumps([]) + "\n" + json.dumps(
            {"message": "API rate limit exceeded for 1.2.3.4"}) + "\n"
        p = run_selection_raw(text, *STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rate limit", p.stderr)

    def test_empty_input_is_a_parse_error(self):
        p = run_selection_raw("", *STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Failed to parse", p.stderr)


class TemplateContract(unittest.TestCase):
    def test_header_matches_the_actual_build_yml_notes_header(self):
        # built_train() reads the TrueNAS version out of the notes header, so
        # rewording it in build.yml must break CI rather than silently make
        # every release trainless (which would strand every script-only run
        # and open the train guard).
        rows = [line for line in BUILD_YML.read_text().splitlines()
                if "Sysext for TrueNAS SCALE" in line]
        self.assertEqual(len(rows), 1, rows)
        body = (rows[0].strip().replace("${TRUENAS_VERSION}", "25.10.9")
                .replace("${TRAIN_NAME}", "Goldeye"))
        rel = dict(release("k6.12.99-memryx2.1-r1", "25.10.9"), body=body)
        p = run_selection([rel], "25.10.9", K105, mode="scripts")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "k6.12.99-memryx2.1-r1")

    def test_regex_matches_the_actual_build_yml_kernel_row(self):
        # The Target kernel row is the installer's primary match key. Feed
        # the selection a body built from the very template line build.yml
        # renders, so rewording the notes breaks CI instead of silently
        # reverting every new release to the legacy fallback.
        rows = [line for line in BUILD_YML.read_text().splitlines()
                if "| Target kernel |" in line]
        self.assertEqual(len(rows), 1, rows)
        body = rows[0].strip().replace("${REAL_KVER}", K99)
        rel = dict(release("v25.10.9-memryx2.1-r1", "25.10.9"), body=body)
        p = run_selection([rel], "25.10.9", K99)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.9-memryx2.1-r1")
        self.assertNotIn("matched by TrueNAS version", p.stderr)


class SharedCopies(unittest.TestCase):
    def test_block_is_identical_in_every_self_contained_script(self):
        block = shared_block(INSTALL_SH)
        for path in (GET_SH, UNINSTALL_SH, RESTORE_SH):
            self.assertEqual(shared_block(path), block, path.name)

    def test_selection_snippet_lives_in_the_shared_block(self):
        self.assertIn(selection_snippet(), shared_block(INSTALL_SH))


class Approval(unittest.TestCase):
    """The approved-for-train filter on top of the kernel match."""

    def test_marker_for_the_train_is_approved_on_a_preview_box(self):
        p = run_selection([preview_build(15, verified=["26"]),
                           preview_build(13)], *BETA)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "k6.18.42-memryx2.1-r15")

    def test_marker_for_the_train_is_approved_on_a_stable_box(self):
        p = run_selection([stable_build(14, verified=["25.10"])], *STABLE)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "k6.12.105-memryx2.1-r14")

    def test_marker_for_another_train_only_is_rejected(self):
        # A 25.10 sign-off says nothing about 26, and the reverse. A marker
        # also narrows a full release to the trains it names: it is no longer
        # grandfathered for every train.
        p = run_selection([stable_build(14, verified=["26"])], *STABLE)
        self.assertNotEqual(p.returncode, 0)
        p = run_selection([preview_build(15, verified=["25.10"])], *BETA)
        self.assertNotEqual(p.returncode, 0)

    def test_newest_approved_wins_over_a_newer_one_for_another_train(self):
        rels = [stable_build(16, verified=["26"]),
                stable_build(14, verified=["25.10"])]
        p = run_selection(rels, *STABLE)
        self.assertEqual(p.stdout, "k6.12.105-memryx2.1-r14")

    def test_newest_approved_wins_over_a_newer_unapproved_one(self):
        rels = [stable_build(16, prerelease=True),
                release("v25.10.7-memryx2.1-r12", "25.10.7", kver=K105,
                        published="2026-09-12T00:00:00Z"),
                stable_build(14, verified=["25.10"])]
        p = run_selection(rels, *STABLE)
        self.assertEqual(p.stdout, "k6.12.105-memryx2.1-r14")

    def test_grandfathered_full_release_is_approved_for_every_train(self):
        # Promoted before per-train sign-off: no marker, not a prerelease.
        # Only a box of its own train installs it (the train guard), but the
        # approval gate is what is under test here.
        rel = release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K91)
        p = run_selection([rel], "25.10.4", K91)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")
        p = run_selection([rel], "25.10.4", K91, train="26", mode="scripts")
        self.assertEqual(p.returncode, 3)

    def test_beta_build_is_never_grandfathered_even_when_not_prerelease(self):
        # A mispublished full BETA/RC release must not count as "promoted
        # before per-train sign-off": it would then install on every train.
        # Only a verified-train marker approves a preview build.
        rels = [release("k6.18.42-memryx2.1-r44", "26.0.0-RC.1", "Halfmoon",
                        kver=K42, prerelease=False)]
        for version in ("26.0.0-RC.1", "26.0.0"):
            p = run_selection(rels, version, K42)
            self.assertNotEqual(p.returncode, 0, version)
        rels[0]["body"] += f"\n\n{marker('26')}\n"
        p = run_selection(rels, "26.0.0-RC.1", K42)
        self.assertEqual(p.returncode, 0, p.stderr)
        # ... and a stable box on the same train still does not take it.
        p = run_selection(rels, "26.0.0", K42)
        self.assertNotEqual(p.returncode, 0)

    def test_prerelease_without_marker_is_rejected_on_a_stable_box(self):
        p = run_selection([stable_build(14, prerelease=True)], *STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_prerelease_without_marker_is_rejected_on_a_preview_box(self):
        # The owner decision behind per-train approval: a beta box no longer
        # installs an unverified beta build straight away.
        p = run_selection([preview_build(15), preview_build(13)], *BETA)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        rels = [dict(r, body=r["body"].replace(f"\n\n{marker('26')}\n", ""))
                for r in RELEASES]
        p = run_selection(rels, "26.0.0-BETA.2", K23)
        self.assertNotEqual(p.returncode, 0)

    def test_signed_off_preview_prerelease_is_approved(self):
        p = run_selection([preview_build(13, verified=["26"]),
                           preview_build(15)], *BETA)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "k6.18.42-memryx2.1-r13")

    def test_stable_box_still_refuses_a_signed_off_preview_build(self):
        # The channel gate stays: a 26 stable box on the same kernel does not
        # take the beta build, even though train 26 approved it.
        rels = [preview_build(15, verified=["26"])]
        p = run_selection(rels, "26.0.0", K42)
        self.assertNotEqual(p.returncode, 0)
        p = run_selection(rels, "26.0.0", K42, mode="scripts")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("No stable release built for TrueNAS train 26", p.stderr)

    def test_draft_with_marker_is_rejected(self):
        p = run_selection([stable_build(14, verified=["25.10"], draft=True)],
                          *STABLE)
        self.assertNotEqual(p.returncode, 0)

    def test_marker_must_start_a_line(self):
        # A changelog line quoting a marker is not an approval: promote.yml
        # writes the marker on a line of its own.
        rel = stable_build(14, prerelease=True)
        rel["body"] += "\n## Changelog\n* ci: add <!-- verified-train: 25.10 --> (#1)\n"
        p = run_selection([rel], *STABLE)
        self.assertNotEqual(p.returncode, 0)

    def test_marker_spacing_and_crlf_are_tolerated(self):
        rel = preview_build(15)
        rel["body"] = rel["body"].replace("\n", "\r\n") + "\r\n<!--verified-train:26-->\r\n"
        p = run_selection([rel], *BETA)
        self.assertEqual(p.stdout, "k6.18.42-memryx2.1-r15")

    def test_marker_does_not_match_a_longer_or_shorter_train_key(self):
        for other in ("25.1", "25.100", "2"):
            p = run_selection([stable_build(14, verified=[other])], *STABLE)
            self.assertNotEqual(p.returncode, 0, other)


class ScriptsOnly(unittest.TestCase):
    """Mode "scripts" (--check, --help, uninstall, a user's own image): the
    approved build for this kernel, else the newest approved release built
    for the box's own train, never one built for another train, not even a
    grandfathered one (its --check and restore.sh know another install)."""

    GRANDFATHERED_2510 = [
        release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K33,
                published="2026-06-26T00:00:00Z"),
        release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K91,
                published="2026-06-14T00:00:00Z")]

    def scripts(self, rels, version, kver, **kw):
        return run_selection(rels, version, kver, mode="scripts", **kw)

    def test_26_box_with_only_grandfathered_2510_releases_refuses(self):
        rels = self.GRANDFATHERED_2510 + [preview_build(15)]
        for version in ("26.0.0-BETA.3", "26.0.1"):
            p = self.scripts(rels, version, K42)
            self.assertEqual(p.returncode, 3, (version, p.stdout))
            self.assertEqual(p.stdout, "")
            self.assertIn("release built for TrueNAS train 26 is approved yet",
                          p.stderr)
            self.assertIn("--release=TAG", p.stderr)

    def test_26_box_names_the_waiting_test_for_its_train(self):
        issues = [{"number": 25,
                   "title": "Preview hardware test | k6.18.42-memryx2.1-r15",
                   "body": "<!-- release-tag: k6.18.42-memryx2.1-r15 -->",
                   "labels": [{"name": "preview-hardware-test"}],
                   "html_url": "https://example.test/issues/25"}]
        p = self.scripts(self.GRANDFATHERED_2510 + [preview_build(15)], *BETA,
                         issues=issues)
        self.assertIn("  k6.18.42-memryx2.1-r15 (prerelease)", p.stderr)
        self.assertIn("#25 Preview hardware test", p.stderr)

    def test_26_box_with_a_26_approved_release_uses_it(self):
        rels = self.GRANDFATHERED_2510 + [preview_build(15, verified=["26"])]
        # On its own kernel, and on a later version of the same train with
        # another kernel.
        for version, kver in (BETA, ("26.0.0-BETA.4", "6.18.50-production+truenas")):
            p = self.scripts(rels, version, kver)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "k6.18.42-memryx2.1-r15")

    def test_2510_box_prefers_its_kernel_else_the_newest_of_its_train(self):
        rels = self.GRANDFATHERED_2510 + [stable_build(14, prerelease=True),
                                          preview_build(15, verified=["26"])]
        # The approved build for its kernel if there is one, else the newest
        # approved release of 25.10 (grandfathered ones included).
        p = self.scripts(rels, "25.10.4", K91)
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")
        p = self.scripts(rels, *STABLE)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "v25.10.3-memryx2.1-r10")

    def test_same_gates_as_install(self):
        # Unverified builds of the train never serve, and a stable box
        # still refuses preview builds.
        p = self.scripts([stable_build(14, prerelease=True)], *STABLE)
        self.assertEqual(p.returncode, 3)
        p = self.scripts([preview_build(15, verified=["26"])], "26.0.1", K42)
        self.assertEqual(p.returncode, 3)

    def test_train_is_read_from_a_vtag_without_a_header(self):
        rel = dict(release("v25.10.4-memryx2.1-r6", "25.10.4", kver=K91),
                   body="")
        p = self.scripts([rel], *STABLE)
        self.assertEqual(p.stdout, "v25.10.4-memryx2.1-r6")
        p = self.scripts([rel], "26.0.1", K42)
        self.assertEqual(p.returncode, 3)

    def test_ktag_without_a_header_is_no_fallback(self):
        # A k-tag names no TrueNAS version, so without the header the
        # train-wide fallback cannot tell its train.
        rel = stable_build(40, "25.10.5", K95)
        rel["body"] = f"| Target kernel | `{K95}` |\n"
        p = self.scripts([rel], *STABLE)
        self.assertNotEqual(p.returncode, 0)

    def test_install_mode_never_falls_back_to_another_kernel(self):
        rels = [release("v25.10.5-memryx2.1-r40", "25.10.5", kver=K95)]
        p = run_selection(rels, *STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("--release=TAG", p.stderr)


class NoCandidate(unittest.TestCase):
    ISSUES = [
        {"number": 22, "title": "Hardware test: MemryX MX3 driver SDK 2.1 | "
         "TrueNAS 25.10.7 (kernel 6.12.105) | k6.12.105-memryx2.1-r14",
         "body": "<!-- release-tag: k6.12.105-memryx2.1-r14 -->",
         "labels": [{"name": "hardware-test"}],
         "html_url": "https://github.com/x/y/issues/22"},
        {"number": 40, "title": "Some pull request touching r14",
         "body": "<!-- release-tag: k6.12.105-memryx2.1-r14 -->",
         "labels": [{"name": "hardware-test"}], "pull_request": {},
         "html_url": "https://github.com/x/y/pull/40"},
        {"number": 41, "title": "Unrelated", "body": "",
         "labels": [], "html_url": "https://github.com/x/y/issues/41"},
    ]

    def test_exit_code_three_means_nothing_approved(self):
        p = run_selection([stable_build(14, prerelease=True)], *STABLE)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(p.stdout, "")

    def test_api_errors_are_exit_one(self):
        p = run_selection_raw(json.dumps({"message": "Not Found"}), *STABLE)
        self.assertEqual(p.returncode, 1)

    def test_message_explains_the_approval_rule(self):
        p = run_selection([stable_build(14, prerelease=True)], *STABLE)
        self.assertIn("No approved stable release found for kernel "
                      f"{K105} (TrueNAS 25.10.7).", p.stderr)
        self.assertIn("Only a release approved for TrueNAS train 25.10 is installed",
                      p.stderr)

    def test_names_the_open_hardware_test_waiting(self):
        rels = [stable_build(14, prerelease=True),
                stable_build(12, prerelease=True)]
        p = run_selection(rels, *STABLE, issues=self.ISSUES)
        self.assertEqual(p.returncode, 3)
        self.assertIn(f"A build for kernel {K105} exists but is a prerelease",
                      p.stderr)
        self.assertIn("  k6.12.105-memryx2.1-r14 (prerelease)", p.stderr)
        self.assertIn("  #22 Hardware test: MemryX MX3 driver", p.stderr)
        self.assertIn("https://github.com/x/y/issues/22", p.stderr)
        self.assertNotIn("#40", p.stderr)  # a pull request, not an issue
        self.assertNotIn("#41", p.stderr)

    def test_matches_an_issue_by_title_when_it_has_no_marker(self):
        issues = [dict(self.ISSUES[0], body="")]
        p = run_selection([stable_build(14, prerelease=True)], *STABLE,
                          issues=issues)
        self.assertIn("#22", p.stderr)

    def test_says_when_no_issue_is_open_for_the_waiting_builds(self):
        p = run_selection([preview_build(15), preview_build(13)], *BETA,
                          issues=self.ISSUES)
        self.assertIn("awaiting its preview hardware", p.stderr)
        self.assertIn("  k6.18.42-memryx2.1-r15 (prerelease)", p.stderr)
        self.assertIn("No hardware-test issue is open for these builds yet.",
                      p.stderr)

    def test_says_when_no_build_exists_to_test(self):
        k = "6.12.110-production+truenas"
        p = run_selection([stable_build(14, verified=["25.10"])], "25.10.9", k,
                          issues=[])
        self.assertIn(f"No build for kernel {k} is waiting for a hardware "
                      "test, so no hardware-test issue", p.stderr)
        self.assertIn("exists for it yet.", p.stderr)

    def test_other_kernels_builds_are_not_named_as_waiting(self):
        p = run_selection([preview_build(15)], "26.0.0-BETA.4",
                          "6.18.50-production+truenas")
        self.assertNotIn("Builds waiting", p.stderr)
        self.assertIn("  k6.18.42-memryx2.1-r15 (" + K42 + ")", p.stderr)

    def test_pending_hint_excludes_preview_builds_on_a_stable_box(self):
        # A stable box never installs a preview build, promoted or not, so
        # it must not be promised an install "once promoted".
        p = run_selection([preview_build(15)], *BETA, train="26",
                          mode="scripts")
        self.assertNotEqual(p.returncode, 0)
        for mode in ("scripts", "install"):
            p = run_selection([preview_build(15)], "26.0.0", K42, mode=mode)
            self.assertNotIn("awaiting hardware-test", p.stderr, mode)

    def test_without_the_issue_list_it_links_the_open_tests(self):
        for issues in (None, "not json", {"message": "API rate limit exceeded"}):
            p = run_selection([preview_build(15)], *BETA, issues=issues)
            self.assertIn(f"https://github.com/{REPO}/issues?q=is%3Aissue+is%3Aopen"
                          "+label%3Apreview-hardware-test", p.stderr, issues)
        p = run_selection([stable_build(14, prerelease=True)], *STABLE)
        self.assertIn("label%3Ahardware-test", p.stderr)

    def test_waiting_builds_are_newest_first_and_capped(self):
        rels = [stable_build(n, prerelease=True) for n in range(1, 9)]
        p = run_selection(rels, *STABLE)
        waiting = p.stderr[p.stderr.index("Builds waiting"):
                           p.stderr.index("Otherwise")]
        tags = [ln.split()[0] for ln in waiting.splitlines()[1:]
                if ln.startswith("  k")]
        self.assertEqual(tags, [f"k6.12.105-memryx2.1-r{n}"
                                for n in (8, 7, 6, 5, 4)])

    def test_approved_builds_for_another_train_are_listed_as_waiting(self):
        p = run_selection([stable_build(14, verified=["26"])], *STABLE)
        self.assertIn("  k6.12.105-memryx2.1-r14\n", p.stderr)

    def test_release_list_shows_kernel_and_approved_trains(self):
        p = run_selection([preview_build(15, verified=["26"])], *STABLE)
        self.assertIn("  k6.18.42-memryx2.1-r15 (" + K42 + ") (prerelease) "
                      "(approved for train 26)", p.stderr)


class TrainKey(unittest.TestCase):
    CASES = {
        "25.10.7": "25.10", "25.10.4": "25.10", "25.10.3.1": "25.10",
        "25.04.2.6": "25.04", "25.10-RC.1": "25.10", "25.10.0-BETA.1": "25.10",
        "26.0.0-BETA.3": "26", "26.0.0-RC.1": "26", "26.0.0": "26",
        "26.1.0": "26", "26.1.2": "26", "27.0.0-BETA.1": "27",
        "27.0.0-RC.1": "27",
        "": None, "abc": None, "25": None, "25.": None, "x25.10": None,
        "26-BETA": None,
    }

    def test_bash_train_key(self):
        for version, want in self.CASES.items():
            self.assertEqual(train_key(version), want, version)

    def test_selection_train_key_matches_bash(self):
        # The selection keys the notes header with train_key and the box
        # with truenas_train_key: the two must agree or the guard misfires.
        src = re.search(r"^def train_key\(v\):\n(?:    .*\n)+",
                        selection_snippet(), re.M).group(0)
        ns = {"re": re}
        exec(src, ns)
        for version, want in self.CASES.items():
            self.assertEqual(ns["train_key"](version) or None, want, version)

    def test_tracked_versions_have_a_train(self):
        # check-releases moves these versions, so pin the rule, not the keys:
        # the preview went from train 26 to 27 when TrueNAS 26 was renamed.
        tracked = json.loads(TRACKED.read_text())
        stable = tracked["truenas"]["version"]
        preview = tracked["truenas_preview"]["version"]
        self.assertIsNotNone(train_key(stable), stable)
        self.assertEqual(train_key(preview), preview.split(".")[0], preview)


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
uname() {{ echo '{kver}'; }}
"""


class ApprovedReleaseTag(unittest.TestCase):
    """approved_release_tag: the shell side of the shared block."""

    def run_tag(self, pages, version, kver=K105, issues=None, args=""):
        with tempfile.TemporaryDirectory() as d:
            for i, page in enumerate(pages, 1):
                Path(d, f"page{i}.json").write_text(json.dumps(page))
            if issues is not None:
                Path(d, "issues.json").write_text(json.dumps(issues))
            p = run_block(STUB.format(d=d, version=version, kver=kver)
                          + f"approved_release_tag {args}")
            calls = Path(d, "calls").read_text().split() if Path(d, "calls").exists() else []
            return p, calls

    def test_fetch_loop_reads_every_page(self):
        page1 = [stable_build(1, prerelease=True) for _ in range(100)]
        page2 = [release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K33)]
        p, calls = self.run_tag([page1, page2], "25.10.3", K33)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "v25.10.3-memryx2.1-r10")
        self.assertEqual([c.rsplit("page=", 1)[1] for c in calls], ["1", "2"])
        self.assertIn(f"Detected TrueNAS version: 25.10.3 (train 25.10, kernel: {K33})",
                      p.stderr)
        self.assertIn("Found release: v25.10.3-memryx2.1-r10 (approved for "
                      "TrueNAS train 25.10)", p.stderr)

    def test_an_unbuilt_version_gets_the_build_for_its_kernel(self):
        p, _ = self.run_tag([[release("v25.10.3-memryx2.1-r10", "25.10.3",
                                      kver=K33)]], "25.10.1", K33)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "v25.10.3-memryx2.1-r10")

    def test_success_does_not_query_issues(self):
        p, calls = self.run_tag([[release("v25.10.3-memryx2.1-r10", "25.10.3",
                                          kver=K33)]], "25.10.3", K33)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(any("/issues?" in c for c in calls), calls)

    def test_nothing_approved_names_the_waiting_issue(self):
        p, calls = self.run_tag([[stable_build(14, prerelease=True)]], *STABLE,
                                issues=NoCandidate.ISSUES)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        self.assertIn("#22 Hardware test", p.stderr)
        self.assertEqual(p.stderr.count("No approved stable release found"), 1)
        self.assertTrue(any("/issues?state=open" in c for c in calls), calls)

    def test_issue_lookup_failure_still_explains(self):
        p, _ = self.run_tag([[stable_build(14, prerelease=True)]], *STABLE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("label%3Ahardware-test", p.stderr)

    def test_scripts_only_mode_takes_the_trains_release(self):
        rels = [release("v25.10.3-memryx2.1-r10", "25.10.3", kver=K33)]
        p, _ = self.run_tag([rels], *STABLE, args="--scripts-only")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "v25.10.3-memryx2.1-r10")
        self.assertIn(f"Detected TrueNAS version: 25.10.7 (train 25.10, kernel: {K105})",
                      p.stderr)
        self.assertIn("Searching for an approved release of this train", p.stderr)

    def test_api_failure_is_an_error_not_a_fallback(self):
        p = run_block(f"""
curl() {{ return 7; }}
midclt() {{ echo '{{"version": "26.0.0-BETA.3"}}'; }}
uname() {{ echo '{K42}'; }}
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

    def test_unreadable_kernel_is_an_error(self):
        p = run_block("""
midclt() { echo '{"version": "25.10.7"}'; }
uname() { return 1; }
curl() { echo "curl $*" >&2; return 7; }
approved_release_tag
""")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("could not read the running kernel (uname -r)", p.stderr)
        self.assertNotIn("curl ", p.stderr)

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
