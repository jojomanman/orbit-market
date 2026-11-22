"""Physics and market sanity: run a short simulation and fail on numbers that cannot be true.

Run: python3 -m unittest tests/test_sanity.py   (about a minute)"""
import json
import os
import random
import sys
import tempfile
import time
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import parse_job                                       # noqa: E402
from sim.customers import Customers                                # noqa: E402
from sim.market import MODES, Demand, Engine, ref_cost             # noqa: E402
from sim.orbit import RE, Constellation, Geometry                  # noqa: E402
from sim.settlement import Ledger                                  # noqa: E402


class Sanity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        now = time.time()
        cls.con = Constellation(now)
        land = json.load(open(os.path.join(ROOT, "static", "land.json")))
        demand = Demand(land)
        cust = Customers(os.path.join(ROOT, "data", "customers"), land, demand.rng)
        cls.engines = {k: Engine(cls.con, k) for k in ("rfq", "fixed")}
        for _ in range(40):
            cls.geo = Geometry(cls.con, now)
            arr = cust.arrivals(now, 60, None, 1.0, demand._poisson)
            for j in arr:
                j["id"], j["ref_cost"] = demand.next_id, ref_cost(j)
                demand.next_id += 1
            for e in cls.engines.values():
                e.tick(cls.geo, 60, [dict(j) for j in arr])
            now += 60

    def test_fleet(self):
        self.assertEqual(self.con.n_sats, 10000)
        alt = np.linalg.norm(self.geo.r, axis=1) - RE
        self.assertTrue(((alt > 500) & (alt < 600)).all())
        self.assertTrue(0.55 < self.geo.sunlit.mean() < 0.95)

    def test_isl_lengths_physical(self):
        up = self.geo.isl_up
        self.assertLess(self.geo.isl_km[up].max(), 6000)

    def test_utilization_never_above_one(self):
        for e in self.engines.values():
            for r, v in e.resource_state().items():
                self.assertLessEqual(v["util"], 1.0 + 1e-9, r)
                self.assertLessEqual(v["hot"], 1.0 + 1e-9, r)
            isl, dl, gs = e.prev_load
            self.assertTrue((isl <= e.net.isl_cap_all + 1e-6).all())
            self.assertTrue((gs <= e.net.gs_cap + 1e-6).all())
            self.assertTrue((e.units_used <= e.fleet.units + 1e-6).all())

    def test_prices_depend_on_place(self):
        rp = self.engines["rfq"].region_prices(self.geo)
        cpu = [x for x in rp["compute"] if x is not None]
        self.assertGreater(max(cpu) / min(cpu), 1.05, "compute price flat across regions")
        fixed = [x for x in self.engines["fixed"].region_prices(self.geo)["compute"] if x is not None]
        self.assertAlmostEqual(max(fixed), min(fixed))

    def test_settlement_conserves_money(self):
        for e in self.engines.values():
            v = e.ledger.view()
            self.assertGreater(v["fee_usd_24h"], 0)
            self.assertGreaterEqual(v["escrow_usd"], -1e-6)
            self.assertTrue(all(p["transit_usd_24h"] >= 0 for p in v["pairs"]))
        led = Ledger(np.array(["A", "B"]))
        led.traffic = {("A", "B"): 100.0, ("B", "A"): 80.0}
        self.assertEqual(led.relation("A", "B"), "peer")
        led.traffic[("B", "A")] = 10.0
        self.assertEqual(led.relation("A", "B"), "transit")
        self.assertEqual(led.relation("A", "A"), "own")
        job = {}
        led.open(job, 10.0, 1, 0.25)
        self.assertEqual(job["settle"]["entry"], "B")
        led.release(job, "compute")
        led.release(job, "delivery", ok=False)
        led.release(job, "delivery")          # a settled leg never pays twice
        self.assertAlmostEqual(led.revenue["B"] + led.refunded + led.fee, 10.0)
        self.assertAlmostEqual(led.held, 0.0)
        led.release({}, "compute")            # jobs never priced (no escrow) are ignored


    def test_booking_holds_capacity(self):
        e, now = self.engines["rfq"], self.geo.t
        req = {"cpu": 16, "memory_gb": 8, "duration_s": 600, "deadline_s": 3600, "max_price_usd": 1e6,
               "location": {"lat": 59.3, "lon": 18.1}, "start_in_s": 1800}
        job = parse_job(req, now)
        self.assertEqual(job["deadline"], now + 1800 + 3600)
        e._book(job)
        s = job["booked"]["sat"]
        self.assertEqual(e._held(now + 1800, now + 2400)[0][s], 16)
        self.assertEqual(e._held(now, now + 600)[0][s], 0)      # free for spot jobs until the slot
        e.bookings.remove(job)
        cheap = parse_job({**req, "max_price_usd": 1e-6}, now)
        e._book(cheap)
        self.assertNotIn("booked", cheap)                       # below the forward price: stays a spot job
        for bad in (-5, "soon", 8 * 86400):
            with self.assertRaises(ValueError):
                parse_job({**req, "start_in_s": bad}, now)

    def test_modes_registered(self):
        self.assertEqual(set(MODES), {"rfq", "posted", "batch", "tatonnement", "fixed"})

    def test_parse_job_rejects_bad_input(self):
        for bad in ([], {"cpu": "x", "deadline_s": 60}, {"cpu": 8, "deadline_s": 60},
                    {"cpu": 8, "duration_s": 100, "deadline_s": 60, "max_price_usd": 1},
                    {"network": {"gb": 1}, "deadline_s": 600, "max_price_usd": 1},
                    {"rate": {"tokens_per_s": 100, "p95_ms": 2}, "duration_s": 60, "location": {"lat": 0, "lon": 0},
                     "max_price_usd": 1}):
            with self.assertRaises(ValueError):
                parse_job(bad, 0)
        ok = parse_job({"cpu": 8, "duration_s": 60, "deadline_s": 600, "max_price_usd": 1}, 0)
        self.assertEqual(ok["units"], 8)


class CustomerFiles(unittest.TestCase):
    def test_bad_files_are_skipped_not_fatal(self):
        land = [[0.0, 0.0], [50.0, 10.0]]
        good = {"id": "ok", "name": "Ok", "annual_budget_usd": 1e6, "jobs": [{"type": "image_area",
                "regions": ["Europe"], "per_job": {"cpu_core_h": 1, "mem_gb_h": 4, "storage_gb": 1, "isl_gb": 1,
                "downlink_gb": 0.1}, "deadline_s": 3600, "jobs_per_day": 10, "value_per_job_usd": 50}]}
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.json"), "w").write("{not json")
            open(os.path.join(d, "b.json"), "w").write(json.dumps({"not": "a list"}))
            open(os.path.join(d, "c.json"), "w").write(json.dumps([good, {"id": "x"}, {**good, "id": "neg",
                                                                   "annual_budget_usd": -5}]))
            c = Customers(d, land, random.Random(1))
            self.assertEqual([o["id"] for o in c.orgs], ["ok"])
            self.assertEqual(len(c.problems), 4)
            self.assertFalse(c.reload())    # unchanged files: no re-read

if __name__ == "__main__":
    unittest.main()
