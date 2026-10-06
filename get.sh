#!/usr/bin/env bash
# Install the MemryX MX3 sysext on TrueNAS from the newest release that a
# hardware test approved for this box's TrueNAS train, built for its running
# kernel.
#
#   curl -fsSL https://raw.githubusercontent.com/truenas-community-sysexts/memryx-mx3-support/main/get.sh | sudo bash
#
# Arguments go after `bash -s --` and pass through to the installer:
#
#   ... | sudo bash -s -- --pool=fast        # any install.sh flag
#   ... | sudo bash -s -- --check            # probe an existing install
#   ... | sudo bash -s -- --release=TAG      # that release, no selection
#   ... | sudo bash -s -- --uninstall        # remove it with the approved
#                                            # release's uninstall.sh
#
# What it does:
#   1. Reads the TrueNAS version (midclt call system.info) and derives the
#      train: the major version from 26 on (every 26.x release, betas
#      included, is train 26), major.minor before that (25.10). Reads the
#      running kernel (uname -r).
#   2. Lists this repo's releases and picks the newest one approved for that
#      train and built for that exact kernel (and, since the sysext also
#      ships userspace staged against a train's base system, for that
#      train). A hardware test approves a build for the train it was built
#      for (promote.yml writes a verified-train marker into its notes); a
#      full release with no marker was promoted before per-train sign-off and
#      counts for every train. Nothing else is ever installed: with no
#      approved build for this kernel it stops and names the hardware test
#      that is waiting, if there is one.
#   3. Downloads THAT release's install.sh, memryx-lib.sh, memryx.raw and
#      memryx.raw.sha256, checks the image against the checksum, and runs
#      that release's installer with your arguments and the local image
#      (every release's installer takes a path to memryx.raw as a positional
#      argument), so the image and the scripts come from the same release.
#
# --check, --help, --uninstall and a path to your own image use only the
# release's scripts, so on a kernel with no approved build they take the
# newest approved release built for this box's own train (never one built for
# another train, grandfathered or not: its --check and restore.sh know
# another install). --uninstall runs its uninstall.sh, with its restore.sh
# and memryx-lib.sh beside it. --release=TAG skips steps 1 and 2 and uses TAG
# as given. --repo=OWNER/NAME (or MEMRYX_REPO) points all of it at a fork.

set -euo pipefail

REPO="${MEMRYX_REPO:-truenas-community-sysexts/memryx-mx3-support}"
WORK_DIR=""

# BEGIN approved-release (a verbatim copy lives in get.sh, scripts/install.sh,
# scripts/uninstall.sh and scripts/restore.sh, each a self-contained curl|bash
# script; tests/test_release_selection.py fails CI when the copies differ)

# TrueNAS version of this box: the version string decides the release channel
# (stable vs preview) and the train. The running kernel picks the build.
detect_truenas_version() {
    local v
    v=$(midclt call system.info | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin)['version'])
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
") || { echo "ERROR: Failed to detect TrueNAS version" >&2; return 1; }
    [ -n "$v" ] || { echo "ERROR: TrueNAS version is empty" >&2; return 1; }
    printf '%s\n' "$v"
}

# Train key of a TrueNAS version: the major version from 26 on (26.0.0-BETA.3
# and 26.1.2 are both train 26), major.minor before that (25.10.7 is 25.10,
# 25.04.2.6 is 25.04). Fails on anything else. Copied verbatim from
# nvidia-driver-support's get.sh; the selection's train_key applies the same
# rule to the version in a release's notes header, and promote.yml's trainKey
# is held to it by tests/test_promote.py.
truenas_train_key() {
    local v="$1" major minor
    major="${v%%.*}"
    case "$major" in ''|*[!0-9]*) return 1 ;; esac
    if [ "$major" -ge 26 ]; then
        printf '%s\n' "$major"
        return 0
    fi
    case "$v" in *.*) ;; *) return 1 ;; esac
    minor="${v#*.}"
    minor="${minor%%[!0-9]*}"
    [ -n "$minor" ] || return 1
    printf '%s.%s\n' "$major" "$minor"
}

