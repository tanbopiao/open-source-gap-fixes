"""Regression tests for the WAL recovery contract (proposal G).

Acceptance criteria from the RFC, mapped to tests:
- Crash injection: kill between intent and mutation -> replay recovers it.
- Durability: with fsync enabled the intent entry is on disk immediately.
- Interleaving: concurrent appenders -> pairing by wal_id is unambiguous
  where ordering-based pairing fails.
"""

import json
import os
import tempfile
import threading
import unittest

from wal_recovery import INTENT, OUTCOME, Wal, idempotency_key


class TestWalRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "wal.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_intent_returns_wal_id_and_entry_is_durable(self):
        wal = Wal(self.path)
        wal_id = wal.log_intent("add_memory", {"drawer_id": "d1", "content": "hi"})
        # With fsync enabled, the entry must be readable from a fresh handle
        # immediately (simulating a crash right after the intent).
        fresh = Wal(self.path)
        entries = fresh.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["wal_id"], wal_id)
        self.assertEqual(entries[0]["kind"], INTENT)

    def test_outcome_carries_same_wal_id(self):
        wal = Wal(self.path)
        wal_id = wal.log_intent("add_memory", {"drawer_id": "d1", "content": "hi"})
        wal.log_result(wal_id, {"status": "ok"})
        pending = wal.pending_intents()
        self.assertEqual(pending, [])  # paired by ID, settled

    def test_crash_injection_replay_recovers_pending(self):
        wal = Wal(self.path)
        wal.log_intent("add_memory", {"drawer_id": "d1", "content": "hi"})
        # process dies here — no outcome ever written
        executed = []
        report = wal.replay(lambda op, params: executed.append((op, params)), dry_run=False)
        self.assertEqual(report["redone"], [report["would_redo"][0]])
        self.assertEqual(executed, [("add_memory", {"drawer_id": "d1", "content": "hi"})])

    def test_replay_is_dry_run_by_default(self):
        wal = Wal(self.path)
        wal.log_intent("add_memory", {"drawer_id": "d1", "content": "hi"})
        executed = []
        report = wal.replay(lambda op, params: executed.append((op, params)))
        self.assertEqual(len(report["would_redo"]), 1)
        self.assertEqual(report["redone"], [])  # nothing executed
        self.assertEqual(executed, [])

    def test_replay_idempotent_on_seen_keys(self):
        wal = Wal(self.path)
        params = {"drawer_id": "d1", "content": "hi"}
        wal.log_intent("add_memory", params)
        key = idempotency_key("add_memory", params)
        report = wal.replay(lambda op, p: None, dry_run=False, seen_keys={key})
        self.assertEqual(report["redone"], [])
        self.assertEqual(len(report["skipped_idempotent"]), 1)

    def test_interleaved_writers_pair_by_wal_id(self):
        # Three "processes" appending concurrently: intent A, intent B,
        # outcome A, outcome B. Ordering-based pairing would wrongly settle
        # A with the first outcome; wal_id pairing stays unambiguous.
        wal = Wal(self.path)
        id_a = wal.log_intent("op_a", {"drawer_id": "a", "content": "x"})
        id_b = wal.log_intent("op_b", {"drawer_id": "b", "content": "y"})
        wal.log_result(id_b, {"status": "ok"})  # B finishes first
        wal.log_result(id_a, {"status": "ok"})
        self.assertEqual(wal.pending_intents(), [])

    def test_legacy_entries_fall_back_to_ordering_pairing(self):
        wal = Wal(self.path)
        # Simulate legacy entries without wal_id.
        wal._append({"kind": INTENT, "op": "add", "params": {"drawer_id": "d"}}, durable=False)
        wal._append({"kind": OUTCOME, "outcome": {"status": "ok"}}, durable=False)
        self.assertEqual(wal.pending_intents(), [])

    def test_interleaving_breaks_ordering_but_not_id_pairing(self):
        # Demonstrate the failure mode the RFC describes: with ordering-based
        # pairing, intent A would be settled by outcome B. Under wal_id
        # pairing, A stays pending until its own outcome arrives.
        wal = Wal(self.path)
        id_a = wal.log_intent("op_a", {"drawer_id": "a", "content": "x"})
        id_b = wal.log_intent("op_b", {"drawer_id": "b", "content": "y"})
        wal.log_result(id_b, {"status": "ok"})  # only B has an outcome
        pending = wal.pending_intents()
        self.assertEqual([e["wal_id"] for e in pending], [id_a])

    def test_concurrent_appenders_produce_valid_jsonl(self):
        wal = Wal(self.path)
        ids = []
        lock = threading.Lock()

        def writer(n):
            for i in range(20):
                wid = wal.log_intent(f"op_{n}_{i}", {"drawer_id": str(n), "content": i})
                with lock:
                    ids.append(wid)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = Wal(self.path).entries()
        self.assertEqual(len(entries), 60)
        self.assertEqual(len({e["wal_id"] for e in entries}), 60)  # all unique


if __name__ == "__main__":
    unittest.main()
