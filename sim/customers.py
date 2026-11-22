"""Demand from 100 named customers (data/customers/*.json, researched per domain with sources and a budget basis).

Each customer job type arrives as a Poisson stream at jobs_per_day x the active need multipliers (seasonal months,
disaster events), shaped by the local daily cycle of where its data is, thinned by price elasticity
(rate x (price index)^-elasticity), and stops for the simulated day once the customer has spent its annual budget
pro-rated per day. Files are re-read when they change; bad entries are skipped and listed in `problems`, never fatal.
Very frequent job types are merged k-to-1 (k x resources and value, rate / k) so one Python process can carry
600k jobs a day; total volume, value and peak sizes are unchanged.
"""
import glob
import json
import math
import os
import re

import numpy as np

from .market import EVENT_RADIUS_DEG, diurnal, pareto
from .orbit import REGIONS, region_of

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
KIND = {"image_area": "eo", "process_onboard": "eo", "monitor_track": "eo", "relay_data": "bulk",
        "downlink_bulk": "bulk", "store": "bulk", "train_model": "batch"}
MAX_STREAM_PER_DAY = 3000      # merge above this many jobs a day into bigger jobs
FLAT_DAY = ("train_model", "store", "downlink_bulk")


def _months(text):
    """Set of month indices named in a 'when' text: 'Sep-Nov floods, Jul-Aug fires' -> {6, 7, 8, 9, 10}."""
    t = text.lower()
    out = set()
    for a, b in re.findall(r"\b(" + "|".join(MONTHS) + r")[a-z]*\s*[-–to ]+\s*(" + "|".join(MONTHS) + r")", t):
        i, j = MONTHS.index(a), MONTHS.index(b)
        k = i
        while True:
            out.add(k)
            if k == j:
                break
            k = (k + 1) % 12
    for m in re.findall(r"\b(" + "|".join(MONTHS) + r")[a-z]*\b", t):
        out.add(MONTHS.index(m))
    return out


def _num(d, k, lo, hi):
    v = d.get(k)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"{k}={v!r}")
    return float(v)