# Every page of the repo's releases, appended to $1 as one JSON array per
# page. The oldest releases are exactly the ones a single newest-first page
# drops once the repo outgrows it, and the version and scripts-only fallbacks
# need them. Only a full page can have more behind it; anything else (short
# page, API error object) ends the loop, and the selection reports API errors.
fetch_release_pages() {
    local out="$1" page=1 page_json page_len
    : > "$out"
    while :; do
        page_json=$(curl -sS --max-time 30 "https://api.github.com/repos/${REPO}/releases?per_page=100&page=${page}") \
            || { echo "ERROR: Failed to query GitHub releases" >&2; return 1; }
        printf '%s\n' "$page_json" >> "$out"
        page_len=$(printf '%s' "$page_json" | python3 -c "
import sys, json
try:
    doc = json.load(sys.stdin)
except Exception:
    print(0)
else:
    print(len(doc) if isinstance(doc, list) else 0)
")
        [ "$page_len" -eq 100 ] || break
        page=$((page + 1))
    done
}

# The newest release approved for train $4 on a box running TrueNAS $2 on
# kernel $3, chosen from the release pages in $5. $1 is the mode: "install"
# (the image is installed, so the release must be built for this kernel and
# this train) or "scripts" (only the release's scripts run: --check, --help,
# uninstall, a user's own image; see the selection below). $6, when given, is
# a file holding the repo's open issues, so the no-match message can name the
# hardware tests that are waiting. Prints the tag; explains on stderr and
# fails when there is none (exit 3 when nothing is approved, 1 on an API or
# parse error).
select_approved_release() {
    MODE="$1" VERSION="$2" KVER="$3" TRAIN="$4" ISSUES_FILE="${6:-}" REPO="$REPO" python3 -c "
# BEGIN release-selection (extracted verbatim by tests/test_release_selection.py;
# single-quoted strings only, \x60 stands for backtick, no dollar signs: this
# code lives inside a double-quoted bash string)
import sys, json, os, re
# stdin carries one JSON array per fetched API page, concatenated.
decoder = json.JSONDecoder()
text = sys.stdin.read()
data = []
pos = 0
while pos < len(text):
    if text[pos].isspace():
        pos += 1
        continue
    try:
        doc, pos = decoder.raw_decode(text, pos)
    except ValueError:
        print('Failed to parse GitHub API response', file=sys.stderr)
        sys.exit(1)
    if isinstance(doc, dict) and 'message' in doc:
        msg = doc['message']
        if 'rate limit' in msg.lower():
            print('GitHub API rate limit exceeded (60 requests/hour for unauthenticated calls).', file=sys.stderr)
            print('Wait a few minutes and try again.', file=sys.stderr)
        else:
            print(f'GitHub API error: {msg}', file=sys.stderr)
        sys.exit(1)
    elif isinstance(doc, list):
        data.extend(doc)
    else:
        print('Failed to parse GitHub API response', file=sys.stderr)
        sys.exit(1)
if not text.strip():
    print('Failed to parse GitHub API response', file=sys.stderr)
    sys.exit(1)
version = os.environ['VERSION']
kver = os.environ['KVER']
# This box's train key, from truenas_train_key (same rule as train_key below).
train = os.environ['TRAIN']
repo = os.environ.get('REPO', '')
# Scripts only (--check, --help, uninstall, a user's own image): no module
# from the release is loaded, so a release built for another kernel of this
# train serves when this kernel has no approved build. See the selection
# below.
scripts_only = os.environ.get('MODE', 'install') == 'scripts'
# Channel gate: a BETA/RC box is on the preview channel and may install
# preview builds, full releases or prereleases (preview builds signed off
# before 2026-10-06 stayed prereleases). A stable box only installs promoted
# (non-prerelease) builds and never a preview build, whatever its flag: an
# unverified stable build stays a prerelease until a human closes its
# hardware-test issue, and auto-installing one would bypass that gate. On
# both channels the approval gate below also applies: no unverified build is
# installed, not even on a preview box.
vu = version.upper()
is_preview = ('-BETA' in vu) or ('-RC' in vu)
hdr_re = re.compile(r'for TrueNAS SCALE (\S+)')
def preview_release(release):
    # Kernel-keyed tags (k6.18.52-...) carry no BETA marker, so the tag check
    # alone does not cover new preview builds; the notes header still names
    # the TrueNAS version they were built for.
    tu = release.get('tag_name', '').upper()
    if ('-BETA' in tu) or ('-RC' in tu):
        return True
    m = hdr_re.search(release.get('body') or '')
    hv = m.group(1).upper() if m else ''
    return ('-BETA' in hv) or ('-RC' in hv)
# Approval gate (per-train sign-off). promote.yml writes one verified-train
# line into the notes when a hardware-test issue for the build closes as
# completed; the train is the one the build was made for. A release with a
# line for this train is approved here; lines for other trains only are not.
# A full release with no line at all was promoted before per-train sign-off
# and is grandfathered for every train, unless it is a preview build. Nothing
# else qualifies: there is no fallback to an unverified build, on stable or
# preview boxes.
vt_re = re.compile(r'^[ \t]*<!--\s*verified-train:\s*([^\s>]+?)\s*-->', re.M)
def verified_trains(release):
    return set(vt_re.findall(release.get('body') or ''))
def approved(release):
    trains = verified_trains(release)
    if trains:
        return train in trains
    return not release.get('prerelease') and not preview_release(release)
def published(release):
    return release.get('published_at') or release.get('created_at') or ''
def train_key(v):
    major, dot, rest = v.partition('.')
    if not major.isdigit():
        return ''
    if int(major) >= 26:
        return major
    minor = re.match(r'[0-9]*', rest).group(0)
    return major + '.' + minor if dot and minor else ''
# The TrueNAS train a build was made for: the version in its notes header (a
# v-tag names it too, a k-tag does not), keyed like truenas_train_key.
def built_train(release):
    m = hdr_re.search(release.get('body') or '')
    v = m.group(1) if m else ''
    if not v:
        tm = re.match(r'v(.+?)-memryx', release.get('tag_name', ''))
        v = tm.group(1) if tm else ''
    return train_key(v)
# Train guard: the sysext also ships userspace (libmemx, mx_accl,
# mxa_manager) staged on a runner matched to a train's base system, so a
# kernel match is only served from the box's own TrueNAS train. A release
# whose train cannot be told (a k-tag whose notes lost their header) passes.
def same_train(release):
    bt = built_train(release)
    return not bt or bt == train
# The Target kernel notes row is the primary key. A k-tag whose body lost the
# row still encodes its short kernel in the tag; check-releases counts such a
# release as covering its kernel (and skips builds for it), so the installer
# must serve it by the same rule. A body row always wins over the tag: it is
# written from REAL_KVER at build time, so a tag/body mismatch means a
# mispublished release that must not be served.
ker_re = re.compile(r'Target kernel\s*\|\s*\x60([^\x60]+)\x60')
def target_kernel(release):
    m = ker_re.search(release.get('body') or '')
    return m.group(1) if m else ''
short = kver.split('-')[0]
def kernel_match(release):
    tk = target_kernel(release)
    if tk:
        return tk == kver
    return release.get('tag_name', '').startswith(f'k{short}-memryx')
candidates = [r for r in data
              if not r.get('draft')
              and (is_preview or (not r.get('prerelease') and not preview_release(r)))
              and approved(r)]
matches = [r for r in candidates if kernel_match(r) and same_train(r)]
cross = [r for r in candidates if kernel_match(r) and not same_train(r)]
for r in ([] if scripts_only else cross):
    print('WARNING: ' + r.get('tag_name', '?') + ' matches kernel ' + kver
          + ' but was built for a different TrueNAS train; not using it'
          + ' (the MemryX userspace must match the train).', file=sys.stderr)
if not matches:
    # Releases published before the Target kernel row existed can only be
    # matched the old way: exact TrueNAS version. Never fall back onto a
    # release that DOES advertise a kernel: a version match with the wrong
    # kernel would ship a module that cannot load.
    prefix = f'v{version}-'
    matches = [r for r in candidates
               if r.get('tag_name', '').startswith(prefix) and not target_kernel(r)]
    if matches:
        print(f'NOTE: no release advertises kernel {kver}; matched by TrueNAS version instead.', file=sys.stderr)
if not matches and scripts_only:
    # With no approved build for this kernel (a box on an untested kernel,
    # say), the scripts of the newest approved release BUILT FOR THIS TRAIN
    # serve. Never another train's, grandfathered or not: an older train's
    # install.sh --check inspects another install layout, and its restore.sh
    # runs another removal flow.
    matches = [r for r in candidates if built_train(r) == train]
if not matches:
    channel = 'preview (beta)' if is_preview else 'stable'
    if scripts_only:
        print(f'No {channel} release built for TrueNAS train {train} is approved yet (TrueNAS {version}).', file=sys.stderr)
        print('Its scripts are needed here, and a release built for another train does not', file=sys.stderr)
        print('serve: pin one with --release=TAG to use it anyway.', file=sys.stderr)
    else:
        print(f'No approved {channel} release found for kernel {kver} (TrueNAS {version}).', file=sys.stderr)
        print(f'Only a release approved for TrueNAS train {train} is installed: a hardware test on', file=sys.stderr)
        print('that train signed it off, or it was promoted before per-train sign-off.', file=sys.stderr)
    # Builds this box would take once approved: its channel, its kernel and
    # train (or for scripts its train), not approved. A stable box never takes
    # a preview build, so none is promised to it.
    pending = sorted([r for r in data
                      if not r.get('draft')
                      and (is_preview or not preview_release(r))
                      and (built_train(r) == train if scripts_only
                           else kernel_match(r) and same_train(r))
                      and not approved(r)], key=published, reverse=True)
    what = f'TrueNAS train {train}' if scripts_only else f'kernel {kver}'
    if pending:
        if is_preview:
            print(f'A build for {what} exists but is a prerelease awaiting its preview hardware', file=sys.stderr)
            print('test; re-run this installer once it is signed off.', file=sys.stderr)
        else:
            print(f'A build for {what} exists but is a prerelease awaiting hardware-test', file=sys.stderr)
            print('promotion; re-run this installer once it is promoted.', file=sys.stderr)
        print('Builds waiting for a hardware test:', file=sys.stderr)
        for r in pending[:5]:
            t = r.get('tag_name', '?')
            mark = ' (prerelease)' if r.get('prerelease') else ''
            print(f'  {t}{mark}', file=sys.stderr)
        # Name the open hardware-test issues for those builds, from the issue
        # list approved_release_tag fetched into ISSUES_FILE.
        issues = None
        path = os.environ.get('ISSUES_FILE', '')
        if path:
            try:
                with open(path) as f:
                    issues = json.load(f)
            except Exception:
                issues = None
        if isinstance(issues, list):
            tags = [r.get('tag_name', '') for r in pending[:5]]
            rt_re = re.compile(r'<!--\s*release-tag:\s*(\S+?)\s*-->')
            waiting = []
            for i in issues:
                if not isinstance(i, dict) or 'pull_request' in i:
                    continue
                names = [(l.get('name') if isinstance(l, dict) else l) for l in i.get('labels') or []]
                if 'hardware-test' not in names and 'preview-hardware-test' not in names:
                    continue
                m = rt_re.search(i.get('body') or '')
                it = m.group(1) if m else ''
                title = i.get('title') or ''
                if it in tags or any(t and t in title for t in tags):
                    waiting.append(i)
            if waiting:
                print('Open hardware-test issues for these builds, waiting for a tester:', file=sys.stderr)
                for i in waiting:
                    num, ttl, url = i.get('number'), i.get('title'), i.get('html_url')
                    print(f'  #{num} {ttl}', file=sys.stderr)
                    print(f'  {url}', file=sys.stderr)
            else:
                print('No hardware-test issue is open for these builds yet.', file=sys.stderr)
        else:
            label = 'preview-hardware-test' if is_preview else 'hardware-test'
            print('Open hardware tests:', file=sys.stderr)
            print(f'  https://github.com/{repo}/issues?q=is%3Aissue+is%3Aopen+label%3A{label}', file=sys.stderr)
    else:
        print(f'No build for {what} is waiting for a hardware test, so no hardware-test issue', file=sys.stderr)
        print('exists for it yet.', file=sys.stderr)
    print('Otherwise a build may not exist yet (the daily check builds within ~24h of an', file=sys.stderr)
    print('ISO going live), or you can build one yourself from the repo. Available releases:', file=sys.stderr)
    for r in [x for x in data if not x.get('draft')]:
        t = r.get('tag_name', '?')
        k = target_kernel(r) or 'no kernel recorded'
        mark = ' (prerelease)' if r.get('prerelease') else ''
        vt = sorted(verified_trains(r))
        if vt:
            mark += ' (approved for train ' + ', '.join(vt) + ')'
        print(f'  {t} ({k}){mark}', file=sys.stderr)
    sys.exit(3)
matches.sort(key=published, reverse=True)
print(matches[0]['tag_name'], end='')
# END release-selection
" < "$5"
}

# The release to use on this box when none is pinned with --release: the
# newest one approved for its TrueNAS train and built for its running kernel
# (and, because of the userspace, for its train). With --scripts-only
# (--check, --help, uninstall, a user's own image: only the release's scripts
# run) that release if there is one, else the newest approved one built for
# this train, never one built for another train. Prints the tag; with nothing
# approved, the message names the hardware tests that are waiting.
approved_release_tag() {
    local mode=install version kver train pages err issues tag="" rc=0
    [ "${1:-}" = --scripts-only ] && mode=scripts
    version=$(detect_truenas_version) || return 1
    # The running kernel is the match key: the module binds to the exact
    # kernel string, and many TrueNAS versions share one kernel, so the right
    # build is the one for this kernel, whichever TrueNAS version produced it.
    kver=$(uname -r 2>/dev/null) || kver=""
    [ -n "$kver" ] || { echo "ERROR: could not read the running kernel (uname -r)" >&2; return 1; }
    train=$(truenas_train_key "$version") || {
        echo "ERROR: cannot derive a TrueNAS train from version '${version}'" >&2
        return 1
    }
    echo "Detected TrueNAS version: ${version} (train ${train}, kernel: ${kver})" >&2
    if [ "$mode" = scripts ]; then
        echo "Searching for an approved release of this train, preferring this kernel..." >&2
    else
        echo "Searching for an approved release matching this kernel..." >&2
    fi
    pages=$(mktemp) || return 1
    err=$(mktemp) || { rm -f "$pages"; return 1; }
    if fetch_release_pages "$pages"; then
        tag=$(select_approved_release "$mode" "$version" "$kver" "$train" "$pages" 2>"$err") || rc=$?
    else
        rc=1
    fi
    if [ "$rc" -eq 3 ]; then
        # Nothing approved: select again with the open issues, so the message
        # names the hardware tests waiting. Best effort: without the issue
        # list it links the open tests instead.
        if issues=$(mktemp); then
            curl -sS --max-time 30 "https://api.github.com/repos/${REPO}/issues?state=open&per_page=100" \
                > "$issues" 2>/dev/null || : > "$issues"
            select_approved_release "$mode" "$version" "$kver" "$train" "$pages" "$issues" \
                > /dev/null 2>"$err" || true
            rm -f "$issues"
        fi
    fi
    cat "$err" >&2
    rm -f "$pages" "$err"
    [ "$rc" -eq 0 ] || return 1
    echo "Found release: ${tag} (approved for TrueNAS train ${train})" >&2
    printf '%s\n' "$tag"
}
# END approved-release

# Download release assets $2... of release $1 into WORK_DIR.
fetch_assets() {
    local tag="$1" asset
    shift
    for asset in "$@"; do
        curl -fsSL --retry 3 --max-time 600 -o "${WORK_DIR}/${asset}" \
            "https://github.com/${REPO}/releases/download/${tag}/${asset}" \
            || { echo "ERROR: could not download ${asset} from release ${tag}" >&2; return 1; }
    done
}

main() {
    local mode=install tag="" arg image=yes
    local -a args=()
    for arg in "$@"; do
        case "$arg" in
            --uninstall) mode=uninstall ;;
            --release=*)
                tag="${arg#*=}"
                [ -n "$tag" ] || { echo "ERROR: --release= needs a tag, e.g. --release=k6.12.105-memryx2.1-r17" >&2; exit 2; }
                ;;
            --repo=*)
                REPO="${arg#*=}"
                [ -n "$REPO" ] || { echo "ERROR: --repo= needs OWNER/NAME" >&2; exit 2; }
                ;;
            *) args+=("$arg") ;;
        esac
    done
    # The release's scripts read the repo from the environment: install.sh's
    # --repo default and uninstall.sh/restore.sh's only override.
    export MEMRYX_REPO="$REPO"

    # Only an install uses an image: --check, --help and --update-firmware do
    # not, and a path on the command line is the user's own image.
    [ "$mode" = install ] || image=no
    for arg in ${args[@]+"${args[@]}"}; do
        case "$arg" in --check|--help|--update-firmware|[!-]*) image=no ;; esac
    done

    if [ -n "$tag" ]; then
        echo "Release ${tag} (pinned with --release)" >&2
    elif [ "$image" = yes ]; then
        tag=$(approved_release_tag) || exit 1
    else
        tag=$(approved_release_tag --scripts-only) || exit 1
    fi

    WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/memryx-get.XXXXXX")
    trap 'rm -rf "$WORK_DIR"' EXIT

    if [ "$mode" = uninstall ]; then
        # uninstall.sh runs the restore.sh beside it, and restore.sh sources
        # the memryx-lib.sh beside it, so all three come from the release.
        fetch_assets "$tag" uninstall.sh restore.sh memryx-lib.sh || exit 1
        bash "${WORK_DIR}/uninstall.sh" ${args[@]+"${args[@]}"}
        return
    fi

    # install.sh sources the memryx-lib.sh beside it.
    fetch_assets "$tag" install.sh memryx-lib.sh || exit 1
    if [ "$image" = yes ]; then
        fetch_assets "$tag" memryx.raw memryx.raw.sha256 || exit 1
        (cd "$WORK_DIR" && sha256sum -c memryx.raw.sha256) >&2 \
            || { echo "ERROR: checksum verification failed for memryx.raw from release ${tag}" >&2; exit 1; }
        args+=("${WORK_DIR}/memryx.raw")
    fi
    # An installer from per-train approval on also records which release the
    # image came from; older ones reject the flag (every release back to r1
    # refuses an unknown option), and the image alone is enough for them.
    if grep -q -e '^[[:space:]]*--release=\*)' "${WORK_DIR}/install.sh"; then
        args=("--release=${tag}" ${args[@]+"${args[@]}"})
    fi
    bash "${WORK_DIR}/install.sh" ${args[@]+"${args[@]}"}
}

# Called on the last line, so bash has read this whole script before
# anything runs and the installer cannot swallow the rest of it from stdin.
main "$@"
