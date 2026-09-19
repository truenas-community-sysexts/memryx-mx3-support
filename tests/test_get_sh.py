"""End-to-end runs of get.sh, uninstall.sh and install.sh against stub
`midclt`, `curl` (and for install.sh `id` and `unsquashfs`) commands on PATH.

The curl stub serves canned GitHub API pages and issues, and for a release
download writes a fake asset: install.sh, uninstall.sh and restore.sh stubs
print which release they came from and the arguments they got, memryx.raw is
a small file naming its release, and memryx.raw.sha256 matches it. So the
tests see exactly what each script would download and run."""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from urllib.parse import urlparse

from release_fixtures import release
from test_release_selection import run_block

ROOT = Path(__file__).resolve().parents[1]
GET_SH = ROOT / "get.sh"
INSTALL_SH = ROOT / "scripts" / "install.sh"
UNINSTALL_SH = ROOT / "scripts" / "uninstall.sh"
REPO = "truenas-community-sysexts/memryx-mx3-support"


def logged_host(line):
    """Hostname of the URL in a stub-log line ("curl <url>"), or "" otherwise."""
    parts = line.split()
    if len(parts) > 1 and parts[0] == "curl":
        return urlparse(parts[1]).hostname or ""
    return ""


def logged_path(line):
    """Path of the URL in a stub-log line ("curl <url>"), or "" otherwise."""
    parts = line.split()
    if len(parts) > 1 and parts[0] == "curl":
        return urlparse(parts[1]).path
    return ""


CURL_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import hashlib, json, os, re, sys
    from urllib.parse import urlparse
    args = sys.argv[1:]
    url = next(a for a in args if a.startswith("https://"))
    parts = urlparse(url)
    host, path = parts.hostname, parts.path
    out = args[args.index("-o") + 1] if "-o" in args else None
    with open(os.environ["STUB_LOG"], "a") as f:
        f.write("curl " + url + "\\n")
    def emit(text):
        if out:
            with open(out, "w") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
    if host == "api.github.com" and path.endswith("/issues"):
        emit(open(os.environ["STUB_ISSUES"]).read())
    elif host == "api.github.com":
        page = int(re.search(r"(?:^|&)page=(\\d+)", parts.query).group(1))
        pages = json.load(open(os.environ["STUB_PAGES"]))
        emit(json.dumps(pages[page - 1] if page <= len(pages) else []))
    elif host == "github.com" and "/releases/download/" in path:
        repo = path.split("/releases/download/")[0].strip("/")
        tag, asset = path.split("/releases/download/")[1].split("/")
        old = tag in os.environ.get("STUB_OLD", "").split()
        image = f"memryx.raw of {tag}\\n"
        if asset == "memryx.raw":
            emit(image)
        elif asset == "memryx.raw.sha256":
            if os.environ.get("STUB_BAD_SHA"):
                image = "tampered"
            emit(hashlib.sha256(image.encode()).hexdigest() + "  memryx.raw\\n")
        elif asset in ("install.sh", "uninstall.sh", "restore.sh"):
            flag = "" if old else ": <<'FLAGS'\\n    --release=*)\\nFLAGS\\n"
            emit("#!/usr/bin/env bash\\n" + flag +
                 f'echo "RAN {asset} from {tag} of {repo} with: $*"\\n'
                 'echo "MEMRYX_REPO=${MEMRYX_REPO:-}"\\n'
                 'here=$(dirname "$0")\\n'
                 'for f in memryx-lib.sh restore.sh; do [ -f "$here/$f" ] && echo "BESIDE $f: $(head -1 "$here/$f")"; done\\n'
                 'last="${*: -1}"\\n'
                 '[ -f "$last" ] && echo "IMAGE: $(cat "$last")"\\n'
                 'exit 0\\n')
        elif asset == "memryx-lib.sh":
            emit(f"# memryx-lib.sh of {tag}\\nmemryx_init_script_lookup() {{ printf ''; }}\\n")
        else:
            sys.exit(22)
    else:
        sys.exit(22)
    """)

MIDCLT_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "midclt $*" >> "$STUB_LOG"
    case "$*" in
        *system.info*)
            [ -n "$STUB_VERSION" ] || exit 1
            echo "{\\"version\\": \\"$STUB_VERSION\\"}" ;;
        *) echo "[]" ;;
    esac
    """)

