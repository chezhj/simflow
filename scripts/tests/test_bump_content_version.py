"""Unit tests for the content bump script - the release-notes draft and the absence of a tag."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Loaded by path: scripts/ is not a package, and the script is meant to be run
# rather than imported.
_spec = importlib.util.spec_from_file_location(
    "bump_content_version", REPO_ROOT / "scripts" / "bump_content_version.py"
)
bump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump)

FIXTURE_PATH = "checklist/fixtures/checklist_content.json"


def _fixture(version, rule_value=1):
    return [
        {
            "model": "checklist.sop",
            "pk": 1,
            "fields": {
                "icao_code": "B738",
                "content_version": version,
                "release_notes": "",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        },
        {
            "model": "checklist.checkitem",
            "pk": 10,
            "fields": {"item": "Autobrake", "auto_check_rule": {"value": rule_value}},
        },
    ]


class TestDraftNotes(unittest.TestCase):
    """Notes are drafted from the commits since the previous bump, not from a tag."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.root / FIXTURE_PATH).parent.mkdir(parents=True)
        self._run("git", "init", "-q")
        self._run("git", "config", "user.email", "test@example.com")
        self._run("git", "config", "user.name", "Test")

    def _run(self, *args):
        return subprocess.run(
            args, cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def _commit(self, data, message):
        (self.root / FIXTURE_PATH).write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._run("git", "add", FIXTURE_PATH)
        self._run("git", "commit", "-q", "-m", message)
        return self._run("git", "rev-parse", "HEAD")

    def test_lists_only_commits_since_the_previous_bump(self):
        self._commit(_fixture("1.0.0"), "fix(content): before the bump")
        self._commit(_fixture("1.1.0"), "chore(sop): bump B738 content to 1.1.0")
        self._commit(_fixture("1.1.0", rule_value=2), "fix(content): after the bump")

        draft = bump._draft_notes("B738", "1.2.0", root=self.root)

        self.assertIn("after the bump", draft)
        self.assertNotIn("before the bump", draft)
        self.assertNotIn("bump B738 content to 1.1.0", draft)

    def test_names_the_previous_bump_commit_in_the_header(self):
        self._commit(_fixture("1.0.0"), "fix(content): first")
        previous = self._commit(_fixture("1.1.0"), "chore(sop): bump B738 content to 1.1.0")
        self._commit(_fixture("1.1.0", rule_value=2), "fix(content): after the bump")

        draft = bump._draft_notes("B738", "1.2.0", root=self.root)

        self.assertIn(f"since {previous[:7]}", draft)

    def test_works_without_any_tag(self):
        """Untagged history (the 1.5.x releases) must not widen the range."""
        self._commit(_fixture("1.0.0"), "fix(content): old")
        self._commit(_fixture("1.1.0"), "chore(sop): bump B738 content to 1.1.0")
        self._commit(_fixture("1.1.0", rule_value=2), "fix(content): new")
        self.assertEqual(self._run("git", "tag", "--list"), "")

        draft = bump._draft_notes("B738", "1.2.0", root=self.root)

        self.assertIn("fix(content): new", draft)
        self.assertNotIn("fix(content): old", draft)

    def test_falls_back_to_all_history_without_a_fixture_history(self):
        self.assertIsNone(bump._previous_bump(self.root))
        self.assertIn("all history", bump._draft_notes("B738", "1.0.0", root=self.root))


class TestMainDoesNotTag(unittest.TestCase):

    def test_bump_commits_and_creates_no_tag(self):
        calls = []
        with mock.patch.object(sys, "argv", ["bump", "B738", "1.2.0"]), \
                mock.patch.object(bump, "_draft_notes", return_value="draft"), \
                mock.patch.object(bump, "_open_editor", return_value="- a change"), \
                mock.patch.object(bump, "_update_fixture"), \
                mock.patch.object(bump, "_load_fixture"), \
                mock.patch.object(bump, "_run", side_effect=lambda cmd, **kw: calls.append(cmd)):
            bump.main()

        self.assertTrue(any(c[:2] == ["git", "commit"] for c in calls))
        self.assertFalse(any("tag" in c for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
