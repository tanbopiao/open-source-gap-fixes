"""Regression tests for the journal-first mutation path (mem0 #7982)."""

import threading
import time
import unittest

from journal_first import APPLIED, PENDING, JournalFirstMemory


class FakeVectorStore:
    """Stands in for the vector store; can simulate partial failure."""

    def __init__(self):
        self.applied = []
        self.fail_next = False

    def apply(self, op, payload):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated crash between journal and vector write")
        self.applied.append((op, payload))


class TestJournalFirst(unittest.TestCase):
    def setUp(self):
        self.store = FakeVectorStore()
        self.mem = JournalFirstMemory(self.store.apply)

    def test_clean_mutation_is_pending_then_applied(self):
        wal_id = self.mem.mutate("ADD", {"memory_id": "m1", "data": "hi"})
        row = self.mem.journal[wal_id]
        self.assertEqual(row["state"], APPLIED)
        self.assertEqual(self.store.applied, [("ADD", {"memory_id": "m1", "data": "hi"})])

    def test_journal_row_exists_before_vector_apply(self):
        # Journal-first: the PENDING row must be observable even if the
        # vector write never lands (the #7964 crash window).
        holder = {}

        def exploding(op, payload):
            self.assertEqual(len(holder["mem"].journal), 1)  # intent already written
            raise RuntimeError("crash")

        mem = JournalFirstMemory(exploding)
        holder["mem"] = mem
        with self.assertRaises(RuntimeError):
            mem.mutate("ADD", {"memory_id": "m1"})
        rows = list(mem.journal.values())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], PENDING)

    def test_recover_replays_orphaned_pending(self):
        self.store.fail_next = True
        with self.assertRaises(RuntimeError):
            self.mem.mutate("DELETE", {"memory_id": "m1"})
        self.assertEqual(self.store.applied, [])  # vector write never landed
        done = self.mem.recover()
        self.assertEqual(done, {next(iter(self.mem.journal)): APPLIED})
        self.assertEqual(self.store.applied, [("DELETE", {"memory_id": "m1"})])

    def test_recover_respects_pending_timeout(self):
        self.store.fail_next = True
        with self.assertRaises(RuntimeError):
            self.mem.mutate("ADD", {"memory_id": "m1"})
        done = self.mem.recover(pending_timeout=3600.0)
        self.assertEqual(done, {})  # too young; in-flight write may still land
        self.assertEqual(self.store.applied, [])

    def test_history_is_the_audit_trail(self):
        self.mem.mutate("ADD", {"memory_id": "m1", "data": "hi"})
        self.mem.mutate("DELETE", {"memory_id": "m1"})
        trail = self.mem.history(memory_id="m1")
        self.assertEqual([r["op"] for r in trail], ["ADD", "DELETE"])
        self.assertEqual([r["state"] for r in trail], [APPLIED, APPLIED])

    def test_per_scope_lock_serializes_same_scope(self):
        order = []
        release = threading.Event()

        def slow_apply(op, payload):
            order.append("start")
            if len(order) == 1:
                release.wait(timeout=5)  # first writer holds the scope
            order.append("end")

        mem = JournalFirstMemory(slow_apply)
        t1 = threading.Thread(
            target=lambda: mem.mutate("ADD", {"memory_id": "m1"}, scope_id="user:alice")
        )
        t1.start()
        time.sleep(0.05)
        t2 = threading.Thread(
            target=lambda: mem.mutate("ADD", {"memory_id": "m2"}, scope_id="user:alice")
        )
        t2.start()
        time.sleep(0.05)
        # Same scope: second writer must still be waiting on the lock.
        self.assertEqual(order, ["start"])
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertEqual(order, ["start", "end", "start", "end"])

    def test_different_scopes_run_concurrently(self):
        gate = threading.Event()
        order = []

        def gated_apply(op, payload):
            order.append("entered")
            gate.wait(timeout=5)
            order.append("done")

        mem = JournalFirstMemory(gated_apply)
        t1 = threading.Thread(target=lambda: mem.mutate("ADD", {}, scope_id="user:a"))
        t1.start()
        time.sleep(0.05)
        t2 = threading.Thread(target=lambda: mem.mutate("ADD", {}, scope_id="user:b"))
        t2.start()
        time.sleep(0.05)
        self.assertEqual(order, ["entered", "entered"])  # both inside concurrently
        gate.set()
        t1.join(timeout=5)
        t2.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