ID_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "${1:-}" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi
    """)

# install.sh unpacks memryx-preinit.sh (and, with a local image and no
# sibling lib, memryx-lib.sh) out of the image.
UNSQUASHFS_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    if "-d" in args:
        d = os.path.join(args[args.index("-d") + 1], "usr/lib/memryx")
        want = args[-1]
        os.makedirs(d, exist_ok=True)
        name = os.path.basename(want)
        if name in ("memryx-preinit.sh", "memryx-lib.sh"):
            with open(os.path.join(d, name), "w") as f:
                f.write("#!/bin/sh\\nmemryx_init_script_lookup() { printf ''; }\\n")
    """)

R5 = "v25.10.4-memryx2.1-r5"
R6 = "v25.10.4-memryx2.1-r6"
R10 = "v25.10.3-memryx2.1-r10"
R13 = "v26.0.0-BETA.3-memryx2.1-r13"
R14 = "v25.10.7-memryx2.1-r14"
R15 = "v26.0.0-BETA.3-memryx2.1-r15"

K95 = "6.12.95-production+truenas"
K99 = "6.12.99-production+truenas"
K105 = "6.12.105-production+truenas"
K42 = "6.18.42-production+truenas"


def releases(signed_off=True):
    """The shape of the real release list: grandfathered 25.10 builds, an
    unverified 25.10.7 build and two 26 beta builds, signed off or not."""
    verified = ["26"] if signed_off else []
    return [
        release(R15, "26.0.0-BETA.3", "Halfmoon", kver=K42, prerelease=True,
                published="2026-09-19T00:23:19Z", verified=verified),
        release(R14, "25.10.7", kver=K105, prerelease=True,
                published="2026-09-03T10:59:34Z"),
        release(R13, "26.0.0-BETA.3", "Halfmoon", kver=K42, prerelease=True,
                published="2026-08-21T06:52:40Z"),
        release(R10, "25.10.3", kver=K95, published="2026-06-26T03:47:08Z"),
        release(R6, "25.10.4", kver=K99, published="2026-06-14T03:40:29Z"),
        release(R5, "25.10.4", kver=K99, published="2026-06-12T00:51:42Z"),
    ]


ISSUES = [{"number": 22, "title": f"Hardware test: ... | {R14}",
           "body": f"<!-- release-tag: {R14} -->",
           "labels": [{"name": "hardware-test"}],
           "html_url": "https://example.test/issues/22"}]


class Stubbed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, text in (("curl", CURL_STUB), ("midclt", MIDCLT_STUB),
                           ("id", ID_STUB), ("unsquashfs", UNSQUASHFS_STUB)):
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)
        self.log = self.dir / "log"
        self.log.write_text("")
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def run_bash(self, argv, version, rels=None, issues=ISSUES, **env):
        pages = self.dir / "pages.json"
        pages.write_text(json.dumps([releases() if rels is None else rels]))
        issues_file = self.dir / "issues.json"
        issues_file.write_text(json.dumps(issues))
        full_env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        STUB_LOG=str(self.log), STUB_PAGES=str(pages),
                        STUB_ISSUES=str(issues_file), STUB_VERSION=version,
                        TMPDIR=str(self.tmp))
        full_env.pop("MEMRYX_REPO", None)
        full_env.update(env)
        return subprocess.run(["bash", *argv], capture_output=True,
                              text=True, env=full_env)

    def calls(self):
        return self.log.read_text().splitlines()

    def downloads(self):
        return [logged_path(c).split("/releases/download/")[1]
                for c in self.calls()
                if logged_host(c) == "github.com"
                and "/releases/download/" in logged_path(c)]


