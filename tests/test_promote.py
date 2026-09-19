"""Run promote.yml's github-script under node with a stub GitHub client.

Each test closes a hardware-test issue against a canned release list and
checks the release update and issue comment the workflow would make:
stable sign-offs promote as before (Latest by version) and add the
verified-train marker in the same update, preview sign-offs add the marker
only, and a second close changes nothing."""
import copy
import json
import subprocess
import unittest
from pathlib import Path

from release_fixtures import issue, marker, release
from test_release_selection import train_key
from workflow_script import run_script, step_script

ROOT = Path(__file__).resolve().parents[1]
PROMOTE_YML = ROOT / ".github" / "workflows" / "promote.yml"

HARNESS = """
const state = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = { updates: [], comments: [], generated: [], compared: [] };
console.log = (...a) => process.stderr.write(a.join(' ') + '\\n');
const notFound = () => Object.assign(new Error('Not Found'), { status: 404 });
const github = {
  paginate: async (fn, args) => (await fn(args)).data,
  rest: {
    repos: {
      getReleaseByTag: async ({ tag }) => {
        const r = state.releases.find((x) => x.tag_name === tag);
        if (!r) throw notFound();
        return { data: r };
      },
      listReleases: async () => ({ data: state.releases }),
      generateReleaseNotes: async (args) => {
        out.generated.push(args);
        return { data: { body: "## What's Changed\\n* fix: a change by @someone" } };
      },
      compareCommitsWithBasehead: async (args) => {
        out.compared.push(args.basehead);
        return { data: { commits: [{ sha: 'abcdef1234567', commit: { message: 'fix: a change\\n\\nmore' } }] } };
      },
      updateRelease: async (args) => { out.updates.push(args); },
    },
    issues: { createComment: async (args) => { out.comments.push(args.body); } },
  },
};
const context = { repo: { owner: 'truenas-community-sysexts', repo: 'memryx-mx3-support' },
                  payload: { issue: state.issue } };
const core = { info: () => {}, warning: () => {} };
(async () => {
%s
})().then(() => process.stdout.write(JSON.stringify(out)),
          (e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def promote_script():
    return step_script("promote.yml", "Record the sign-off on the release this issue gates")


def helpers_snippet():
    text = PROMOTE_YML.read_text()
    begin = text.index("// BEGIN promote-helpers")
    end = text.index("// END promote-helpers")
    return "\n".join(line.strip() for line in text[begin:end].splitlines())


def close(iss, releases):
    return run_script(HARNESS, promote_script(), {"issue": iss, "releases": releases})


def apply(releases, update):
    """The release list after the workflow's updateRelease call."""
    rels = copy.deepcopy(releases)
    for r in rels:
        if r["id"] == update["release_id"]:
            r["prerelease"] = update.get("prerelease", r["prerelease"])
            r["body"] = update.get("body", r["body"])
    return rels


K95 = "6.12.95-production+truenas"
K99 = "6.12.99-production+truenas"
K105 = "6.12.105-production+truenas"
K42 = "6.18.42-production+truenas"

R6 = "v25.10.4-memryx2.1-r6"
R10 = "v25.10.3-memryx2.1-r10"
R14 = "v25.10.7-memryx2.1-r14"
R15 = "v26.0.0-BETA.3-memryx2.1-r15"


def stable_rels():
    return [release(R14, "25.10.7", kver=K105, prerelease=True,
                    published="2026-09-03T10:59:34Z"),
            release(R10, "25.10.3", kver=K95, published="2026-06-26T03:47:08Z"),
            release(R6, "25.10.4", kver=K99, published="2026-06-14T03:40:29Z")]


def preview_rels():
    return [release(R15, "26.0.0-BETA.3", "Halfmoon", kver=K42, prerelease=True,
                    published="2026-09-19T00:23:19Z")] + stable_rels()


STABLE_ISSUE = dict(labels=("hardware-test",), preview=False)
PREVIEW_ISSUE = dict(labels=("preview-hardware-test",), preview=True)


