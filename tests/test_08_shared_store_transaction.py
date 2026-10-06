"""Acceptance: shared-store transaction across CommitLog instances.

Disk ``committed.json`` is the sole truth: every push/remove runs
refresh → compare → write as ONE transaction under cross-instance/process
exclusion, with the per-ref lock nested inside. Two live instances on one
path therefore never lose each other's commits, same-instance keep-both is
retained, and the session-end queue only acks durable commits.
"""

import copy
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loader import load  # noqa: E402

merge_mod = load("merge")
provenance_mod = load("provenance")
provider_mod = load("provider")
queue_mod = load("queue")

CommitLog = merge_mod.CommitLog


def prov(repo="default", session="sess-shared", author="Ada", ts=1_700_000_000.0):
    return provenance_mod.make_provenance(
        source_session=session, author=author, origin_repo=repo,
        trace_id="trace-shared", created_at=ts)


def entry(ref, content):
    return {"ref": ref, "content": content, "salience": 0.5,
            "provenance": prov()}


def make_provider(home, **config):
    full = dict(provider_mod._DEFAULTS)
    full.update(config)
    provider = provider_mod.CognitionMemoryProvider(config=full)
    provider.initialize("sess-ack", hermes_home=home, platform="cli",
                        agent_context="primary")
    return provider


class SharedStoreTransactionTest(unittest.TestCase):
    def test_two_instances_sequential_both_applied_reopen_shows_both(self):
        path = os.path.join(tempfile.mkdtemp(prefix="cog-shared-"),
                            "committed.json")
        log_a = CommitLog(path)
        log_b = CommitLog(path)  # built while the store is empty: must not go stale
        self.assertEqual(
            merge_mod.fetch_first_push(log_a, entry("shared-a", "from-A"), None),
            ("applied", "shared-a"))
        # A distinct ref from the stale instance still applies — its refresh
        # inside the transaction sees A's commit instead of overwriting it.
        self.assertEqual(
            merge_mod.fetch_first_push(log_b, entry("shared-b", "from-B"), None),
            ("applied", "shared-b"))
        reopened = CommitLog(path)
        self.assertEqual(
            {item["ref"] for item in merge_mod.committed_snapshot(reopened)},
            {"shared-a", "shared-b"})
        # Live cross-instance visibility without reopening (disk is truth).
        self.assertEqual(log_a.get("shared-b")["content"], "from-B")
        self.assertEqual(log_b.get("shared-a")["content"], "from-A")

    def test_same_instance_same_ref_conflict_still_keep_both(self):
        path = os.path.join(tempfile.mkdtemp(prefix="cog-shared-"),
                            "committed.json")
        log = CommitLog(path)
        original = entry("shared-c", "original")
        merge_mod.fetch_first_push(log, original, None)
        stale_base = copy.deepcopy(original)  # a writer that read too early
        newer = entry("shared-c", "writer-B")
        self.assertEqual(
            merge_mod.fetch_first_push(log, newer, stale_base),
            ("applied", "shared-c"))
        status, landed = merge_mod.fetch_first_push(
            log, entry("shared-c", "writer-A-late"), stale_base, now=200.0)
        self.assertEqual((status, landed), ("conflict", "shared-c-conflict-200"))
        self.assertEqual(log.get("shared-c")["content"], "writer-B")
        late = log.get("shared-c-conflict-200")
        self.assertIsNotNone(late)
        self.assertEqual(late["content"], "writer-A-late")
        self.assertEqual(late["conflict_with"], "shared-c")
        reopened = CommitLog(path)
        self.assertEqual(
            {item["ref"] for item in merge_mod.committed_snapshot(reopened)},
            {"shared-c", "shared-c-conflict-200"})

    def test_queue_ack_only_for_durable_commits(self):
        payload = {"content": "I prefer concise answers with no fluff",
                   "base": None, "salience": 0.5, "provenance": prov()}
        failing_home = tempfile.mkdtemp(prefix="cog-ack-fail-")
        failing = make_provider(failing_home)
        ref = failing._queue.enqueue(copy.deepcopy(payload),
                                     session_id="sess-ack")
        with patch.object(merge_mod.os, "replace",
                          side_effect=OSError("simulated replace failure")):
            failing._drain_due()  # push fails -> retry, never ack
        self.assertEqual(failing._queue.pending_count(), 1)
        self.assertEqual(failing._log.refs(), [])
        failing.shutdown()

        durable_home = tempfile.mkdtemp(prefix="cog-ack-ok-")
        durable = make_provider(durable_home)
        durable._queue.enqueue(copy.deepcopy(payload), session_id="sess-ack")
        durable._drain_due()  # push commits -> acked
        self.assertEqual(durable._queue.pending_count(), 0)
        self.assertEqual(durable._log.refs(), [ref])
        self.assertEqual(
            durable._log.get(ref)["content"],
            "I prefer concise answers with no fluff")
        durable.shutdown()


if __name__ == "__main__":
    unittest.main()