class GetSh(Stubbed):
    def get(self, *args, version="25.10.4", **kw):
        return self.run_bash([str(GET_SH), *args], version, **kw)

    def test_installs_the_approved_build_for_this_version(self):
        p = self.get()
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.splitlines()
        self.assertTrue(lines[0].startswith(
            f"RAN install.sh from {R6} of {REPO} with: --release={R6} "), lines)
        self.assertTrue(lines[0].endswith("/memryx.raw"), lines)
        self.assertIn(f"BESIDE memryx-lib.sh: # memryx-lib.sh of {R6}", lines)
        self.assertIn(f"IMAGE: memryx.raw of {R6}", lines)
        self.assertEqual(sorted(self.downloads()),
                         sorted(f"{R6}/{a}" for a in
                                ("install.sh", "memryx-lib.sh", "memryx.raw",
                                 "memryx.raw.sha256")))

    def test_each_box_gets_its_own_approved_build(self):
        for version, tag in (("25.10.4", R6), ("25.10.3", R10),
                             ("26.0.0-BETA.3", R15)):
            p = self.get(version=version)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(f"RAN install.sh from {tag} ", p.stdout)

    def test_arguments_pass_through(self):
        p = self.get("--pool=fast", "--dry-run")
        self.assertIn(f"with: --release={R6} --pool=fast --dry-run ", p.stdout)

    def test_old_installer_gets_the_image_without_release(self):
        # Releases from before per-train approval reject --release (every
        # generation back to r1 refuses an unknown option); the local image
        # alone makes them install this release's build.
        p = self.get(STUB_OLD=R6)
        self.assertEqual(p.returncode, 0, p.stderr)
        line = p.stdout.splitlines()[0]
        self.assertNotIn("--release", line)
        self.assertTrue(line.endswith("/memryx.raw"), line)
        self.assertIn(f"IMAGE: memryx.raw of {R6}", p.stdout)

    def test_nothing_approved_stops_before_any_download(self):
        p = self.get(version="25.10.7")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.downloads(), [])
        self.assertIn("No approved stable release found for TrueNAS version "
                      "25.10.7.", p.stderr)
        self.assertIn("#22 Hardware test", p.stderr)

    def test_unverified_beta_is_not_installed_on_a_beta_box(self):
        p = self.get(version="26.0.0-BETA.3", rels=releases(signed_off=False))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.downloads(), [])
        self.assertIn("No approved preview (beta) release found", p.stderr)
        self.assertIn(f"  {R15} (prerelease)", p.stderr)

    def test_checksum_mismatch_stops_before_the_installer(self):
        p = self.get(STUB_BAD_SHA="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("checksum verification failed for memryx.raw", p.stderr)
        self.assertNotIn("RAN install.sh", p.stdout)

    def test_check_uses_an_approved_release_of_the_train_and_no_image(self):
        # --check loads no modules: a box on a TrueNAS version with no
        # approved build still gets an installer of its train to probe with.
        p = self.get("--check", version="25.10.7")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines()[0],
                         f"RAN install.sh from {R10} of {REPO} with: --release={R10} --check")
        self.assertNotIn(f"{R10}/memryx.raw", self.downloads())

    def test_update_firmware_needs_no_image(self):
        p = self.get("--update-firmware", version="25.10.7")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(f"{R10}/memryx.raw", self.downloads())

    def test_own_image_path_is_not_replaced(self):
        own = self.dir / "mine.raw"
        own.write_text("my image\n")
        p = self.get(str(own))
        self.assertIn(f"with: --release={R6} {own}", p.stdout)
        self.assertIn("IMAGE: my image", p.stdout)
        self.assertNotIn(f"{R6}/memryx.raw", self.downloads())

    def test_uninstall_runs_the_approved_releases_uninstaller(self):
        p = self.get("--uninstall", "--force", version="25.10.7")
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], f"RAN uninstall.sh from {R10} of {REPO} with: --force")
        self.assertIn("BESIDE restore.sh: #!/usr/bin/env bash", lines)
        self.assertIn(f"BESIDE memryx-lib.sh: # memryx-lib.sh of {R10}", lines)
        self.assertEqual(sorted(self.downloads()),
                         sorted(f"{R10}/{a}" for a in
                                ("uninstall.sh", "restore.sh", "memryx-lib.sh")))

    def test_pinned_release_skips_selection(self):
        p = self.get(f"--release={R14}", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"RAN install.sh from {R14} of {REPO} with: --release={R14} --dry-run ",
                      p.stdout)
        self.assertIn(f"IMAGE: memryx.raw of {R14}", p.stdout)
        self.assertFalse(any(c.startswith("midclt")
                             or logged_host(c) == "api.github.com"
                             for c in self.calls()), self.calls())

    def test_pinned_uninstall(self):
        p = self.get("--uninstall", f"--release={R13}")
        self.assertEqual(p.stdout.splitlines()[0].rstrip(),
                         f"RAN uninstall.sh from {R13} of {REPO} with:")

    def test_repo_points_everything_at_a_fork(self):
        p = self.get("--repo=someone/memryx-fork", f"--release={R6}")
        self.assertIn(f"RAN install.sh from {R6} of someone/memryx-fork", p.stdout)
        self.assertIn("MEMRYX_REPO=someone/memryx-fork", p.stdout)
        p = self.get(version="25.10.4", MEMRYX_REPO="someone/memryx-fork")
        self.assertTrue(any(logged_host(c) == "api.github.com"
                            and logged_path(c) == "/repos/someone/memryx-fork/releases"
                            for c in self.calls()), self.calls())

    def test_empty_flags_are_refused(self):
        self.assertEqual(self.get("--release=").returncode, 2)
        self.assertEqual(self.get("--repo=").returncode, 2)

    def test_unreadable_truenas_version_is_an_error(self):
        p = self.get(version="")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Failed to detect TrueNAS version", p.stderr)
        self.assertEqual(self.downloads(), [])

    def test_temp_dir_is_removed(self):
        self.get()
        self.get("--uninstall")
        self.assertEqual(list(self.tmp.glob("memryx-get.*")), [])

    def test_never_uses_latest(self):
        self.get()
        self.get("--check")
        self.get("--uninstall")
        self.assertFalse(any("releases/latest" in c for c in self.calls()))


