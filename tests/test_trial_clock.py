"""Three deterministic checks; no network and no provider credentials."""

from decimal import Decimal
import importlib.util
from pathlib import Path
import shutil
import unittest


PRODUCT = Path(__file__).resolve().parents[1]
WORKSPACE = PRODUCT.parents[1] if PRODUCT.parent.name == "products" else PRODUCT.parent
spec = importlib.util.spec_from_file_location("trial_clock", PRODUCT / "trial_clock.py")
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)


class TrialClockChecks(unittest.TestCase):
    def setUp(self):
        self.inputs = clock.read_json(PRODUCT / "example_input.json")
        self.snapshot = clock.load_fixture(PRODUCT / "fixtures" / "reviewed-policy.json")

    def test_exact_cost_seven_day_expiry_and_two_hour_ending(self):
        plan = clock.calculate(self.inputs, self.snapshot["policy"])
        self.assertEqual(Decimal(plan["resource_cost_usd"]), Decimal("0.19401"))
        self.assertEqual(plan["credit_expires_at"], "2026-12-01T00:31:00Z")
        self.assertEqual(plan["decision"], "FITS_ENVELOPE")
        self.assertEqual(plan["finish_work_by"], "2026-11-25T02:16:00Z")
        self.assertEqual(plan["export_by"], "2026-11-25T02:26:00Z")
        self.assertEqual(plan["destroy_by"], "2026-11-25T02:31:00Z")
        self.inputs["credit_expires_at"] = "2026-11-25T01:31:00Z"
        self.assertEqual(clock.calculate(self.inputs, self.snapshot["policy"])["decision"], "DO_NOT_START")

    def test_card_transition_bills_the_whole_month_not_only_excess(self):
        policy = self.snapshot["policy"]
        self.assertEqual(clock.card_comparison("7.00", policy)["standard_rate_usage_billed_usd"], "7")
        self.assertTrue(clock.card_comparison("5.00", policy)["automatic_payg"])
        self.assertFalse(clock.card_comparison("4.00", policy)["automatic_payg"])
        self.assertEqual(clock.card_comparison("4.00", policy)["standard_rate_usage_billed_usd"], "0")
        self.assertFalse(clock.card_comparison("7.00", policy)["applies_to_this_run"])

    def test_persist_reopen_and_labelled_replay_change(self):
        directory = WORKSPACE / "temp" / "hacksgiving-r79" / "trial-clock-test"
        try:
            original = clock.make_run(self.inputs, self.snapshot)
            clock.save_run(directory, original)
            reopened = clock.read_json(directory / "state.json")
            self.assertEqual(reopened["snapshot"]["gateway_requests_this_run"], 0)
            unchanged = clock.make_run(self.inputs, self.snapshot, reopened)
            self.assertEqual(unchanged["changes"]["policy_fields"], [])
            changed = clock.load_fixture(PRODUCT / "fixtures" / "simulated-price-change.json")
            refreshed = clock.make_run(self.inputs, changed, reopened)
            clock.save_run(directory, refreshed, reopened)
            self.assertEqual(refreshed["plan"]["decision"], "DO_NOT_START")
            self.assertEqual(refreshed["changes"]["policy_fields"][0]["field"], "rates.cold_gb_hour")
            self.assertEqual(clock.read_json(directory / "previous.json")["plan"]["decision"], "FITS_ENVELOPE")
            page = (directory / "report.html").read_text()
            self.assertIn("SIMULATED", page)
            self.assertIn("0.19401", page)
            self.assertIn("1.85133", page)
            self.assertIn("Native gateway requests this run: 0", page)
        finally:
            if directory.exists():
                shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
