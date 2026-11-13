import datetime as dt
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402
import backtest  # noqa: E402


class CoreTests(unittest.TestCase):
    def test_temperature_keeps_sign(self):
        for T in (0.3, 1.0, 4.0):
            self.assertGreater(agent._tp(5, T), 0.5)
            self.assertLess(agent._tp(-5, T), 0.5)

    def test_calibration_needs_min_days(self):
        es = [{"date": str(i), "score10": random.uniform(-8, 8), "outcome": {"change_pct": random.gauss(0, 1)}} for i in range(10)]
        self.assertFalse(agent.calibrate(es)["active"])

    def test_vol_backwardation(self):
        r = agent.analyze_vol({"vix": {"last": 30}, "vix3m": {"last": 25}})
        self.assertEqual(r["term"]["state"], "backwardation")

    def test_breadth_requires_universe(self):
        self.assertIsNone(agent.compute_breadth100({"A": {"2026-01-01": 1.0}}))

    def test_integrations_never_expose_values(self):
        os.environ["FRED_API_KEY"] = "SECRET-VALUE"
        try:
            dump = json.dumps(agent.integrations({}))
            self.assertNotIn("SECRET-VALUE", dump)
            self.assertTrue(next(i for i in agent.integrations({}) if i["id"] == "fred")["set"])
        finally:
            os.environ.pop("FRED_API_KEY")

    def test_fred_manual_events_replaced(self):
        now = dt.datetime(2026, 11, 14, tzinfo=dt.timezone.utc)
        ev = agent.upcoming_events(now, None, 30, fred={"events": [("2026-12-10", "08:30", "CPI", "tinggi", True, "x")]})
        self.assertEqual([e["date"] for e in ev if e["name"] == "CPI"], ["2026-12-10"])

    def test_backtest_pipeline_on_noise(self):
        random.seed(1)
        dates, x = [], dt.date(2021, 1, 4)
        while len(dates) < 900:
            if x.weekday() < 5:
                dates.append(x.isoformat())
            x += dt.timedelta(1)

        def walk(p0, v):
            p, o = p0, {}
            for d in dates:
                p *= 1 + random.gauss(0, v)
                o[d] = p
            return o
        data = {k: walk(100, 0.01) for k in ("nasdaq", "nq_fut", "es_fut", "sox", "yield10", "vix", "brent", "dxy")}
        r = backtest.analyze(backtest.build_rows(data))
        self.assertIsNotNone(r)
        self.assertLess(abs(r["ic"]), 0.15)


if __name__ == "__main__":
    unittest.main()
