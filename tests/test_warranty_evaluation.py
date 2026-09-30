from __future__ import annotations

import unittest

from warranty_claims.evaluation import evaluate


class EvaluationTests(unittest.TestCase):
    def test_active_within_months_window(self) -> None:
        result = evaluate(
            "fitted", "months:12",
            "2026-06-01T02:00:00Z", "2026-09-28T03:00:00Z",
        )
        self.assertEqual(result.state, "active")
        self.assertEqual(result.effective_start, "2026-06-01")
        self.assertEqual(result.effective_end, "2027-05-31")

    def test_expired_after_window(self) -> None:
        result = evaluate(
            "fitted", "months:3",
            "2026-06-01T00:00:00Z", "2026-12-02T00:00:00Z",
        )
        self.assertEqual(result.state, "expired")
        self.assertIn("晚于到期日", result.note)

    def test_failure_before_start(self) -> None:
        result = evaluate(
            "date:2027-01-01", "months:12",
            "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z",
        )
        self.assertEqual(result.state, "not_started")

    def test_indeterminate_throughput_needs_ledger(self) -> None:
        result = evaluate(
            "fitted", "throughput_kwh:50000",
            "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z",
        )
        self.assertEqual(result.state, "indeterminate")
        self.assertIn("台账", result.note)

    def test_fixed_date_end(self) -> None:
        result = evaluate(
            "fitted", "date:2030-01-01",
            "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z",
        )
        self.assertEqual(result.state, "active")
        self.assertEqual(result.effective_end, "2030-01-01")


if __name__ == "__main__":
    unittest.main()
