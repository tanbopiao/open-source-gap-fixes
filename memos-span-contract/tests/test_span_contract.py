"""Regression tests for the span-level update contract (RFC #2656 / issue #2527).

Run with the stdlib only::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memos.mem_feedback.span_contract import (  # noqa: E402
    SpanContractViolation,
    apply_update_with_contract,
    current_mode,
    normalize_ws,
    verify_untouched_spans,
)

# A long multi-fact memory in the shape of issue #2527's reproduction.
OLD_TEXT = (
    "The user is a primary-school Chinese teacher in Haidian, Beijing. "
    "The user prefers paper books, especially long-form nonfiction, on weekends. "
    "The user plans to visit their sister in Hangzhou in May 2026 and stay one weekend. "
    "The user's sister lives in Xihu District, Hangzhou, and they talk once a month."
)

# Correcting only the job fact (the whole-string replacement the LLM would emit).
JOB_CORRECTED = OLD_TEXT.replace("primary-school Chinese teacher", "middle-school math teacher")

# Deleting only the Hangzhou-visit sentence (silently dropped fact).
VISIT_DELETED = OLD_TEXT.replace(
    "The user plans to visit their sister in Hangzhou in May 2026 and stay one weekend. ",
    "",
)

# A snapshot that predates the job correction; restoring it wipes that fix.
OLD_SNAPSHOT = OLD_TEXT
RESTORED = OLD_SNAPSHOT  # rollback target, while current text has the job fix


class TestSpanContract(unittest.TestCase):
    def locate(self, old: str, needle: str):
        n_old = normalize_ws(old)
        n_needle = normalize_ws(needle)
        start = n_old.index(n_needle)
        return [(start, start + len(n_needle))]

    # --- mode resolution -------------------------------------------------
    def test_default_mode_is_off(self):
        self.assertEqual(current_mode({}), "off")

    def test_mode_from_env(self):
        self.assertEqual(current_mode({"MEMOS_UPDATE_CONTRACT": "warn"}), "warn")
        self.assertEqual(current_mode({"MEMOS_UPDATE_CONTRACT": "ENFORCE"}), "enforce")
        self.assertEqual(current_mode({"MEMOS_UPDATE_CONTRACT": "bogus"}), "off")

    # --- gap 1: undeclared whole-string rewrite --------------------------
    def test_undeclared_rewrite_is_flagged(self):
        spans = self.locate(OLD_TEXT, "primary-school Chinese teacher")
        violations = verify_untouched_spans(OLD_TEXT, JOB_CORRECTED, spans)
        self.assertEqual(violations, [])

    def test_undeclared_extra_change_is_caught(self):
        spans = self.locate(OLD_TEXT, "primary-school Chinese teacher")
        sneaky = JOB_CORRECTED.replace(
            "paper books", "e-books"
        )  # second fact silently altered
        violations = verify_untouched_spans(OLD_TEXT, sneaky, spans)
        self.assertEqual(len(violations), 1)

    # --- gap 2: silent deletion of one fact ------------------------------
    def test_silent_fact_deletion_is_caught(self):
        # No spans declared at all: the deletion must be flagged.
        violations = verify_untouched_spans(OLD_TEXT, VISIT_DELETED, [])
        self.assertGreaterEqual(len(violations), 1)

    def test_declared_deletion_passes(self):
        spans = self.locate(
            OLD_TEXT,
            "The user plans to visit their sister in Hangzhou in May 2026 and stay one weekend.",
        )
        violations = verify_untouched_spans(OLD_TEXT, VISIT_DELETED, spans)
        self.assertEqual(violations, [])

    # --- gap 3: rollback wiping interleaved corrections ------------------
    def test_rollback_wiping_correction_is_caught(self):
        # current text already contains the job fix; restoring the old
        # snapshot silently reverts it -> must be flagged as undeclared.
        violations = verify_untouched_spans(JOB_CORRECTED, RESTORED, [])
        self.assertGreaterEqual(len(violations), 1)

    # --- gap 4: fallback behaviour via apply_update_with_contract --------
    def test_off_mode_passthrough(self):
        self.assertEqual(
            apply_update_with_contract(OLD_TEXT, VISIT_DELETED, [], mode="off"),
            VISIT_DELETED,
        )

    def test_warn_mode_logs_and_allows(self):
        logger = logging.getLogger("span-contract-test")
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        result = apply_update_with_contract(
            OLD_TEXT, VISIT_DELETED, [], mode="warn", logger=logger
        )
        self.assertEqual(result, VISIT_DELETED)
        self.assertEqual(len(records), 1)
        self.assertIn("undeclared change", records[0].getMessage())

    def test_enforce_mode_raises(self):
        with self.assertRaises(SpanContractViolation):
            apply_update_with_contract(OLD_TEXT, VISIT_DELETED, [], mode="enforce")

    def test_clean_update_passes_enforce(self):
        spans = self.locate(OLD_TEXT, "primary-school Chinese teacher")
        result = apply_update_with_contract(
            OLD_TEXT, JOB_CORRECTED, spans, mode="enforce"
        )
        self.assertEqual(result, JOB_CORRECTED)

    # --- whitespace normalization ----------------------------------------
    def test_whitespace_only_edit_is_not_a_change(self):
        violations = verify_untouched_spans(OLD_TEXT, OLD_TEXT + "  ", [])
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
