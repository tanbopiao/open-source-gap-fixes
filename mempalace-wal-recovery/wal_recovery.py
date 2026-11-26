"""WAL recovery contract — reference implementation.

Reference for MemPalace/mempalace RFC (proposal G, cross-refs #2048, #1982,
#1502). Upstream v3.12.0 has a WAL that logs a pre-mutation intent entry
before every backend write, and its docstring promises that "a replay script
can distinguish completed writes (skip them) from pending ones (redo them)
using the intent entry as the key." Three gaps close that promise:

1. No consumer — no replay tooling reads the WAL for recovery.
2. No durability — intent appends are buffered without fsync, so the entry
   recovery depends on can vanish on power loss.
3. No correlation — outcome entries carry no ID, so intent/outcome pairing
   relies on file ordering, which breaks under interleaved O_APPEND writers
   (daemon, CLI, MCP server are separate processes).

This module implements the proposed contract:

- ``Wal.log_intent(op, params)`` generates a ``wal_id`` (uuid), appends the
  intent entry with fsync, and returns the ``wal_id`` to the caller.
- ``Wal.log_result(wal_id, outcome)`` carries the same ``wal_id`` — pairing
  by ID, not by ordering. Entries without a ``wal_id`` fall back to
  ordering-based pairing (backward compatible).
- ``Wal.replay(executor, dry_run=True, seen_keys=None)`` reads intents,
  skips those with a matching outcome, and re-executes pending ones.
  Replay is dry-run by default, idempotent (``drawer_id`` + content hash as
  the idempotency key), and lock-aware (callers hold the advisory lock).

Self-contained: standard library only; the journal is a JSONL file with
explicit ``os.fsync`` on intent writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Callable, Optional

INTENT = "intent"
OUTCOME = "outcome"


def idempotency_key(op: str, params: Any) -> str:
    """Stable key so a partially-completed write is not duplicated.

    Red-team fix (2026-11-26 exercise): the key now includes the op and the
    full params. The legacy drawer_id+content key collided across ops —
    e.g. DELETE and CLEAR on the same drawer with no content produced the
    same key, so one real mutation was silently skipped as a duplicate.
    NOTE: signature change from (drawer_id, content); callers must pass the
    op through.
    """
    blob = json.dumps({"op": op, "params": params}, sort_keys=True, ensure_ascii=False)
    return f"{op}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"


class Wal:
    """JSONL write-ahead log with fsync'd intents and ID-based pairing."""

    def __init__(self, path: str, fsync_intents: bool = True):
        self.path = path
        self.fsync_intents = fsync_intents
        self._ordering_fallback_cursor = 0  # for entries without wal_id
        self.corrupt_lines = 0  # red-team telemetry: skipped malformed lines

    # -- low-level append -------------------------------------------------------

    def _append(self, entry: dict, durable: bool) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
            if durable:
                os.fsync(fd)  # the intent must survive power loss
        finally:
            os.close(fd)

    # -- write path ---------------------------------------------------------------

    def log_intent(self, op: str, params: dict) -> str:
        """Write the intent entry durably and return its wal_id."""
        wal_id = uuid.uuid4().hex
        self._append(
            {"kind": INTENT, "wal_id": wal_id, "op": op, "params": params},
            durable=self.fsync_intents,
        )
        return wal_id

    def log_result(self, wal_id: Optional[str], outcome: dict) -> None:
        """Write the outcome entry; carries the intent's wal_id when known."""
        entry: dict[str, Any] = {"kind": OUTCOME, "outcome": outcome}
        if wal_id is not None:
            entry["wal_id"] = wal_id
        self._append(entry, durable=False)

    # -- read path ----------------------------------------------------------------

    def entries(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # Red-team fix: a torn/corrupt tail line (partial write)
                    # must not make the whole log unreadable. Skip it; the
                    # intent it may have held stays pending by definition.
                    self.corrupt_lines += 1
        return out

    def pending_intents(self) -> list[dict]:
        """Intents without a matching outcome, paired by wal_id.

        Backward compatible: legacy entries without wal_id pair by ordering
        (an intent is settled by the next outcome entry in file order).
        """
        entries = self.entries()
        settled_ids = {e["wal_id"] for e in entries if e["kind"] == OUTCOME and "wal_id" in e}
        pending = []
        awaiting_legacy: list[dict] = []
        for e in entries:
            if e["kind"] == INTENT:
                if "wal_id" in e and e["wal_id"] in settled_ids:
                    continue  # paired by ID
                if "wal_id" not in e:
                    awaiting_legacy.append(e)  # pairing decided by next outcome
                else:
                    pending.append(e)
            elif e["kind"] == OUTCOME and "wal_id" not in e:
                if awaiting_legacy:
                    awaiting_legacy.pop(0)  # legacy ordering-based pairing
        pending.extend(awaiting_legacy)
        return pending

    # -- recovery -------------------------------------------------------------------

    def replay(
        self,
        executor: Callable[[str, dict], Any],
        dry_run: bool = True,
        seen_keys: Optional[set] = None,
    ) -> dict:
        """Re-execute pending intents.

        - dry_run (default): print/collect what would be redone, change nothing.
        - idempotent: intents whose idempotency key is in ``seen_keys`` are
          skipped, so a partially-completed write is not duplicated.
        - lock-aware: the caller is expected to hold the advisory lock
          (upstream ``palace_lock``) for the whole replay.
        """
        seen = seen_keys if seen_keys is not None else set()
        would_redo, redone, skipped = [], [], []
        for entry in self.pending_intents():
            key = idempotency_key(entry["op"], entry["params"])
            if key in seen:
                skipped.append(entry["wal_id"])
                continue
            would_redo.append(entry["wal_id"])
            if not dry_run:
                executor(entry["op"], entry["params"])
                redone.append(entry["wal_id"])
                # Red-team fix: self-settlement. Without an outcome the
                # intent stays pending forever and every recovery re-executes
                # it (double-apply). Replay now settles what it redid.
                self.log_result(entry["wal_id"], {"status": "replayed"})
            seen.add(key)
        return {"would_redo": would_redo, "redone": redone, "skipped_idempotent": skipped}