class ScriptsOnlyTrain(Stubbed):
    """Script-only modes never use a release built for another train, not
    even a grandfathered one: a 26 box must not run a 25.10 build's --check
    or restore.sh."""

    def only_2510(self):
        return [r for r in releases(signed_off=False)
                if r["tag_name"] in (R10, R6, R15)]

    def test_26_box_with_only_grandfathered_2510_releases_refuses(self):
        for args in (("--check",), ("--uninstall",), ("--help",)):
            self.log.write_text("")
            p = self.run_bash([str(GET_SH), *args], "26.0.0-BETA.3",
                              rels=self.only_2510())
            self.assertNotEqual(p.returncode, 0, args)
            self.assertNotIn("RAN", p.stdout, args)
            self.assertEqual(self.downloads(), [], args)
            self.assertIn("release built for TrueNAS train 26 is approved yet",
                          p.stderr, args)

    def test_26_box_uninstall_sh_refuses(self):
        d = self.dir / "run"
        d.mkdir()
        shutil.copy(UNINSTALL_SH, d / "uninstall.sh")
        p = self.run_bash([str(d / "uninstall.sh")], "26.0.0-BETA.3",
                          rels=self.only_2510())
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.downloads(), [])
        self.assertIn("pin one with --release=TAG", p.stderr)

    def test_26_box_install_sh_check_refuses_another_trains_lib(self):
        d = self.dir / "alone"
        d.mkdir()
        shutil.copy(INSTALL_SH, d / "install.sh")
        p = self.run_bash([str(d / "install.sh"), "--check"], "26.0.0-BETA.3",
                          rels=self.only_2510())
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.downloads(), [])
        self.assertIn("Could not load memryx-lib.sh", p.stderr)

    def test_26_box_with_a_26_approved_release_uses_it(self):
        for version in ("26.0.0-BETA.3", "26.0.0-BETA.4"):
            self.log.write_text("")
            p = self.run_bash([str(GET_SH), "--uninstall"], version)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(f"RAN uninstall.sh from {R15} ", p.stdout)
            p = self.run_bash([str(GET_SH), "--check"], version)
            self.assertIn(f"RAN install.sh from {R15} ", p.stdout)

    def test_2510_box_is_unchanged(self):
        p = self.run_bash([str(GET_SH), "--check"], "25.10.4")
        self.assertIn(f"RAN install.sh from {R6} ", p.stdout)
        p = self.run_bash([str(GET_SH), "--uninstall"], "25.10.7")
        self.assertIn(f"RAN uninstall.sh from {R10} ", p.stdout)

    def test_pinned_release_is_the_escape_hatch(self):
        p = self.run_bash([str(GET_SH), "--uninstall", f"--release={R10}"],
                          "26.0.0-BETA.3", rels=self.only_2510())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"RAN uninstall.sh from {R10} ", p.stdout)


