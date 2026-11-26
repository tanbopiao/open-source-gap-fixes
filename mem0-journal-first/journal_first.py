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
FAILED = "FAILED"  # parked after max_retries failed recovery attempts


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
        check_applied: Callable[[str, dict], bool] | None = None,
    ) -> None:
        self.apply_mutation = apply_mutation
        self.check_applied = check_applied
        self.clock = clock
        self.journal: dict[str, dict] = {}  # wal_id -> row (history DB + state column)
        self.locks = ScopeLocks()

    # -- journal (history DB) -------------------------------------------------

    def _add_history(self, wal_id: str, op: str, payload: dict, state: str,
                     scope_id: str = "default") -> None:
        self.journal[wal_id] = {
            "wal_id": wal_id,
            "op": op,
            "payload": payload,
            "state": state,
            "ts": self.clock(),
            "scope_id": scope_id,
            "attempts": 0,
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
            self._add_history(wal_id, op, payload, PENDING, scope_id=scope_id)
            self.apply_mutation(op, payload)
            self._set_state(wal_id, APPLIED)
            return wal_id

    # -- recovery --------------------------------------------------------------

    def recover(self, pending_timeout: float = 0.0,
                max_retries: int = 3) -> dict[str, str]:
        """Sweep PENDING rows older than ``pending_timeout``.

        Red-team hardening (2026-11-26 exercise):
        - Per-row exception isolation: one poison row can no longer abort the
          whole sweep; failing rows are retried up to ``max_retries`` and then
          parked in state ``FAILED`` so they stop blocking later rows.
        - Reconcile hook: when ``check_applied`` is provided, a row whose
          mutation already landed is marked APPLIED without re-applying,
          closing the double-apply window (crash after vector write, before
          the APPLIED flip).
        - Scope lock: each re-apply runs under the row's scope lock, so
          recovery cannot race an in-flight mutate on the same scope.
        """
        now = self.clock()
        results: dict[str, str] = {}
        for row in list(self.journal.values()):
            if row["state"] != PENDING:
                continue
            if now >= row["ts"] and now - row["ts"] < pending_timeout:
                continue  # too young; an in-flight write may still land
            # Clock rollback (now < ts): treat the row as mature. A wall-clock
            # jump backwards must not freeze recovery forever — the row was
            # already old enough to need recovery when it was written.
            # (Red-team fix, 2026-11-26 exercise: RT-J5)
            wal_id = row["wal_id"]
            with self.locks.lock_for(row.get("scope_id", "default")):
                # Re-check under the lock: the mutate may have landed while
                # we waited for the scope lock.
                if self.journal.get(wal_id, {}).get("state") != PENDING:
                    continue
                if self.check_applied is not None:
                    try:
                        if self.check_applied(row["op"], row["payload"]):
                            self._set_state(wal_id, APPLIED)
                            results[wal_id] = APPLIED
                            continue
                    except Exception:
                        pass  # reconcile failure falls through to re-apply
                try:
                    self.apply_mutation(row["op"], row["payload"])
                    self._set_state(wal_id, APPLIED)
                    results[wal_id] = APPLIED
                except Exception:
                    row["attempts"] = row.get("attempts", 0) + 1
                    if row["attempts"] >= max_retries:
                        self._set_state(wal_id, FAILED)
                        results[wal_id] = FAILED
                    # else: stays PENDING, retried on a later sweep
        return results

    # -- audit -----------------------------------------------------------------

    def history(self, memory_id: str | None = None) -> list[dict]:
        rows = list(self.journal.values())
        if memory_id is not None:
            rows = [r for r in rows if r["payload"].get("memory_id") == memory_id]
        return sorted(rows, key=lambda r: r["ts"])
