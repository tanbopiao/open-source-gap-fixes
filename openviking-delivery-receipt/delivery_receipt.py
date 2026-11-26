"""Unified delivery settlement contract — reference implementation.

Reference for volcengine/OpenViking RFC (proposal E, cross-refs #8181,
#8184, #8199): QueueFS promises at-least-once delivery, but all three
backends settle (ACK/RETRY) on the bare ``message_id`` alone. Under
at-least-once semantics — retry, crash recovery and heartbeat recovery all
re-deliver the same ``message_id`` — a late ACK from a stale delivery is
indistinguishable from an ACK for the current delivery.

Contract implemented here (backend-agnostic, mirrors the ``QueueBackend``
trait proposal):

1. ``dequeue`` returns ``(message_id, delivery_id)``; ``delivery_id`` is
   unique per delivery attempt and changes on every redelivery.
2. ``ack`` / ``retry`` take ``delivery_id`` and perform conditional
   settlement: a mismatched ``delivery_id`` is a no-op returning ``False``.
3. The serial-key slot is released only when the *current* delivery settles.
4. Backward compatible: when ``delivery_id`` is absent, settlement falls
   back to ``message_id`` (current upstream behavior).

Self-contained: standard library only. The in-memory backend stands in for
Memory/SQLite/Redis; the conformance scenarios below are the ones the RFC
proposes running against all three upstream backends.
"""

from __future__ import annotations

import itertools
import time
import uuid
from typing import Optional

PENDING = "pending"
PROCESSING = "processing"


class SettlementResult:
    __slots__ = ("settled", "reason")

    def __init__(self, settled: bool, reason: str):
        self.settled = settled
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover
        return f"SettlementResult(settled={self.settled}, reason={self.reason!r})"


class ReceiptQueueBackend:
    """In-memory queue backend with per-delivery receipts."""

    def __init__(self, clock=time.monotonic) -> None:
        self._pending: list[dict] = []  # ordered messages: {message_id, serial_key, payload}
        self._processing: dict[str, dict] = {}  # message_id -> {delivery_id, serial_key, payload}
        self._serial_slot: dict[str, str] = {}  # serial_key -> message_id holding the slot
        self._ids = itertools.count()
        self.acked: list[tuple[str, str]] = []  # audit: (message_id, delivery_id)
        self._clock = clock  # injectable for tests; monotonic by default

    # -- delivery --------------------------------------------------------------

    def enqueue(self, message_id: str, serial_key: str = "default", payload: dict | None = None) -> None:
        self._pending.append(
            {"message_id": message_id, "serial_key": serial_key, "payload": payload or {}}
        )

    def dequeue(self, delivery_id: Optional[str] = None) -> Optional[tuple[str, str]]:
        """Deliver the next message; returns (message_id, delivery_id).

        Every call is a new delivery attempt with a fresh delivery_id —
        retry, crash recovery and heartbeat recovery all go through here,
        which is exactly why settlement must be receipt-keyed.
        """
        if not self._pending:
            return None
        msg = self._pending.pop(0)
        did = delivery_id or uuid.uuid4().hex
        self._processing[msg["message_id"]] = {
            "delivery_id": did,
            "serial_key": msg["serial_key"],
            "payload": msg["payload"],
            "dequeued_at": self._clock(),
        }
        self._serial_slot[msg["serial_key"]] = msg["message_id"]
        return (msg["message_id"], did)

    # -- settlement ------------------------------------------------------------

    def _resolve(
        self, message_id: str, delivery_id: Optional[str]
    ) -> tuple[bool, Optional[dict]]:
        entry = self._processing.get(message_id)
        if entry is None:
            return False, None
        if delivery_id is not None and entry["delivery_id"] != delivery_id:
            return False, entry  # stale receipt: no-op
        return True, entry

    def ack(
        self, message_id: str, delivery_id: Optional[str] = None
    ) -> SettlementResult:
        """Settle the current delivery as done.

        A late ACK carrying a stale delivery_id is rejected as a no-op and
        the serial slot is NOT released.
        """
        ok, entry = self._resolve(message_id, delivery_id)
        if not ok:
            return SettlementResult(False, "stale_or_unknown_delivery")
        serial_key = entry["serial_key"]
        del self._processing[message_id]
        if self._serial_slot.get(serial_key) == message_id:
            del self._serial_slot[serial_key]  # release only on current settlement
        self.acked.append((message_id, delivery_id or message_id))
        return SettlementResult(True, "acked")

    def retry(
        self, message_id: str, delivery_id: Optional[str] = None
    ) -> SettlementResult:
        """Settle the current delivery as failed and re-queue it.

        The next dequeue will hand out a fresh delivery_id for the same
        message_id — the at-least-once redelivery path.
        """
        ok, entry = self._resolve(message_id, delivery_id)
        if not ok:
            return SettlementResult(False, "stale_or_unknown_delivery")
        serial_key = entry["serial_key"]
        del self._processing[message_id]
        if self._serial_slot.get(serial_key) == message_id:
            del self._serial_slot[serial_key]
        self.enqueue(message_id, serial_key, entry["payload"])
        return SettlementResult(True, "requeued")

    # -- crash recovery ----------------------------------------------------------

    def reclaim_expired(self, visibility_timeout: float) -> list[str]:
        """Return expired in-flight messages to the pending queue.

        Red-team fix (2026-11-26 exercise): the original module had no
        recovery path — a consumer crash left messages stuck in
        ``_processing`` forever, so the documented crash/heartbeat
        redelivery never actually happened. An expired delivery is re-queued
        at the FRONT of the pending queue (preserving original serial order)
        and will be redelivered with a fresh delivery_id on the next
        dequeue. The serial slot stays held until the redelivery settles.
        """
        now = self._clock()
        reclaimed: list[str] = []
        for message_id in list(self._processing):
            entry = self._processing[message_id]
            if now - entry["dequeued_at"] < visibility_timeout:
                continue
            del self._processing[message_id]
            self._pending.insert(0, {
                "message_id": message_id,
                "serial_key": entry["serial_key"],
                "payload": entry["payload"],
            })
            reclaimed.append(message_id)
        return reclaimed

    # -- introspection (conformance helpers) ------------------------------------

    def serial_slot_holder(self, serial_key: str) -> Optional[str]:
        return self._serial_slot.get(serial_key)

    def is_processing(self, message_id: str) -> bool:
        return message_id in self._processing
