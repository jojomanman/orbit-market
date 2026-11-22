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
from sim.market import MODES, Demand, Engine, ref_cost, tasking_usd  # noqa: E402
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
        led.open(job, 10.0, 1, {"uplink": 0.1, "compute": 0.25, "delivery": 0.65})
        self.assertEqual(job["settle"]["entry"], "B")
        job.update(storage_gb=3.0, out_gb=1.0)
        led.release(job, "uplink")
        self.assertAlmostEqual(led.transit[("B", "GS")], 0.98)     # the station gets the whole uplink share
        self.assertEqual(job["settle"]["forwarders"]["GS"]["gb"], 2.0)
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

    def test_anchor_prices_land_in_reference_ranges(self):
        """docs/MODEL.md "Anchor prices": the typical jobs Jonas named cost what the real market charges."""
        image = lambda window: ref_cost(dict(kind="image", units=16, mem=64, storage_gb=20, duration=30, out_gb=0.5,
                                             location=(59.3, 18.1), sensor_usd=tasking_usd(25, window)))
        self.assertTrue(3000 <= image(900) <= 10000, image(900))          # urgent 25 km2 SAR: several thousand
        self.assertTrue(300 <= image(86400) <= image(900) / 3, image(86400))
        weather_h = ref_cost(dict(kind="eo", units=40, mem=64, storage_gb=30, duration=3600, out_gb=3, location=(60, 10)))
        self.assertTrue(100 <= weather_h <= 5000, weather_h)               # onboard-processing hour: hundreds+
        relay = ref_cost(dict(kind="bulk", units=1, mem=2, storage_gb=5, duration=3600, out_gb=5, location=(50, 0)))
        self.assertTrue(10 <= relay <= 100, relay)                         # small relay: tens
        self.assertEqual(tasking_usd(5, 900), tasking_usd(25, 900))        # 25 km2 minimum order

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


class FeaturedPlan(unittest.TestCase):
    def test_planned_route_is_connected_and_priced_at_the_paid_price(self):
        from server import Sim
        sim = Sim(0.2, "customers")
        sim.step()
        v = sim.next_featured()
        self.assertIsNotNone(v.get("plan"), v)
        legs, ca = v["plan"]["legs"], v["plan"]["compute_at"]
        self.assertEqual(legs[ca]["node"], v["sat"])
        self.assertEqual(legs[-1]["kind"], "gs")
        self.assertTrue(all(l["kind"] == "sat" for l in legs[1:-1]))
        self.assertEqual(legs[0]["kind"], "gs")         # picker skips jobs without a customer location
        self.assertEqual([i["what"] for i in legs[1]["items"]], ["uplink", "contact"])
        if "settlement" in v:
            st = v["settlement"]["legs"]
            self.assertEqual(st["uplink"]["state"], "paid")
            self.assertAlmostEqual(sum(i["usd"] for i in legs[1]["items"]), st["uplink"]["usd"], delta=0.001)
            self.assertTrue(all(abs(i["usd"] - i["qty"] * i["rate"]) < 0.002 for i in legs[1]["items"]))   # amount x rate
            chain = sum(i["usd"] for l in legs for i in l["items"]) + sum(i["usd"] for i in v["plan"]["compute"])
            self.assertAlmostEqual(chain + v["settlement"]["fee_usd"], v["price_usd"], delta=0.01 + 0.002 * v["price_usd"])

    def test_planned_routes_are_physical_and_near_shortest(self):
        """Every hop within ISL range and clear of the Earth (+80 km); on the dense shells each half of the route is
        within 1.5x of the straight-line light time between its ends plus 10 ms, the +Grid staircase on a short route
        (3 cross-plane hops of ~1,400 km then in-plane: 2.1x on 12 ms). The 8-plane polar shell has no seam links."""
        from server import DEFAULT_MODE, Sim
        from sim.orbit import C_KM_S, ISL_MAX_KM
        sim = Sim(0.2, "customers")
        sim.step()
        e, con, n = sim.engines[DEFAULT_MODE], sim.con, sim.con.n_sats
        jobs = [t["job"] for t in list(e.tasks) if t["job"].get("location")][:8]
        self.assertTrue(jobs)
        for j in jobs:
            L, at = (lambda p: (p["legs"], p["compute_at"]))(sim.plan_route(e, j))
            for seg in (L[:at + 1], L[at:]):
                t = seg[-1]["t"]
                geo = Geometry(con, t)
                pos = lambda x: geo.ecef[x] if x < n else con.gs_ecef[x - n]
                for a, b in zip(seg, seg[1:]):
                    if a["node"] < n and b["node"] < n:
                        p, q = pos(a["node"]), pos(b["node"])
                        d = q - p
                        k = np.clip(-(p @ d) / (d @ d), 0, 1)
                        self.assertGreater(np.linalg.norm(p + k * d), RE + 80)
                        self.assertLessEqual(np.linalg.norm(d), ISL_MAX_KM)
                if len(seg) > 1 and con.shell[j["placed"]["sat"]] != 3:
                    lb = np.linalg.norm(pos(seg[0]["node"]) - pos(seg[-1]["node"])) / C_KM_S * 1000
                    self.assertLess(sum(l["ms"] for l in seg[1:]), 1.5 * lb + 10, (j["id"], seg[0]["name"], seg[-1]["name"]))

    def test_no_jobs_means_no_featured(self):
        from server import Sim
        sim = Sim(0, "customers")
        for e in sim.engines.values():
            e.tasks.clear()
        self.assertEqual(sim.next_featured(), {"job": None})


if __name__ == "__main__":
    unittest.main()
