#!/usr/bin/env bash
# Uninstall the MemryX MX3 sysext. Thin alias for restore.sh, kept under
# this name because users searching for "uninstall" won't grep for
# "restore". restore.sh is still shipped in releases for backwards
# compatibility with old install instructions.
#
# Usage: curl -fsSL https://raw.githubusercontent.com/truenas-community-sysexts/memryx-mx3-support/main/get.sh | sudo bash -s -- --uninstall
#    or: curl -fsSL <release-url>/uninstall.sh | sudo bash
#    or: sudo ./uninstall.sh [--release=TAG] [--force]
#
# --release=TAG fetches restore.sh from that release instead of the newest
# approved one (only when there is no restore.sh beside this script); every
# other argument goes to restore.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

TAG=""
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --release=*)
            TAG="${arg#*=}"
            [ -n "$TAG" ] || { echo "ERROR: --release= requires a release tag" >&2; exit 2; }
            ;;
        *) ARGS+=("$arg") ;;
    esac
done

# When piped through `curl | sudo bash`, $0 is /dev/stdin and there is no
# sibling restore.sh on disk. Detect that case and fetch restore.sh from
# the release chosen below. Otherwise (run from a checked-out tree, an
# extracted release tarball, or get.sh's download dir), exec the sibling
# directly.
if [ -f "${SCRIPT_DIR}/restore.sh" ]; then
    exec bash "${SCRIPT_DIR}/restore.sh" ${ARGS[@]+"${ARGS[@]}"}
fi

# Fallback: stdin path. MEMRYX_REPO is honored to match install.sh's --repo=
# override.
REPO="${MEMRYX_REPO:-truenas-community-sysexts/memryx-mx3-support}"

# BEGIN approved-release (a verbatim copy lives in get.sh, scripts/install.sh,
# scripts/uninstall.sh and scripts/restore.sh, each a self-contained curl|bash
# script; tests/test_release_selection.py fails CI when the copies differ)

# TrueNAS version of this box: the version string decides the release channel
# (stable vs preview) and the train, and is matched against the release tags.
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
# nvidia-driver-support's get.sh; promote.yml's trainKey is held to it by
# tests/test_promote.py.
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
# drops once the repo outgrows it, and the scripts-only fallback needs them.
# Only a full page can have more behind it; anything else (short page, API
# error object) ends the loop, and the selection reports API errors.
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

# The newest release approved for train $3 on a box running TrueNAS $2,
# chosen from the release pages in $4. $1 is the mode: "install" (the image
# is installed, so the release must be the one built for this exact TrueNAS
# version) or "scripts" (only the release's scripts run: --check, --help,
# uninstall, a user's own image; see the selection below). $5, when given, is
# a file holding the repo's open issues, so the no-match message can name the
# hardware tests that are waiting. Prints the tag; explains on stderr and
# fails when there is none (exit 3 when nothing is approved, 1 on an API or
# parse error).
select_approved_release() {
    MODE="$1" VERSION="$2" TRAIN="$3" ISSUES_FILE="${5:-}" REPO="$REPO" python3 -c "
# BEGIN release-selection (extracted verbatim by tests/test_release_selection.py;
# single-quoted strings only, no backticks and no dollar signs: this code lives
# inside a double-quoted bash string)
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
train = os.environ['TRAIN']
repo = os.environ.get('REPO', '')
# Scripts only (--check, --help, uninstall, a user's own image): no module
# from the release is loaded, so a release built for another version of this
# train serves when this version has no approved build. See the selection
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
# and is grandfathered for every train. Nothing else qualifies: there is no
# fallback to an unverified build, on stable or preview boxes.
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
# The TrueNAS train a build was made for: the version in its notes header
# (the tag names it too), keyed like truenas_train_key.
def built_train(release):
    m = hdr_re.search(release.get('body') or '')
    v = m.group(1) if m else ''
    if not v:
        tm = re.match(r'v(.+?)-memryx', release.get('tag_name', ''))
        v = tm.group(1) if tm else ''
    major, dot, rest = v.partition('.')
    if not major.isdigit():
        return ''
    if int(major) >= 26:
        return major
    minor = ''
    for ch in rest:
        if not ch.isdigit():
            break
        minor += ch
    return major + '.' + minor if dot and minor else ''
# The image is built against one TrueNAS version's kernel headers, so an
# install still matches the tag to this box's exact version. The prefix is
# the full version string, so a stable box can never match a BETA/RC release;
# the prerelease and preview checks are the second lock on that.
prefix = f'v{version}-'
candidates = [r for r in data
              if not r.get('draft')
              and (is_preview or (not r.get('prerelease') and not preview_release(r)))
              and approved(r)]
if scripts_only:
    # Only a release built for this box's own train serves, grandfathered or
    # not: an older train's install.sh --check inspects another install
    # layout, and its restore.sh runs another removal flow. The build for
    # this TrueNAS version if it is approved, else the newest approved one of
    # the train.
    own = [r for r in candidates if built_train(r) == train]
    matches = [r for r in own if r.get('tag_name', '').startswith(prefix)] or own
else:
    matches = [r for r in candidates if r.get('tag_name', '').startswith(prefix)]
if not matches:
    channel = 'preview (beta)' if is_preview else 'stable'
    if scripts_only:
        print(f'No {channel} release built for TrueNAS train {train} is approved yet (TrueNAS {version}).', file=sys.stderr)
        print('Its scripts are needed here, and a release built for another train does not', file=sys.stderr)
        print('serve: pin one with --release=TAG to use it anyway.', file=sys.stderr)
    else:
        print(f'No approved {channel} release found for TrueNAS version {version}.', file=sys.stderr)
        print(f'Only a release approved for TrueNAS train {train} is installed: a hardware test on', file=sys.stderr)
        print('that train signed it off, or it was promoted before per-train sign-off.', file=sys.stderr)
    # Builds this box would take once approved: its channel, its TrueNAS
    # version (or for scripts its train), not approved. A stable box never
    # takes a preview build, so none is promised to it.
    pending = sorted([r for r in data
                      if not r.get('draft')
                      and (is_preview or not preview_release(r))
                      and (built_train(r) == train if scripts_only
                           else r.get('tag_name', '').startswith(prefix))
                      and not approved(r)], key=published, reverse=True)
    what = f'TrueNAS train {train}' if scripts_only else f'TrueNAS {version}'
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
        mark = ' (prerelease)' if r.get('prerelease') else ''
        print(f'  {t}{mark}', file=sys.stderr)
    sys.exit(3)
matches.sort(key=published, reverse=True)
print(matches[0]['tag_name'], end='')
# END release-selection
" < "$4"
}