class StableSignOff(unittest.TestCase):
    def test_promotes_and_adds_the_marker_in_one_update(self):
        rels = stable_rels()
        out = close(issue(R14, **STABLE_ISSUE), rels)
        self.assertEqual(len(out["updates"]), 1, out)
        up = out["updates"][0]
        self.assertIs(up["prerelease"], False)
        self.assertEqual(up["make_latest"], "true")  # newest stable version
        self.assertTrue(up["body"].startswith(rels[0]["body"]))
        self.assertEqual(up["body"].count("## Changelog"), 1)
        self.assertEqual(up["body"].count(marker("25.10")), 1)
        self.assertTrue(up["body"].rstrip().endswith(marker("25.10")))
        self.assertIn("promoted", out["comments"][0])
        self.assertIn("approved for TrueNAS train `25.10`", out["comments"][0])

    def test_latest_stays_on_the_newer_stable_version(self):
        # Promoting an older build does not take Latest from a newer promoted
        # one.
        rels = [release("v25.10.3.1-memryx2.1-r20", "25.10.3.1", kver=K95,
                        prerelease=True, published="2026-09-20T00:00:00Z"),
                release(R6, "25.10.4", kver=K99, published="2026-06-14T03:40:29Z")]
        out = close(issue("v25.10.3.1-memryx2.1-r20", **STABLE_ISSUE), rels)
        up = out["updates"][0]
        self.assertEqual(up["make_latest"], "false")
        self.assertIn(marker("25.10"), up["body"])
        self.assertIn("Latest stays on the newer `25.10.4`", out["comments"][0])

    def test_a_beta_release_never_blocks_a_stable_promotion(self):
        # v26.0.0-BETA.3-... parses as version 26.0.0, which would outrank
        # every 25.x stable version if preview releases were not excluded.
        rels = preview_rels()
        out = close(issue(R14, **STABLE_ISSUE), rels)
        self.assertEqual(out["updates"][0]["make_latest"], "true")

    def test_legacy_issue_without_preview_marker_takes_the_same_path(self):
        out = close(issue(R14), stable_rels())
        self.assertIs(out["updates"][0]["prerelease"], False)
        self.assertIn(marker("25.10"), out["updates"][0]["body"])

    def test_grandfathered_full_release_is_left_alone(self):
        # Already approved for every train; a marker would narrow that.
        out = close(issue(R6, **STABLE_ISSUE), stable_rels())
        self.assertEqual(out["updates"], [])
        self.assertIn("already promoted", out["comments"][0])

    def test_full_release_approved_for_another_train_gets_this_one(self):
        rels = [release("v26.0.0-memryx2.1-r30", "26.0.0", "Halfmoon",
                        kver="6.18.60-production+truenas", verified=["27"])]
        out = close(issue("v26.0.0-memryx2.1-r30", **STABLE_ISSUE), rels)
        up = out["updates"][0]
        self.assertNotIn("prerelease", up)
        self.assertEqual(up["make_latest"], "false")
        self.assertEqual(up["body"], rels[0]["body"] + f"\n\n{marker('26')}\n")

    def test_changelog_starts_at_the_previous_promoted_release(self):
        # Not at the interleaved beta prerelease.
        out = close(issue(R14, **STABLE_ISSUE), preview_rels())
        self.assertEqual(out["compared"], [f"{R10}...{R14}"])


class PreviewSignOff(unittest.TestCase):
    def test_adds_the_marker_only(self):
        rels = preview_rels()
        out = close(issue(R15, **PREVIEW_ISSUE), rels)
        self.assertEqual(len(out["updates"]), 1, out)
        up = out["updates"][0]
        self.assertNotIn("prerelease", up)  # stays a prerelease
        self.assertEqual(up["make_latest"], "false")
        self.assertEqual(up["body"], rels[0]["body"] + f"\n\n{marker('26')}\n")
        self.assertEqual(out["generated"], [])
        self.assertIn("approved for TrueNAS train `26`", out["comments"][0])
        self.assertIn("never promoted to Latest", out["comments"][0])

    def test_any_preview_signal_means_marker_only(self):
        # Mislabeled as a stable test: the preview-build marker, the BETA
        # tag, or the notes header each keep it from being promoted.
        rels = preview_rels()
        for iss in (issue(R15, labels=("hardware-test",), preview=True),
                    issue(R15, labels=("hardware-test",))):
            out = close(iss, rels)
            self.assertNotIn("prerelease", out["updates"][0], iss)
            self.assertIn(marker("26"), out["updates"][0]["body"])

    def test_before_the_marker_selection_refuses_it_and_after_it_takes_it(self):
        from test_release_selection import run_selection
        rels = preview_rels()
        p = run_selection(rels, "26.0.0-BETA.3")
        self.assertNotEqual(p.returncode, 0)
        out = close(issue(R15, **PREVIEW_ISSUE), rels)
        rels = apply(rels, out["updates"][0])
        p = run_selection(rels, "26.0.0-BETA.3")
        self.assertEqual(p.stdout, R15)
        # And a 25.10 box is not affected.
        p = run_selection(rels, "25.10.7")
        self.assertNotEqual(p.returncode, 0)