class Customers:
    def __init__(self, folder, land_points, rng):
        self.folder = folder
        self.rng = rng
        self.mtimes = {}
        self.orgs = []
        self.problems = []
        pts = np.array(land_points, float)
        reg = region_of(pts[:, 0], pts[:, 1])
        sea = np.stack([np.degrees(np.arcsin(np.linspace(-0.95, 0.95, 4000))),
                        (np.arange(4000) * 137.508) % 360 - 180], 1)
        sea_reg = region_of(sea[:, 0], sea[:, 1])
        both = np.concatenate([pts, sea])
        both_reg = np.concatenate([reg, sea_reg])
        self.points = {i: both[both_reg == i] for i in range(len(REGIONS))}
        self.spend = {}            # org id -> $ today (simulated day)
        self.day = None
        self.stats = {}            # org id -> {"served": n, "missed": n}
        self.reload()

    def reload(self):
        files = sorted(glob.glob(os.path.join(self.folder, "*.json")))
        mt = {f: os.path.getmtime(f) for f in files}
        if mt == self.mtimes:
            return False
        orgs, problems = [], []
        for f in files:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                if not isinstance(data, list):
                    raise ValueError("top level is not a list")
            except (OSError, ValueError) as e:
                problems.append(f"{os.path.basename(f)}: {e}")
                continue
            for o in data:
                try:
                    orgs.append(self._org(o))
                except (ValueError, KeyError, TypeError) as e:
                    problems.append(f"{os.path.basename(f)}:{o.get('id', '?') if isinstance(o, dict) else '?'}: {e}")
        self.orgs, self.problems, self.mtimes = orgs, problems, mt
        return True

    def _org(self, o):
        org = {"id": str(o["id"]), "name": str(o["name"]), "domain": str(o.get("domain", "")),
               "budget": _num(o, "annual_budget_usd", 1, 1e11), "budget_basis": str(o.get("budget_basis", "")),
               "what": str(o.get("what_they_buy", "")), "jobs": [], "needs": []}
        for j in o["jobs"]:
            try:
                pj = j["per_job"]
                regions = [REGIONS.index(r) for r in j["regions"] if r in REGIONS]
                if not regions:
                    raise ValueError("no known region")
                per_day = _num(j, "jobs_per_day", 0, 1e7)
                merge = max(1, math.ceil(per_day / MAX_STREAM_PER_DAY))
                org["jobs"].append({
                    "type": str(j["type"]), "kind": KIND.get(j["type"], "eo"), "regions": regions,
                    "core_h": _num(pj, "cpu_core_h", 0, 1e6) * merge, "mem_gb_h": _num(pj, "mem_gb_h", 0, 1e7) * merge,
                    "storage_gb": _num(pj, "storage_gb", 0, 1e6) * merge,
                    "out_gb": max(_num(pj, "downlink_gb", 0, 1e6), _num(pj, "isl_gb", 0, 1e6)
                                  if j["type"] == "relay_data" else 0) * merge,
                    "deadline_s": _num(j, "deadline_s", 1, 30 * 86400), "rate": per_day / merge / 86400,
                    "value": _num(j, "value_per_job_usd", 0, 1e9) * merge,
                    "elasticity": _num(j, "price_elasticity", 0, 5) if "price_elasticity" in j else 0.5})
            except (ValueError, KeyError, TypeError) as e:
                self.problems.append(f"{org['id']}/{j.get('type', '?') if isinstance(j, dict) else '?'}: {e}")
        org["raw_jobs"] = [j for j in o["jobs"] if isinstance(j, dict)]
        for n in o.get("need_changes") or []:
            if isinstance(n, dict) and isinstance(n.get("multiplier"), (int, float)):
                org["needs"].append({"trigger": str(n.get("trigger", "")), "mult": float(n["multiplier"]),
                                     "months": _months(str(n.get("when", ""))), "what": str(n.get("what", ""))})
        if not org["jobs"]:
            raise ValueError("no valid jobs")
        return org

    def multiplier(self, org, month, event):
        m = 1.0
        for n in org["needs"]:
            if n["trigger"] == "seasonal" and month in n["months"]:
                m *= n["mult"]
            elif n["trigger"] == "event" and event is not None:
                m *= n["mult"]
        return m

    def arrivals(self, now, dt, event, price_index, poisson):
        """Jobs arriving in [now, now + dt) from every customer."""
        day = int(now // 86400)
        if day != self.day:
            self.day, self.spend = day, {}
        month = int(np.datetime64(int(now), "s").astype("datetime64[M]").astype(int) % 12)
        jobs = []
        for org in self.orgs:
            if self.spend.get(org["id"], 0) >= org["budget"] / 365:
                continue
            mult = self.multiplier(org, month, event)
            for jt in org["jobs"]:
                at_event = event is not None and jt["kind"] == "eo" and mult > 1
                lam = jt["rate"] * dt * mult * price_index ** -jt["elasticity"]
                for _ in range(poisson(lam)):
                    if at_event and self.rng.random() < 0.7:
                        lat = event["lat"] + self.rng.gauss(0, EVENT_RADIUS_DEG / 2)
                        lon = event["lon"] + self.rng.gauss(0, EVENT_RADIUS_DEG / 2)
                    else:
                        pts = self.points[self.rng.choice(jt["regions"])]
                        lat, lon = (float(x) for x in pts[self.rng.randrange(len(pts))])
                    if jt["type"] not in FLAT_DAY and self.rng.random() > diurnal(now, lon) / 1.6:
                        continue
                    jobs.append(self._job(org, jt, now, dt, lat, lon))
        return jobs

    def _job(self, org, jt, now, dt, lat, lon):
        dl = jt["deadline_s"]
        size = pareto(self.rng, 2.5, 0.6, 4.0)          # spread around the stated per-job size, mean ~1
        core_h = jt["core_h"] * size
        duration = min(max(60.0, dl * 0.4), 3600.0) if core_h > 0 else 0.0
        units = min(64, max(1, math.ceil(core_h * 3600 / duration))) if duration else 0
        if units == 64:
            duration = core_h * 3600 / 64
        hours = max(duration / 3600, 1 / 60)
        return {"kind": jt["kind"], "units": units, "mem": min(256, jt["mem_gb_h"] * size / hours) if units else 0,
                "storage_gb": min(8000, jt["storage_gb"] * size + jt["out_gb"] * size),
                "duration": duration, "out_gb": jt["out_gb"] * size,
                "location": None if jt["kind"] == "batch" else (lat, lon),
                "deadline": now + max(dl, duration * 1.2), "arrival": now + self.rng.random() * dt,
                "value": jt["value"] * size, "customer": org["id"], "ctype": jt["type"]}

    def record(self, done_rows, paid_rows):
        """Fold one tick of the posted engine's results into per-customer stats and today's spend."""
        for org_id, ok in done_rows:
            s = self.stats.setdefault(org_id, {"served": 0, "missed": 0})
            s["served" if ok else "missed"] += 1
        for org_id, usd in paid_rows:
            self.spend[org_id] = self.spend.get(org_id, 0) + usd

    def table(self):
        return [{"id": o["id"], "name": o["name"], "domain": o["domain"], "budget_usd": o["budget"],
                 "budget_basis": o["budget_basis"], "what": o["what"], "jobs": o["raw_jobs"],
                 "spend_today": round(self.spend.get(o["id"], 0)),
                 **self.stats.get(o["id"], {"served": 0, "missed": 0})} for o in self.orgs]
