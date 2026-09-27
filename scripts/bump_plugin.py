#!/usr/bin/env python
"""
Bump the xFlow plugin version — two-phase workflow.

Phase 1 — prepare (run first):
    python scripts/bump_plugin.py 1.1.0

  1. Updates PLUGIN_VERSION in xplane_plugin/xFlow/PI_xFlow.py and
     CURRENT_PLUGIN_VERSION in checklist/plugin_version.py
  2. Prepends a stub section to xplane_plugin/CHANGELOG.md
  3. Commits: "chore(plugin): bump version to X.Y.Z"
  4. Tags: plugin-vX.Y.Z

  → Edit xplane_plugin/CHANGELOG.md to fill in the release notes.

Phase 2 — commit & push (run after editing the CHANGELOG):
    python scripts/bump_plugin.py --push

  1. Stages xplane_plugin/CHANGELOG.md
  2. Amends the bump commit (preserves message)
  3. Moves the tag to the amended commit
  4. Pushes branch + tag

Run from the repo root.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILE = REPO_ROOT / "xplane_plugin" / "xFlow" / "PI_xFlow.py"
PLUGIN_CHANGELOG = REPO_ROOT / "xplane_plugin" / "CHANGELOG.md"

# The app's copy of the plugin version. It has to move in the same commit:
# release-deploy.yaml excludes xplane_plugin from the rsync, so the server
# cannot read PI_xFlow.py and this file is the only thing it has to compare a
# client's reported version against.
VERSION_MARKER = REPO_ROOT / "checklist" / "plugin_version.py"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, cwd=REPO_ROOT)


def _current_version() -> str:
    text = PLUGIN_FILE.read_text(encoding="utf-8")
    m = re.search(r'^PLUGIN_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not m:
        sys.exit(f"ERROR: PLUGIN_VERSION not found in {PLUGIN_FILE}")
    return m.group(1)


def _set_version(new_ver: str) -> None:
    text = PLUGIN_FILE.read_text(encoding="utf-8")
    new_text = re.sub(
        r'^(PLUGIN_VERSION\s*=\s*["\'])[^"\']+(["\'])',
        rf"\g<1>{new_ver}\g<2>",
        text,
        flags=re.MULTILINE,
    )
    if new_text == text:
        sys.exit("ERROR: PLUGIN_VERSION replacement had no effect — check the file.")
    PLUGIN_FILE.write_text(new_text, encoding="utf-8")


def _set_marker_version(new_ver: str) -> None:
    """Keep checklist/plugin_version.py in step with PI_xFlow.py."""
    text = VERSION_MARKER.read_text(encoding="utf-8")
    new_text = re.sub(
        r'^(CURRENT_PLUGIN_VERSION\s*=\s*")[^"]+(")',
        rf"\g<1>{new_ver}\g<2>",
        text,
        flags=re.MULTILINE,
    )
    if new_text == text:
        sys.exit(
            f"ERROR: CURRENT_PLUGIN_VERSION replacement had no effect in "
            f"{VERSION_MARKER.relative_to(REPO_ROOT)} — check the file."
        )
    VERSION_MARKER.write_text(new_text, encoding="utf-8")


def _prepend_changelog(new_ver: str) -> None:
    today = __import__("datetime").date.today().isoformat()
    header = f"\n## [{new_ver}] — {today}\n\n### Changed\n- (fill in release notes)\n"
    text = PLUGIN_CHANGELOG.read_text(encoding="utf-8")
    # Insert after the first H1 line
    lines = text.splitlines(keepends=True)
    insert_at = next(
        (i + 1 for i, line in enumerate(lines) if line.startswith("# ")),
        0,
    )
    lines.insert(insert_at, header)
    PLUGIN_CHANGELOG.write_text("".join(lines), encoding="utf-8")
    print(f"  → Remember to fill in the release notes in {PLUGIN_CHANGELOG.relative_to(REPO_ROOT)}")


def _push_phase() -> None:
    new_ver = _current_version()
    tag = f"plugin-v{new_ver}"
    changelog_rel = str(PLUGIN_CHANGELOG.relative_to(REPO_ROOT))

    print(f"Phase 2: amend + push for plugin-v{new_ver}")

    print("Staging CHANGELOG …")
    _run(["git", "add", changelog_rel])

    print("Amending commit …")
    _run(["git", "commit", "--amend", "--no-edit"])

    print(f"Moving tag {tag} …")
    _run(["git", "tag", "-f", tag])

    print("Pushing branch …")
    _run(["git", "push"])

    print(f"Pushing tag {tag} …")
    _run(["git", "push", "origin", tag])

    print(f"\nDone! plugin-v{new_ver} pushed.")


def main() -> None:
    if "--push" in sys.argv:
        _push_phase()
        return

    if len(sys.argv) != 2:
        sys.exit(
            f"Usage: python {Path(__file__).name} <new-version>  (e.g. 1.1.0)\n"
            f"       python {Path(__file__).name} --push          (after editing CHANGELOG)"
        )

    new_ver = sys.argv[1].lstrip("v")
    old_ver = _current_version()

    print(f"Bumping plugin version: {old_ver} → {new_ver}")

    _set_version(new_ver)
    _set_marker_version(new_ver)
    _prepend_changelog(new_ver)

    print("Staging files …")
    _run(["git", "add",
          str(PLUGIN_FILE.relative_to(REPO_ROOT)),
          str(VERSION_MARKER.relative_to(REPO_ROOT)),
          str(PLUGIN_CHANGELOG.relative_to(REPO_ROOT))])

    print("Committing …")
    _run(["git", "commit", "-m", f"chore(plugin): bump version to {new_ver}"])

    tag = f"plugin-v{new_ver}"
    print(f"Tagging {tag} …")
    _run(["git", "tag", tag])

    print(f"\nPhase 1 done. Edit the release notes in xplane_plugin/CHANGELOG.md,")
    print(f"then run:  python {Path(__file__).name} --push")


if __name__ == "__main__":
    main()
