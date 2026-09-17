"""Unit tests for the pre-bump content check — pure Python, no Django required."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from checklist.content_hash import (  # noqa: E402  (needs REPO_ROOT on sys.path)
    content_diff,
    content_hash,
    content_payload,
)

# Loaded by path: scripts/ is not a package, and the script is meant to be run
# rather than imported.
_spec = importlib.util.spec_from_file_location(
    "check_content_bump", REPO_ROOT / "scripts" / "check_content_bump.py"
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _fixture(version="1.0.0", notes="first", rule_value=1, attributes=None):
    """A miniature fixture with the shape the real one has."""
    return [
        {
            "model": "checklist.sop",
            "pk": 1,
            "fields": {
                "name": "Boeing 737-800",
                "icao_code": "B738",
                "content_version": version,
                "release_notes": notes,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        },
        {
            "model": "checklist.checkitem",
            "pk": 10,
            "fields": {
                "item": "Autobrake",
                "auto_check_rule": {"dataref": "a/b", "op": "eq", "value": rule_value},
                "attributes": [3, 4] if attributes is None else attributes,
            },
        },
    ]


class TestContentPayload(unittest.TestCase):

    def test_release_fields_are_stripped(self):
        fields = content_payload(_fixture())[1]["fields"]   # sop sorts after checkitem
        self.assertNotIn("content_version", fields)
        self.assertNotIn("release_notes", fields)
        self.assertNotIn("updated_at", fields)
        self.assertEqual(fields["icao_code"], "B738")

    def test_version_and_notes_do_not_change_the_hash(self):
        self.assertEqual(
            content_hash(_fixture(version="1.0.0", notes="first")),
            content_hash(_fixture(version="9.9.9", notes="rewritten")),
        )

    def test_edited_rule_changes_the_hash(self):
        self.assertNotEqual(
            content_hash(_fixture(rule_value=1)),
            content_hash(_fixture(rule_value=2)),
        )

    def test_added_and_removed_rows_change_the_hash(self):
        base = _fixture()
        extra = base + [{"model": "checklist.checkitem", "pk": 11, "fields": {"item": "X"}}]
        self.assertNotEqual(content_hash(base), content_hash(extra))
        self.assertNotEqual(content_hash(extra), content_hash(extra[:-1] + []))

    def test_sop_name_is_content(self):
        renamed = copy.deepcopy(_fixture())
        renamed[0]["fields"]["name"] = "Boeing 737-900"
        self.assertNotEqual(content_hash(_fixture()), content_hash(renamed))

    def test_hash_is_stable_under_object_reordering(self):
        reordered = list(reversed(copy.deepcopy(_fixture())))
        self.assertEqual(content_hash(_fixture()), content_hash(reordered))

    def test_hash_is_stable_under_m2m_reordering(self):
        self.assertEqual(
            content_hash(_fixture(attributes=[3, 4])),
            content_hash(_fixture(attributes=[4, 3])),
        )


class TestContentDiff(unittest.TestCase):

    def test_classifies_added_changed_and_removed(self):
        before = _fixture(rule_value=1)
        after = copy.deepcopy(before)
        after[1]["fields"]["auto_check_rule"]["value"] = 2
        after.append({"model": "checklist.checkitem", "pk": 11, "fields": {"item": "New"}})
        after.append({"model": "checklist.procedure", "pk": 5, "fields": {"slug": "p"}})
        removed_from = before + [
            {"model": "checklist.procedure", "pk": 9, "fields": {"slug": "gone"}}
        ]

        diff = content_diff(before, after)
        self.assertEqual(diff["changed"], [("checklist.checkitem", 10)])
        self.assertEqual(
            diff["added"], [("checklist.checkitem", 11), ("checklist.procedure", 5)]
        )
        self.assertEqual(diff["removed"], [])
        self.assertEqual(
            content_diff(removed_from, before)["removed"], [("checklist.procedure", 9)]
        )

    def test_release_notes_only_change_is_not_a_diff(self):
        diff = content_diff(_fixture(notes="first"), _fixture(notes="second"))
        self.assertEqual(diff, {"added": [], "changed": [], "removed": []})

    def test_a_differing_hash_always_has_a_diff_to_explain_it(self):
        renamed = copy.deepcopy(_fixture())
        renamed[0]["fields"]["name"] = "Boeing 737-900"
        self.assertNotEqual(content_hash(_fixture()), content_hash(renamed))
        self.assertEqual(content_diff(_fixture(), renamed)["changed"], [("checklist.sop", 1)])


class TestSetterCommit(unittest.TestCase):
    """The git walk, against throwaway repositories."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fixture = self.root / check.FIXTURE_PATH
        self.fixture.parent.mkdir(parents=True)
        self._run("git", "init", "-q")
        self._run("git", "config", "user.email", "test@example.com")
        self._run("git", "config", "user.name", "Test")
        self.addCleanup(self._tmp.cleanup)

    def _run(self, *args):
        subprocess.run(args, cwd=self.root, check=True, capture_output=True)

    def _commit(self, data, message):
        self.fixture.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._run("git", "add", check.FIXTURE_PATH)
        self._run("git", "commit", "-q", "-m", message)
        return self._run_out("git", "rev-parse", "HEAD")

    def _run_out(self, *args):
        return subprocess.run(
            args, cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_finds_the_commit_that_introduced_the_version(self):
        self._commit(_fixture(version="1.0.0"), "content: first")
        bump = self._commit(_fixture(version="1.1.0"), "chore(sop): bump to 1.1.0")
        self._commit(_fixture(version="1.1.0", rule_value=2), "content: edit a rule")

        head = check.fixture_at("HEAD", root=self.root)
        self.assertEqual(
            check.setter_commit(check.versions(head), root=self.root), bump
        )

    def test_never_bumped_walks_back_to_the_first_commit(self):
        first = self._commit(_fixture(version="1.0.0"), "content: first")
        self._commit(_fixture(version="1.0.0", rule_value=2), "content: edit a rule")

        head = check.fixture_at("HEAD", root=self.root)
        self.assertEqual(
            check.setter_commit(check.versions(head), root=self.root), first
        )

    def test_content_edited_after_the_bump_is_detected(self):
        self._commit(_fixture(version="1.1.0"), "chore(sop): bump to 1.1.0")
        self._commit(_fixture(version="1.1.0", rule_value=2), "content: edit a rule")

        head = check.fixture_at("HEAD", root=self.root)
        setter = check.setter_commit(check.versions(head), root=self.root)
        self.assertNotEqual(
            content_hash(check.fixture_at(setter, root=self.root)), content_hash(head)
        )

    def test_a_fresh_bump_clears_the_condition(self):
        self._commit(_fixture(version="1.1.0"), "chore(sop): bump to 1.1.0")
        self._commit(_fixture(version="1.1.0", rule_value=2), "content: edit a rule")
        self._commit(_fixture(version="1.2.0", rule_value=2), "chore(sop): bump to 1.2.0")

        head = check.fixture_at("HEAD", root=self.root)
        setter = check.setter_commit(check.versions(head), root=self.root)
        self.assertEqual(
            content_hash(check.fixture_at(setter, root=self.root)), content_hash(head)
        )

    def test_notes_only_bump_still_leaves_content_matching(self):
        self._commit(_fixture(version="1.1.0", notes="first"), "chore(sop): bump")
        self._commit(_fixture(version="1.1.0", notes="reworded"), "docs: reword notes")

        head = check.fixture_at("HEAD", root=self.root)
        setter = check.setter_commit(check.versions(head), root=self.root)
        self.assertEqual(
            content_hash(check.fixture_at(setter, root=self.root)), content_hash(head)
        )

    def test_no_fixture_history_is_undetermined(self):
        with self.assertRaises(check.Undetermined):
            check.setter_commit({"B738": "1.0.0"}, root=self.root)


if __name__ == "__main__":
    unittest.main()