class SelectionParity(Stubbed):
    """get.sh, install.sh, uninstall.sh and restore.sh choose the same
    release: same block, same inputs, same answer (SharedCopies holds the
    text identical)."""

    SCENARIOS = (
        ("25.10.3", True), ("25.10.4", True), ("25.10.7", True),
        ("26.0.0-BETA.3", True), ("26.0.0-BETA.3", False),
        ("26.1.0", True), ("25.04.2", True),
    )

    def chosen(self, path, version, signed_off):
        pages = self.dir / "parity.json"
        pages.write_text(json.dumps(releases(signed_off)))
        p = run_block(f"""
curl() {{
    case "${{*: -1}}" in
        *"page=1") cat "{pages}" ;;
        *) echo '[]' ;;
    esac
}}
midclt() {{ echo '{{"version": "{version}"}}'; }}
approved_release_tag
""", path=path)
        return p.returncode == 0, p.stdout.strip()

    def test_every_copy_chooses_the_same_release(self):
        restore = ROOT / "scripts" / "restore.sh"
        for version, signed in self.SCENARIOS:
            got = self.chosen(GET_SH, version, signed)
            for path in (INSTALL_SH, UNINSTALL_SH, restore):
                self.assertEqual(got, self.chosen(path, version, signed),
                                 (path.name, version, signed))

    def test_get_sh_runs_what_the_block_chose(self):
        for version, signed in self.SCENARIOS:
            ok, tag = self.chosen(GET_SH, version, signed)
            p = self.run_bash([str(GET_SH)], version, rels=releases(signed))
            if ok:
                self.assertIn(f"RAN install.sh from {tag} ", p.stdout, version)
            else:
                self.assertNotEqual(p.returncode, 0)
                self.assertNotIn("RAN", p.stdout)


class UninstallSh(Stubbed):
    def uninstall(self, *args, sibling=False, version="25.10.7", **kw):
        d = self.dir / "run"
        d.mkdir(exist_ok=True)
        shutil.copy(UNINSTALL_SH, d / "uninstall.sh")
        if sibling:
            (d / "restore.sh").write_text('echo "SIBLING restore.sh with: $*"\n')
        return self.run_bash([str(d / "uninstall.sh"), *args], version, **kw)

    def test_piped_run_fetches_restore_from_the_approved_release(self):
        p = self.uninstall("--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines()[0],
                         f"RAN restore.sh from {R10} of {REPO} with: --force")
        self.assertEqual(sorted(self.downloads()),
                         [f"{R10}/memryx-lib.sh", f"{R10}/restore.sh"])
        self.assertFalse(any("releases/latest" in c for c in self.calls()))

    def test_pinned_release(self):
        p = self.uninstall(f"--release={R13}", "--force")
        self.assertEqual(p.stdout.splitlines()[0],
                         f"RAN restore.sh from {R13} of {REPO} with: --force")
        self.assertFalse(any(logged_host(c) == "api.github.com"
                             for c in self.calls()))

    def test_sibling_restore_runs_without_network(self):
        p = self.uninstall(f"--release={R13}", "--force", sibling=True)
        self.assertEqual(p.stdout.strip(), "SIBLING restore.sh with: --force")
        self.assertEqual(self.calls(), [])

    def test_nothing_approved_is_an_error(self):
        p = self.uninstall(version="26.0.0-BETA.3",
                           rels=releases(signed_off=False)[:3])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("pin one with --release=TAG", p.stderr)
        self.assertEqual(self.downloads(), [])


