"""Journal-first mutation path (outbox pattern) — reference implementation.

Reference for mem0ai/mem0 issue #7982: every memory mutation writes two
stores — the vector store (the actual memory) and the history DB (the audit
trail) — with no transactional coupling. A crash between the two writes
leaves the stores permanently inconsistent (symptom family: #7440, #7964,
#7792, #7715, #6515/#6531, #6243).

This module implements the proposed contract on top of pluggable stores:

1. Journal first: every mutation writes a PENDING intent row to the journal
   (the existing history DB) *before* touching the vector store.
2. Apply, then mark APPLIED: the vector-store mutation happens second;
   success flips the journal row's state.
3. Recovery sweep: PENDING rows older than a threshold are re-applied or
   reconciled against the vector store, eliminating orphaned mutations.
4. Per-scope write locks: serialize writes per scope id (user_id / agent_id /
   run_id) to close the TOCTOU family at the same boundary.

Self-contained: standard library only. Stores are plain callables/dicts, so
the same logic maps onto mem0's ``vector_store`` + ``db.add_history`` pair.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

PENDING = "PENDING"
APPLIED = "APPLIED"


class ScopeLocks:
    """Per-scope write locks: serialize writes within the same scope id.

    Different scopes proceed concurrently; the same scope is serialized,
    which closes the hash-dedup / entity-store TOCTOU family (#6515, #6531,
    #6243) at one boundary.
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, scope_id: str) -> threading.Lock:
        with self._guard:
            if scope_id not in self._locks:
                self._locks[scope_id] = threading.Lock()
            return self._locks[scope_id]


class JournalFirstMemory:
    """Mutation path with a write-ahead journal in the history store.

    Parameters
    ----------
    apply_mutation:
        Callable(op, payload) that performs the vector-store side effect.
        Raising inside it simulates the crash window between the two writes.
    clock:
        Monotonic-ish callable returning a timestamp; injectable for tests.
    """

    def __init__(
        self,
        apply_mutation: Callable[[str, dict], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.apply_mutation = apply_mutation
        self.clock = clock
        self.journal: dict[str, dict] = {}  # wal_id -> row (history DB + state column)
        self.locks = ScopeLocks()

    # -- journal (history DB) -------------------------------------------------

    def _add_history(self, wal_id: str, op: str, payload: dict, state: str) -> None:
        self.journal[wal_id] = {
            "wal_id": wal_id,
            "op": op,
            "payload": payload,
            "state": state,
            "ts": self.clock(),
        }

    def _set_state(self, wal_id: str, state: str) -> None:
        self.journal[wal_id]["state"] = state

    # -- mutation path ---------------------------------------------------------

    def mutate(self, op: str, payload: dict, scope_id: str = "default") -> str:
        """Journal-first mutation: PENDING -> apply -> APPLIED.

        Returns the wal_id. If the vector-store side raises, the PENDING row
        stays in the journal and :meth:`recover` can replay it.
        """
        with self.locks.lock_for(scope_id):
            wal_id = uuid.uuid4().hex
            self._add_history(wal_id, op, payload, PENDING)
            self.apply_mutation(op, payload)
            self._set_state(wal_id, APPLIED)
            return wal_id

    # -- recovery --------------------------------------------------------------

    def recover(self, pending_timeout: float = 0.0) -> dict[str, str]:
        """Sweep PENDING rows older than ``pending_timeout``.

        Each stale PENDING row is re-applied to the vector store and marked
        APPLIED (idempotent re-apply is the caller's contract, mirroring the
        proposal's reconcile step). Returns {wal_id: final_state}.
        """
        now = self.clock()
        results: dict[str, str] = {}
        for row in list(self.journal.values()):
            if row["state"] != PENDING:
                continue
            if now - row["ts"] < pending_timeout:
                continue  # too young; an in-flight write may still land
            self.apply_mutation(row["op"], row["payload"])
            self._set_state(row["wal_id"], APPLIED)
            results[row["wal_id"]] = APPLIED
        return results

    # -- audit -----------------------------------------------------------------

    def history(self, memory_id: str | None = None) -> list[dict]:
        rows = list(self.journal.values())
        if memory_id is not None:
            rows = [r for r in rows if r["payload"].get("memory_id") == memory_id]
        return sorted(rows, key=lambda r: r["ts"])