# The release to use on this box when none is pinned with --release: the
# newest one approved for its TrueNAS train and built for its exact TrueNAS
# version. With --scripts-only (--check, --help, uninstall, a user's own
# image: only the release's scripts run) that release if it is approved, else
# the newest approved one built for this train, never one built for another
# train. Prints the tag; with nothing approved, the message names the
# hardware tests that are waiting.
approved_release_tag() {
    local mode=install version train pages err issues tag="" rc=0
    [ "${1:-}" = --scripts-only ] && mode=scripts
    version=$(detect_truenas_version) || return 1
    train=$(truenas_train_key "$version") || {
        echo "ERROR: cannot derive a TrueNAS train from version '${version}'" >&2
        return 1
    }
    echo "Detected TrueNAS version: ${version} (train ${train})" >&2
    if [ "$mode" = scripts ]; then
        echo "Searching for an approved release of this train, preferring this TrueNAS version..." >&2
    else
        echo "Searching for an approved release matching this TrueNAS version..." >&2
    fi
    pages=$(mktemp) || return 1
    err=$(mktemp) || { rm -f "$pages"; return 1; }
    if fetch_release_pages "$pages"; then
        tag=$(select_approved_release "$mode" "$version" "$train" "$pages" 2>"$err") || rc=$?
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
            select_approved_release "$mode" "$version" "$train" "$pages" "$issues" \
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

# restore.sh comes from the release approved for this box's TrueNAS version,
# or else the newest approved release built for its train (removing the
# sysext loads no modules; another train's restore.sh would run another
# removal flow), the rule get.sh and install.sh select by, not from whatever
# GitHub marks Latest.
if [ -z "$TAG" ]; then
    TAG=$(approved_release_tag --scripts-only) || {
        echo "ERROR: no approved release to take restore.sh from; pin one with --release=TAG." >&2
        exit 1
    }
fi
BASE_URL="https://github.com/${REPO}/releases/download/${TAG}"
echo "uninstall.sh: fetching restore.sh + memryx-lib.sh from ${REPO} release ${TAG}..." >&2
TMPDIR=$(mktemp -d /tmp/memryx-uninstall.XXXXXXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT
if ! curl -fsSL --max-time 60 "${BASE_URL}/restore.sh" -o "${TMPDIR}/restore.sh"; then
    echo "ERROR: failed to download restore.sh from ${REPO} release ${TAG}" >&2
    exit 1
fi
if [ ! -s "${TMPDIR}/restore.sh" ]; then
    echo "ERROR: downloaded restore.sh is empty (${REPO} release ${TAG})" >&2
    exit 1
fi
# memryx-lib.sh is a shared library that restore.sh sources at startup.
# A download failure is not fatal here: restore.sh has its own fallback
# that re-fetches the lib if the sibling is missing.
curl -fsSL --max-time 30 "${BASE_URL}/memryx-lib.sh" -o "${TMPDIR}/memryx-lib.sh" 2>/dev/null || true
bash "${TMPDIR}/restore.sh" ${ARGS[@]+"${ARGS[@]}"}
exit $?