class RestoreSh(Stubbed):
    """restore.sh takes memryx-lib.sh from the approved release too, never
    from whatever GitHub marks Latest."""

    def restore(self, *args, version="25.10.7", **kw):
        d = self.dir / "restore-run"
        d.mkdir(exist_ok=True)
        shutil.copy(ROOT / "scripts" / "restore.sh", d / "restore.sh")
        return self.run_bash([str(d / "restore.sh"), *args], version, **kw)

    def test_lib_comes_from_the_approved_release(self):
        p = self.restore("--force")
        self.assertEqual(self.downloads(), [f"{R10}/memryx-lib.sh"])
        self.assertFalse(any("releases/latest" in c for c in self.calls()))
        self.assertIn("=== Removing MemryX MX3 sysext ===", p.stdout)

    def test_pinned_release(self):
        self.restore(f"--release={R13}", "--force")
        self.assertEqual(self.downloads(), [f"{R13}/memryx-lib.sh"])
        self.assertFalse(any(logged_host(c) == "api.github.com"
                             for c in self.calls()))

    def test_nothing_approved_is_an_error(self):
        p = self.restore(version="26.0.0-BETA.3",
                         rels=releases(signed_off=False)[:3])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Could not load memryx-lib.sh", p.stderr)
        self.assertEqual(self.downloads(), [])


class InstallSh(Stubbed):
    """install.sh itself, run with --dry-run so nothing on the system
    changes; the stubs stand in for the TrueNAS commands it reads."""

    def install(self, *args, script=INSTALL_SH, version="25.10.4", **kw):
        return self.run_bash([str(script), "--dry-run", "--pool=tank", *args],
                             version, **kw)

    def alone(self):
        """install.sh with no memryx-lib.sh beside it (the curl|bash case)."""
        d = self.dir / "alone"
        d.mkdir(exist_ok=True)
        shutil.copy(INSTALL_SH, d / "install.sh")
        return d / "install.sh"

    def test_selects_and_installs_the_approved_build(self):
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(f"Found release: {R6} (approved for TrueNAS train 25.10)",
                      p.stderr)
        self.assertIn(f"  Release tag:       {R6}", p.stdout)
        self.assertIn("  MemryX SDK:        2.1", p.stdout)
        self.assertIn("Checksum OK", p.stdout)
        self.assertEqual(sorted(self.downloads()),
                         [f"{R6}/memryx.raw", f"{R6}/memryx.raw.sha256"])

    def test_nothing_approved_stops_before_any_download(self):
        p = self.install(version="26.0.0-BETA.3", rels=releases(signed_off=False))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.downloads(), [])
        self.assertIn("No approved preview (beta) release found", p.stderr)
        self.assertNotIn("Dry-run complete", p.stdout)

    def test_signed_off_beta_installs_on_a_beta_box(self):
        p = self.install(version="26.0.0-BETA.3")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(f"  Release tag:       {R15}", p.stdout)

    def test_pinned_release_skips_selection(self):
        p = self.install(f"--release={R14}", version="25.10.7")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(f"Release {R14} (pinned with --release)", p.stdout)
        self.assertFalse(any("system.info" in c
                             or logged_host(c) == "api.github.com"
                             for c in self.calls()), self.calls())

    def test_local_image_with_release_records_the_release(self):
        image = self.dir / "memryx-input.raw"
        image.write_text("local image\n")
        p = self.install(f"--release={R6}", str(image))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(f"Using local memryx.raw: {image}", p.stdout)
        self.assertIn(f"  Release tag:       {R6}", p.stdout)
        self.assertEqual(self.downloads(), [])

    def test_lib_comes_from_the_selected_release_not_latest(self):
        p = self.install(script=self.alone())
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn(f"{R6}/memryx-lib.sh", self.downloads())
        self.assertFalse(any("releases/latest" in c for c in self.calls()))

    def test_check_without_a_lib_takes_it_from_an_approved_release(self):
        p = self.run_bash([str(self.alone()), "--check"], "25.10.7")
        self.assertIn(f"{R10}/memryx-lib.sh", self.downloads())
        self.assertIn("=== MemryX MX3 install status ===", p.stdout)
        self.assertFalse(any("releases/latest" in c for c in self.calls()))

    def test_empty_release_flag_is_refused(self):
        p = self.install("--release=")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--release= requires a release tag", p.stderr)

    def test_help_lists_release(self):
        p = self.run_bash([str(INSTALL_SH), "--help"], "25.10.4")
        self.assertIn("--release=TAG", p.stdout)


if __name__ == "__main__":
    unittest.main()
