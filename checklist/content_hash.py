"""
Canonical hash of the checklist content a fixture carries.

The fixture is the source of truth for checklist content, and `SOP.content_version`
is the label that content ships under. Nothing ties the two together on its own, so
they drift — content lands under a version that no longer describes it, and the app
reports a version that means nothing. This module is the part that can tell: it
hashes the content and only the content.

An SOP's `content_version`, `release_notes` and `updated_at` describe the release
rather than the content, so they are stripped before hashing. Re-wording notes or
re-stamping `updated_at` is therefore not a content change, while editing a rule,
adding an item or deleting a procedure is.

Pure standard library, and deliberately no Django import: `scripts/check_content_bump.py`
imports this without a configured settings module, and
`docs/SOP_VERSIONING_SPEC.md` step 1 can store `content_hash()` on the SOP row
from inside Django without a second definition growing up beside it.
"""

import hashlib
import json

SOP_MODEL = "checklist.sop"

# SOP fields that label the release instead of describing the content.
RELEASE_FIELDS = ("content_version", "release_notes", "updated_at")


def _normalise_fields(model: str, fields: dict) -> dict:
    """Fields reduced to what counts as content, in a comparable form."""
    out = {}
    for key, value in fields.items():
        if model == SOP_MODEL and key in RELEASE_FIELDS:
            continue
        # M2M links serialise as a list of pks whose order carries no meaning.
        if isinstance(value, list) and all(isinstance(item, int) for item in value):
            value = sorted(value)
        out[key] = value
    return out


def content_payload(data: list) -> list:
    """
    The fixture reduced to its content, ordered so two fixtures describing the
    same content produce the same payload however their objects were written out.
    """
    return [
        {
            "model": obj["model"],
            "pk": obj["pk"],
            "fields": _normalise_fields(obj["model"], obj["fields"]),
        }
        for obj in sorted(data, key=lambda obj: (obj["model"], obj["pk"]))
    ]


def content_hash(data: list) -> str:
    """sha256 over the content payload. Equal hashes mean equal content."""
    serialised = json.dumps(
        content_payload(data), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def _index(data: list) -> dict:
    return {
        (obj["model"], obj["pk"]): json.dumps(
            _normalise_fields(obj["model"], obj["fields"]), sort_keys=True
        )
        for obj in data
    }


def content_diff(old: list, new: list) -> dict:
    """
    What moved between two fixtures: {"added", "changed", "removed"}, each a sorted
    list of (model, pk).

    Covers the same rows the hash does — SOP records included, minus their release
    fields — so a differing hash always has a diff that explains it.
    """
    before, after = _index(old), _index(new)
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(
            key for key in before.keys() & after.keys() if before[key] != after[key]
        ),
    }
