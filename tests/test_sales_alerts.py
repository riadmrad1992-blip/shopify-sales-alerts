"""Unit tests for the alert logic. Run with:  python -m unittest discover -s tests"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sales_alerts as sa  # noqa: E402

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)


def order(hours_ago, amount, refunded=0.0):
    return {
        "processedAt": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
        "totalPriceSet": {"shopMoney": {"amount": str(amount), "currencyCode": "USD"}},
        "totalRefundedSet": {"shopMoney": {"amount": str(refunded)}},
    }


def baseline_week(daily_amount=100.0):
    """One order per day for the 7 days before the last 24 hours."""
    return [order(24 + 24 * d + 12, daily_amount) for d in range(7)]


class AnalyzeTests(unittest.TestCase):
    def test_normal_day(self):
        r = sa.analyze(baseline_week() + [order(3, 100)], NOW)
        self.assertEqual(r["status"], "Normal")
        self.assertEqual(r["alerts"], [])
        self.assertAlmostEqual(r["daily_avg"], 100.0)

    def test_sales_drop(self):
        r = sa.analyze(baseline_week() + [order(3, 40)], NOW)
        self.assertEqual(r["status"], "SALES DROP")

    def test_zero_sales_is_a_drop(self):
        r = sa.analyze(baseline_week(), NOW)
        self.assertEqual(r["status"], "SALES DROP")
        self.assertEqual(r["recent"]["orders"], 0)

    def test_sales_spike(self):
        r = sa.analyze(baseline_week() + [order(3, 200)], NOW)
        self.assertEqual(r["status"], "SALES SPIKE")

    def test_high_refund_rate(self):
        r = sa.analyze(baseline_week() + [order(3, 100, refunded=25)], NOW)
        self.assertEqual(r["status"], "Normal")
        self.assertTrue(any("Refund rate" in a for a in r["alerts"]))

    def test_no_history_yet(self):
        r = sa.analyze([order(3, 100)], NOW)
        self.assertTrue(r["status"].startswith("No baseline yet"))
        self.assertEqual(r["alerts"], [])

    def test_orders_older_than_8_days_ignored(self):
        r = sa.analyze(baseline_week() + [order(24 * 9, 5000), order(3, 100)], NOW)
        self.assertAlmostEqual(r["daily_avg"], 100.0)

    def test_message_contains_key_numbers(self):
        r = sa.analyze(baseline_week() + [order(3, 40)], NOW)
        msg = sa.build_message("demo.myshopify.com", r)
        self.assertIn("SALES DROP", msg)
        self.assertIn("40.00 USD", msg)
        self.assertIn("100.00 USD", msg)


class SeedPlanTests(unittest.TestCase):
    def test_plan_size_and_dates(self):
        import seed_orders

        plan = seed_orders.build_orders(days=5, per_day=3, currency="USD", now=NOW)
        self.assertEqual(len(plan), 15)
        self.assertTrue(all(o["when"] < NOW - timedelta(hours=3) for o in plan))
        self.assertTrue(all(o["total"] > 0 for o in plan))


if __name__ == "__main__":
    unittest.main()
