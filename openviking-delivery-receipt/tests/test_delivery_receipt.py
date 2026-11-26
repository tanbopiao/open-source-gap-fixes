"""Conformance scenarios for the delivery settlement contract (proposal E).

These mirror the acceptance criteria in the RFC: run identical delivery /
retry / stale-ACK-injection scenarios and assert (a) late ACKs are rejected
no-ops; (b) serial ordering is preserved under late-ACK injection; (c) Work
settlement is never mutated by stale deliveries.
"""

import unittest

from delivery_receipt import ReceiptQueueBackend


class TestDeliverySettlement(unittest.TestCase):
    def setUp(self):
        self.q = ReceiptQueueBackend()

    def test_dequeue_returns_fresh_delivery_id_per_attempt(self):
        self.q.enqueue("m1")
        _, d1 = self.q.dequeue()
        self.q.retry("m1")  # redelivery path
        _, d2 = self.q.dequeue()
        self.assertNotEqual(d1, d2)  # delivery_id changes on every redelivery

    def test_late_ack_from_stale_delivery_is_noop(self):
        # The #8199 scenario: message redelivered, then the FIRST delivery's
        # ACK arrives late.
        self.q.enqueue("m1")
        _, d1 = self.q.dequeue()
        self.q.retry("m1", d1)
        _, d2 = self.q.dequeue()
        r = self.q.ack("m1", d1)  # stale receipt
        self.assertFalse(r.settled)
        self.assertTrue(self.q.is_processing("m1"))  # current delivery untouched

    def test_current_ack_settles(self):
        self.q.enqueue("m1")
        _, d = self.q.dequeue()
        r = self.q.ack("m1", d)
        self.assertTrue(r.settled)
        self.assertFalse(self.q.is_processing("m1"))

    def test_backward_compat_ack_by_message_id(self):
        # When delivery_id is absent, settlement falls back to message_id
        # (current upstream behavior).
        self.q.enqueue("m1")
        self.q.dequeue()
        r = self.q.ack("m1")
        self.assertTrue(r.settled)

    def test_serial_slot_not_released_by_stale_ack(self):
        # The #8181 scenario: a stale ACK must not release the serial slot
        # early and let a later message jump the queue.
        self.q.enqueue("m1", serial_key="k")
        _, d1 = self.q.dequeue()
        self.q.retry("m1", d1)
        _, d2 = self.q.dequeue()
        self.q.ack("m1", d1)  # stale ACK
        self.assertEqual(self.q.serial_slot_holder("k"), "m1")  # slot still held
        self.q.ack("m1", d2)  # current ACK
        self.assertIsNone(self.q.serial_slot_holder("k"))  # released only now

    def test_serial_ordering_preserved_under_late_ack_injection(self):
        self.q.enqueue("m1", serial_key="k")
        self.q.enqueue("m2", serial_key="k")
        _, d1 = self.q.dequeue()  # m1, first attempt
        self.q.retry("m1", d1)  # m1 requeued behind m2
        _, d2 = self.q.dequeue()  # m2 delivered next
        _, d1b = self.q.dequeue()  # m1 redelivered with a fresh receipt
        self.q.ack("m1", d1)  # late stale ACK for m1
        # m2 must still be processing; the stale ACK must not have disturbed it
        self.assertTrue(self.q.is_processing("m2"))
        self.q.ack("m1", d1b)
        self.q.ack("m2", d2)
        self.assertEqual(len(self.q.acked), 2)

    def test_work_settlement_not_mutated_by_stale_delivery(self):
        # Work items settle by delivery_id; a stale delivery's settlement
        # attempt must leave the current Work state untouched.
        self.q.enqueue("work-1", serial_key="w")
        _, d1 = self.q.dequeue()
        self.q.retry("work-1", d1)
        _, d2 = self.q.dequeue()
        r1 = self.q.retry("work-1", d1)  # stale retry
        self.assertFalse(r1.settled)
        r2 = self.q.retry("work-1", d2)  # current retry
        self.assertTrue(r2.settled)


if __name__ == "__main__":
    unittest.main()
