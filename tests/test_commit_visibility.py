"""Commit publication must follow successful persistence, including failure/retry paths.

Loads the storage modules directly so these stdlib-only tests also run outside
Hermes. This does not replace provider discovery or host integration coverage.
"""

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_PACKAGE = "cognition_commit_visibility_tests"
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[_PACKAGE] = package
merge = importlib.import_module(_PACKAGE + ".merge")
provenance = importlib.import_module(_PACKAGE + ".provenance")


def fact(ref, content="I prefer small, isolated changes."):
    return {
        "ref": ref,
        "content": content,
        "provenance": provenance.make_provenance(
            source_session="test-session", author="test-author", origin_repo="test-repo",
            trace_id=ref, created_at=1.0,
        ),
    }


class CommitVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.path = str(Path(self.home.name) / "committed.json")
        self.log = merge.CommitLog(self.path)

    def assert_visible(self, entries):
        expected = {entry["ref"]: entry for entry in entries}
        self.assertEqual({e["ref"]: e for e in merge.committed_snapshot(self.log)}, expected)
        self.assertEqual(self.log.refs(), sorted(expected))
        self.assertEqual({e["ref"]: e for e in self.log.entries_for_repo("test-repo")}, expected)
        reopened = merge.CommitLog(self.path)
        self.assertEqual({e["ref"]: e for e in merge.committed_snapshot(reopened)}, expected)
        for ref, entry in expected.items():
            self.assertEqual(self.log.get(ref), entry)

    def test_successful_insert_survives_reopen(self):
        entry = fact("a")
        self.assertEqual(merge.fetch_first_push(self.log, entry, None), ("applied", "a"))
        self.assert_visible([entry])

    def test_successful_matching_base_update_survives_reopen(self):
        old, new = fact("a", "old"), fact("a", "new")
        merge.fetch_first_push(self.log, old, None)
        self.assertEqual(merge.fetch_first_push(self.log, new, old), ("applied", "a"))
        self.assert_visible([new])

    def test_successful_remove_survives_reopen(self):
        entry = fact("a")
        merge.fetch_first_push(self.log, entry, None)
        self.assertEqual(self.log.remove("a"), entry)
        self.assert_visible([])

    def test_missing_remove_does_not_write(self):
        with patch.object(merge.os, "replace") as replace:
            self.assertIsNone(self.log.remove("missing"))
        replace.assert_not_called()
        self.assert_visible([])

    def test_same_second_conflicts_preserve_all_versions(self):
        original = fact("a", "original")
        merge.fetch_first_push(self.log, original, None)
        first = merge.fetch_first_push(self.log, fact("a", "first"), None, now=10.0)
        second = merge.fetch_first_push(self.log, fact("a", "second"), None, now=10.0)
        self.assertEqual(first, ("conflict", "a-conflict-10"))
        self.assertEqual(second, ("conflict", "a-conflict-10-2"))
        self.assertEqual(self.log.get("a"), original)
        self.assertEqual(len(merge.committed_snapshot(merge.CommitLog(self.path))), 3)

    def test_reads_are_detached_copies(self):
        entry = fact("a")
        merge.fetch_first_push(self.log, entry, None)
        self.log.get("a")["provenance"]["author"] = "changed"
        merge.committed_snapshot(self.log)[0]["content"] = "changed"
        self.assert_visible([entry])

    def test_failed_insert_never_becomes_visible(self):
        with patch.object(merge.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                merge.fetch_first_push(self.log, fact("a"), None)
        self.assertIsNone(self.log.get("a"))
        self.assert_visible([])
        self.assertEqual(list(Path(self.home.name).glob(".commitlog-*")), [])

    def test_failed_update_keeps_previous_committed_value(self):
        old = fact("a", "original")
        merge.fetch_first_push(self.log, old, None)
        with patch.object(merge.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                merge.fetch_first_push(self.log, fact("a", "replacement"), old)
        self.assert_visible([old])

    def test_failed_remove_keeps_previous_committed_value(self):
        entry = fact("a")
        merge.fetch_first_push(self.log, entry, None)
        with patch.object(merge.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                self.log.remove("a")
        self.assert_visible([entry])

    def test_failed_conflict_does_not_publish_an_uncommitted_version(self):
        original = fact("a", "original")
        merge.fetch_first_push(self.log, original, None)
        with patch.object(merge.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                merge.fetch_first_push(self.log, fact("a", "conflicting"), None, now=10.0)
        self.assert_visible([original])

    def test_retry_after_failed_insert_is_not_a_conflict(self):
        entry = fact("a")
        with patch.object(merge.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                merge.fetch_first_push(self.log, entry, None)
        self.assertEqual(merge.fetch_first_push(self.log, entry, None), ("applied", "a"))
        self.assert_visible([entry])

    def test_later_write_cannot_commit_a_previously_failed_insert(self):
        with patch.object(merge.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                merge.fetch_first_push(self.log, fact("a"), None)
        entry = fact("b")
        merge.fetch_first_push(self.log, entry, None)
        self.assert_visible([entry])


if __name__ == "__main__":
    unittest.main()