class Idempotent(unittest.TestCase):
    def test_second_close_changes_nothing(self):
        for rels, iss, tag, train in (
                (preview_rels(), issue(R15, **PREVIEW_ISSUE), R15, "26"),
                (stable_rels(), issue(R14, **STABLE_ISSUE), R14, "25.10")):
            first = close(iss, rels)
            rels = apply(rels, first["updates"][0])
            again = close(dict(iss, number=2), rels)
            self.assertEqual(again["updates"], [], train)
            self.assertIn(f"already approved for TrueNAS train `{train}`",
                          again["comments"][0])
            body = next(r for r in rels if r["tag_name"] == tag)["body"]
            self.assertEqual(body.count(marker(train)), 1)


class Guards(unittest.TestCase):
    def test_issue_without_release_tag_changes_nothing(self):
        iss = dict(issue(R14), body="no markers here")
        out = close(iss, stable_rels())
        self.assertEqual(out, {"updates": [], "comments": [], "generated": [],
                               "compared": []})

    def test_missing_release_is_reported(self):
        out = close(issue("v25.10.9-memryx2.1-r99"), stable_rels())
        self.assertEqual(out["updates"], [])
        self.assertIn("No release found for tag", out["comments"][0])

    def test_train_comes_from_the_tag_when_the_body_has_no_header(self):
        tag = "v25.10.5-memryx2.1-r8"
        rels = [dict(release(tag, "25.10.5", kver=K95, prerelease=True), body="")]
        out = close(issue(tag), rels)
        self.assertIn(marker("25.10"), out["updates"][0]["body"])

    def test_no_version_anywhere_changes_nothing(self):
        tag = "memryx2.1-r8"
        rels = [dict(release(tag, prerelease=True), body="")]
        out = close(issue(tag), rels)
        self.assertEqual(out["updates"], [])
        self.assertIn("Cannot tell which TrueNAS train", out["comments"][0])


class Trigger(unittest.TestCase):
    def test_job_runs_for_both_labels_on_completed_only(self):
        text = PROMOTE_YML.read_text()
        cond = text[text.index("    if: >-"):text.index("    runs-on:")]
        self.assertIn("'hardware-test'", cond)
        self.assertIn("'preview-hardware-test'", cond)
        self.assertIn("github.event.issue.state_reason == 'completed'", cond)


TRAIN_DRIVER = """
const versions = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(versions.map((v) => trainKey(v))));
"""

VERSIONS = ["25.10.7", "25.10.3.1", "25.04.2.6", "25.10-RC.1", "26.0.0-BETA.3",
            "26.0.0-RC.1", "26.1.2", "26", "27.0.0-BETA.1", "", "abc", "25",
            "25.", "x25.10", "025.10.1"]


class TrainKeyParity(unittest.TestCase):
    """promote.yml's trainKey and install.sh's truenas_train_key give the
    same key for every version."""

    def test_promote_train_key_matches_the_installers(self):
        p = subprocess.run(["node", "-e", helpers_snippet() + TRAIN_DRIVER],
                           input=json.dumps(VERSIONS), capture_output=True,
                           text=True)
        if p.returncode != 0:
            raise AssertionError(p.stderr)
        self.assertEqual(json.loads(p.stdout), [train_key(v) or "" for v in VERSIONS])


if __name__ == "__main__":
    unittest.main()
