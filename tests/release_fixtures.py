"""Shared release fixture mirroring the body build.yml's notes template renders.

The release-selection block in get.sh / install.sh / uninstall.sh /
restore.sh parses that body, so a template change breaks the suite instead of
silently reverting every new release to a no-match.

Per-train approval: `verified` appends one verified-train line per train in
the form promote.yml writes it (tests/test_promote.py holds the form to what
promote.yml actually appends).
"""
import zlib


def marker(train):
    return f"<!-- verified-train: {train} -->"


def release(tag, version="", train="Goldeye", kver=None, prerelease=False,
            draft=False, published="2026-01-01T00:00:00Z", verified=()):
    body = (f"## MemryX MX3 Sysext for TrueNAS SCALE {version} ({train})\n"
            "| Field | Value |\n| --- | --- |\n"
            "| MemryX SDK | `2.1` (packages `2.1.0`, apt channel `stable`) |\n")
    if kver:
        body += f"| Target kernel | `{kver}` |\n"
    for t in verified:
        body += f"\n\n{marker(t)}\n"
    return {"id": zlib.crc32(tag.encode()), "tag_name": tag, "body": body,
            "prerelease": prerelease, "draft": draft,
            "html_url": f"https://example.test/{tag}",
            "published_at": published, "created_at": published}


def issue(tag, labels=("hardware-test",), number=1, preview=None, title=None):
    """A hardware-test issue carrying the markers build.yml writes
    (release-tag, and preview-build when `preview` is not None)."""
    body = f"**Release:** {tag}\n<!-- release-tag: {tag} -->\n"
    if preview is not None:
        body += f"<!-- preview-build: {'true' if preview else 'false'} -->\n"
    return {"number": number, "title": title or f"Hardware test: {tag}",
            "body": body, "labels": [{"name": name} for name in labels],
            "html_url": f"https://example.test/issues/{number}"}
