#!/usr/bin/env python
"""
Refuse a release whose checklist content moved without a content_version bump.

Wired into commitizen as a pre_bump_hook, so `cz bump` stops when the fixture's
content differs from what it was when the current content_version was set. The
answer it gives is the local half of the rule docs/SOP_VERSIONING_SPEC.md plans to
enforce server-side at import time.

How it decides:
  1. Read the fixture at HEAD — that is what cz is about to tag — and take its
     content_version.
  2. Walk the fixture's history newest to oldest. The last commit still carrying
     that version is the commit that set it.
  3. Compare the content hash there against HEAD's. Differ -> a bump is needed.

Usage:
    python scripts/check_content_bump.py

Exit codes:
    0  content and version agree (or the check was skipped)
    1  a content_version bump is needed, or content is uncommitted
    2  the check could not be made (no git history for the fixture, unreadable blob)

Override:
    SIMFLOW_SKIP_CONTENT_CHECK=1 cz bump

Standard library only, so it runs under whatever `python` the shell resolves to and
does not care whether the project venv is active.

Known limits:
  - One SOP per fixture is assumed. The hash covers the whole file, so with several
    SOP records any content change would flag all of them. checklist_content.py notes
    that a fixture describes one SOP, and the multi-version future in
    SOP_VERSIONING_SPEC.md moves this check server-side anyway.
  - Only the fixture is covered. A migration that changes content semantics is not
    seen here, though bump_content_version.py does look at checklist/migrations/ when
    it drafts release notes.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from checklist.content_hash import (  # noqa: E402  (needs REPO_ROOT on sys.path)
    SOP_MODEL,
    content_diff,
    content_hash,
)

# Forward slashes: this is a git pathspec, not a filesystem path.
FIXTURE_PATH = "checklist/fixtures/checklist_content.json"

SKIP_ENV = "SIMFLOW_SKIP_CONTENT_CHECK"

EXIT_OK = 0
EXIT_BUMP_NEEDED = 1
EXIT_UNDETERMINED = 2


class Undetermined(Exception):
    """The check could not be made — reported as exit 2, never as a pass."""


def _git(*args, root: Path = REPO_ROOT) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Undetermined(
            f"git {' '.join(args)} failed: {result.stderr.strip() or 'no output'}"
        )
    return result.stdout


def fixture_at(sha: str, root: Path = REPO_ROOT) -> list:
    """The fixture as it stood at a commit."""
    try:
        return json.loads(_git("show", f"{sha}:{FIXTURE_PATH}", root=root))
    except json.JSONDecodeError as exc:
        raise Undetermined(f"fixture at {sha} is not valid JSON: {exc}") from exc


def versions(data: list) -> dict:
    """{icao_code: content_version} for every SOP the fixture carries."""
    return {
        obj["fields"]["icao_code"]: obj["fields"]["content_version"]
        for obj in data
        if obj["model"] == SOP_MODEL
    }


def setter_commit(target_versions: dict, root: Path = REPO_ROOT) -> str:
    """
    The commit that set the given content_version(s).

    Walks the fixture's history newest first; the last commit still carrying these
    versions is where they were introduced. When every commit carries them — the
    version has never been bumped — that is the first commit touching the fixture.
    """
    shas = _git("log", "--format=%H", "--", FIXTURE_PATH, root=root).split()
    if not shas:
        raise Undetermined(f"no commits touch {FIXTURE_PATH}")

    setter = None
    for sha in shas:
        if versions(fixture_at(sha, root=root)) != target_versions:
            break
        setter = sha
    if setter is None:
        raise Undetermined(
            "the fixture at HEAD does not match its own last commit — "
            "commit the fixture before releasing"
        )
    return setter


def commits_since(sha: str, root: Path = REPO_ROOT) -> list:
    """One-line log of fixture commits after the given commit."""
    log = _git(
        "log", "--format=%h %s", f"{sha}..HEAD", "--", FIXTURE_PATH, root=root
    ).strip()
    return log.splitlines() if log else []


def _format_rows(rows: list) -> list:
    """Group (model, pk) pairs into one readable line per model."""
    by_model: dict = {}
    for model, pk in rows:
        by_model.setdefault(model.replace("checklist.", ""), []).append(pk)
    return [
        f"    {model:<11}{'pk ' + ', '.join(str(pk) for pk in sorted(pks))}"
        for model, pks in sorted(by_model.items())
    ]


def _report_diff(old: list, new: list) -> None:
    diff = content_diff(old, new)
    for label in ("added", "changed", "removed"):
        rows = diff[label]
        if not rows:
            continue
        for line in _format_rows(rows):
            print(f"{line:<52}{label}")


def main() -> int:
    if os.environ.get(SKIP_ENV):
        print(f"{SKIP_ENV} set — content version check skipped.")
        return EXIT_OK

    try:
        working = json.loads((REPO_ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
        head = fixture_at("HEAD")

        # Content edited but not committed would be left out of the release entirely.
        if content_hash(working) != content_hash(head):
            print("\n✗ The fixture has uncommitted content changes.\n")
            _report_diff(head, working)
            print(
                "\n  These are not in HEAD, so they would not ship with this release.\n"
                "  Commit them (and bump the content version) before bumping.\n"
            )
            _print_undo()
            return EXIT_BUMP_NEEDED

        head_versions = versions(head)
        if not head_versions:
            raise Undetermined("no checklist.sop record in the fixture")

        setter = setter_commit(head_versions)
        if content_hash(fixture_at(setter)) == content_hash(head):
            labels = ", ".join(f"{k} {v}" for k, v in sorted(head_versions.items()))
            print(f"Content version check: OK ({labels}).")
            return EXIT_OK

        _report_bump_needed(head, head_versions, setter)
        return EXIT_BUMP_NEEDED

    except Undetermined as exc:
        print(
            f"\n✗ Could not check whether a content bump is needed: {exc}\n"
            "\n  This is not a pass — the check did not run.\n"
            f"  To release anyway:  {SKIP_ENV}=1 cz bump\n"
        )
        _print_undo()
        return EXIT_UNDETERMINED


def _report_bump_needed(head: list, head_versions: dict, setter: str) -> None:
    labels = ", ".join(f"{k} content_version {v}" for k, v in sorted(head_versions.items()))
    described = _git("log", "-1", "--format=%h (%ad) %s", "--date=short", setter).strip()

    print(f"\n✗ Content changed since {labels} was set.\n")
    print(f"  Set in {described}")
    print("  Since then the fixture's content has changed:\n")
    _report_diff(fixture_at(setter), head)

    log = commits_since(setter)
    if log:
        print(f"\n  {len(log)} commit(s) touch the fixture since then:")
        for line in log:
            print(f"    {line}")

    icao = sorted(head_versions)[0]
    print(
        "\n  cz has already written the version files, but nothing was committed\n"
        "  or tagged. Undo, bump the content, then retry:\n"
    )
    _print_undo()
    print(
        f"    python scripts/bump_content_version.py {icao} <next-version>\n"
        "    cz bump\n"
        f"\n  To release anyway:  {SKIP_ENV}=1 cz bump\n"
    )


def _print_undo() -> None:
    print(
        "    git restore pyproject.toml smart_training_checklist/__init__.py CHANGELOG.md"
    )


if __name__ == "__main__":
    sys.exit(main())
