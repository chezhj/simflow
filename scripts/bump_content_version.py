#!/usr/bin/env python
"""
Bump the content version of a SOP.

Usage:
    python scripts/bump_content_version.py B738 1.1.0

The fixture (checklist/fixtures/checklist_content.json) is the source of truth for
checklist content — you edit it by hand and import it. This script follows that
direction: it writes the new version into the fixture, then loads the fixture into
the dev database so the two agree.

What it does:
  1. Finds the SOP with the given ICAO code in the fixture.
  2. Drafts release notes from git log (commits touching migrations/ and fixtures/)
     since the previous sop-{icao}-v* tag.
  3. Opens the draft in $EDITOR (falls back to printing it) for you to edit.
  4. Writes content_version and release_notes into the fixture.
  5. Loads the fixture into the dev DB (manage.py checklist_content import).
  6. Commits the fixture: "chore(sop): bump B738 content to X.Y.Z"
  7. Creates a git tag: sop-B738-vX.Y.Z

Run from the repo root with the Django dev settings active.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGE = REPO_ROOT / "manage.py"
FIXTURE = REPO_ROOT / "checklist" / "fixtures" / "checklist_content.json"


def _run(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        check=True,
        cwd=REPO_ROOT,
        capture_output=capture,
        text=capture,
    )


def _draft_notes(icao: str, new_ver: str) -> str:
    """
    Build a draft of release notes from git log entries that touched
    checklist/migrations/ or checklist/fixtures/ since the last sop-{icao}-v* tag.
    """
    tag_prefix = f"sop-{icao}-v"
    # Find the most recent matching tag
    result = subprocess.run(
        ["git", "tag", "--list", f"{tag_prefix}*", "--sort=-version:refname"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    tags = result.stdout.strip().splitlines()
    prev_tag = tags[0] if tags else None

    range_spec = f"{prev_tag}..HEAD" if prev_tag else "HEAD"
    log_result = subprocess.run(
        [
            "git", "log", range_spec,
            "--oneline",
            "--",
            "checklist/migrations/",
            "checklist/fixtures/",
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    log_lines = log_result.stdout.strip()

    since_note = f"since {prev_tag}" if prev_tag else "all history"
    draft = (
        f"# Release notes for {icao} SOP v{new_ver}\n"
        f"# Edit this file, save and close to continue.\n"
        f"# Lines starting with # are ignored.\n\n"
        f"## [{new_ver}]\n\n"
        f"### Changed\n"
        f"- (describe your checklist changes here)\n\n"
        f"### Commits ({since_note})\n"
    )
    if log_lines:
        for line in log_lines.splitlines():
            draft += f"# {line}\n"
    else:
        draft += "# (no migration/fixture commits found)\n"

    return draft


def _open_editor(draft: str) -> str:
    """Open draft in $EDITOR and return the final text."""
    editor = os.environ.get("EDITOR", "")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(draft)
        tmp_path = f.name

    if editor:
        subprocess.run([editor, tmp_path], check=True)
    else:
        print("\n--- DRAFT RELEASE NOTES (no $EDITOR set — edit the file manually) ---")
        print(f"File: {tmp_path}")
        print(draft)
        input("Press Enter when you have finished editing the file above …")

    text = Path(tmp_path).read_text(encoding="utf-8")
    Path(tmp_path).unlink(missing_ok=True)

    # Strip comment lines
    lines = [l for l in text.splitlines() if not l.startswith("#")]
    return "\n".join(lines).strip()


def _update_fixture(icao: str, new_ver: str, notes: str) -> None:
    """
    Write the new version and notes into the SOP record in the fixture.

    Rewritten the same way the export command writes it: ASCII-escaped, indent=2.
    A raw-UTF-8 fixture is prone to cp1252 re-corruption on Windows, which would
    then bake mojibake into the DB on the next import.
    """
    if not FIXTURE.exists():
        sys.exit(f"Fixture not found: {FIXTURE}")

    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)

    matches = [
        obj for obj in data
        if obj.get("model") == "checklist.sop"
        and obj.get("fields", {}).get("icao_code") == icao
    ]
    if not matches:
        sys.exit(f"No checklist.sop record with icao_code '{icao}' in {FIXTURE.name}.")
    if len(matches) > 1:
        sys.exit(f"Multiple checklist.sop records with icao_code '{icao}' in {FIXTURE.name}.")

    fields = matches[0]["fields"]
    old_ver = fields.get("content_version")
    fields["content_version"] = new_ver
    fields["release_notes"] = notes
    fields["updated_at"] = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

    with open(FIXTURE, "w", encoding="ascii") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)

    print(f"  {FIXTURE.name}: {icao} {old_ver} -> {new_ver}")


def _load_fixture() -> None:
    """Load the fixture into the dev DB so it matches what was just written."""
    _run([sys.executable, str(MANAGE), "checklist_content", "import"])


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(
            f"Usage: python {Path(__file__).name} <icao-code> <new-version>\n"
            "  e.g. python scripts/bump_content_version.py B738 1.1.0"
        )

    icao = sys.argv[1].upper()
    new_ver = sys.argv[2].lstrip("v")

    print(f"Bumping {icao} SOP content version to {new_ver} …")

    draft = _draft_notes(icao, new_ver)
    notes = _open_editor(draft)

    if not notes:
        sys.exit("Aborted — release notes are empty.")

    print("Updating fixture …")
    _update_fixture(icao, new_ver, notes)

    print("Loading fixture into the dev database …")
    _load_fixture()

    print("Committing …")
    _run(["git", "add", str(FIXTURE)])
    _run(["git", "commit", "-m", f"chore(sop): bump {icao} content to {new_ver}"])

    tag = f"sop-{icao}-v{new_ver}"
    print(f"Tagging {tag} …")
    _run(["git", "tag", tag])

    print(f"\nDone. Push with:\n  git push && git push origin {tag}")


if __name__ == "__main__":
    main()
