"""Demand, clearing and pricing. Two engines see the same orbits and the same job stream: the market and a baseline.

Clearing is pluggable (CLEARING): a policy decides the order pending jobs are served in and what a resource costs on
a given satellite. Prices are per resource and region; the market moves them by tatonnement on excess demand each
tick, the baseline keeps them fixed and serves first come, first served. Both bill at admission and both must fit
capacity, so the only differences are who gets scarce capacity and where flexible work goes.
"""
import math
import random

import numpy as np

from . import physics as ph
from .orbit import C_KM_S, REGIONS, RE
from .routing import ROUTERS, Network

RES = ["compute", "memory", "storage", "isl", "downlink"]
# Anchor prices. compute: an L-class satellite (~$1.5M built and launched, 64 units, 5 years) amortises to ~$0.5 per
# unit-hour; memory and storage at a space premium over terrestrial clouds; downlink near AWS Ground Station minutes.
REF = {"compute": 0.60,     # $/unit-h
       "memory": 0.006,     # $/GB-h
       "storage": 0.30,     # $/TB-h
       "isl": 0.002,        # $/GB per hop
       "downlink": 0.50}    # $/GB
OWNER_MARKUP = {"A": 1.0, "B": 0.9, "C": 1.1, "D": 1.0}
FOOTPRINT_KM = 940.0        # ground radius seen above 25 deg elevation from ~550 km
TARGET_UTIL = 0.85
ETA = 0.12                  # tatonnement step per tick

HUBS = [(40.7, -74.0, 5), (34.0, -118.2, 3), (41.9, -87.6, 2), (37.4, -122.1, 3), (43.7, -79.4, 1.5),
        (19.4, -99.1, 1.5), (-23.5, -46.6, 2), (-34.6, -58.4, 1), (51.5, -0.1, 4), (48.9, 2.35, 3), (50.1, 8.7, 3),
        (59.3, 18.1, 1.5), (6.5, 3.4, 1.5), (-26.2, 28.0, 1), (30.0, 31.2, 1.5), (25.2, 55.3, 2), (19.1, 72.9, 3),
        (28.6, 77.2, 3), (13.0, 77.6, 2), (1.35, 103.8, 3), (-6.2, 106.8, 2), (35.7, 139.7, 4), (37.6, 127.0, 2.5),
        (31.2, 121.5, 3), (39.9, 116.4, 3), (-33.9, 151.2, 2), (55.8, 37.6, 2)]


def pareto(rng, alpha, xmin, cap):
    return min(cap, xmin / (1 - rng.random()) ** (1 / alpha))


def loguniform(rng, lo, hi):
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


def local_hour(unix_s, lon):
    return (unix_s / 3600 + lon / 15) % 24


def diurnal(unix_s, lon):
    """Internet-style daily cycle: peak ~21:00 local, trough ~05:00, peak/trough ~4x (published IXP traffic curves)."""
    return 1 + 0.6 * math.cos(2 * math.pi * (local_hour(unix_s, lon) - 21) / 24)


def sun_elevation_sin(sun_eci, gmst_rad, lat, lon):
    la, lo = math.radians(lat), math.radians(lon) + gmst_rad
    return math.cos(la) * math.cos(lo) * sun_eci[0] + math.cos(la) * math.sin(lo) * sun_eci[1] + math.sin(la) * sun_eci[2]


class Demand:
    """Poisson arrivals per job kind, heavy-tailed sizes, regional daily cycles. Rates are per simulated second."""

    RATES = {"eo": 1.0, "edge": 3.0, "bulk": 0.12, "batch": 1.2, "relay": 0.3}

    def __init__(self, land_points, seed=1):
        self.rng = random.Random(seed)
        self.land = land_points
        self.next_id = 1
        w = np.array([h[2] for h in HUBS], float)
        self.hub_w = w / w.sum()

    def _hub(self):
        h = HUBS[int(np.searchsorted(np.cumsum(self.hub_w), self.rng.random()))]
        return h[0] + self.rng.gauss(0, 1.5), h[1] + self.rng.gauss(0, 1.5)

    def _target(self):
        if self.rng.random() < 0.7:
            lat, lon = self.land[self.rng.randrange(len(self.land))]
        else:
            lat, lon = math.degrees(math.asin(self.rng.uniform(-0.85, 0.85))), self.rng.uniform(-180, 180)
        return float(lat), float(lon)

    def _poisson(self, lam):
        k, p, L = 0, 1.0, math.exp(-lam)
        while True:
            p *= self.rng.random()
            if p < L:
                return k
            k += 1

    def arrivals(self, now, dt, sun, gmst_rad):
        jobs = []
        for kind, rate in self.RATES.items():
            for _ in range(self._poisson(rate * dt * (1.3 if kind in ("edge", "relay") else 1))):
                job = getattr(self, "_" + kind)(now, sun, gmst_rad)
                if job is None:
                    continue
                job.update(id=self.next_id, kind=kind, arrival=now + self.rng.random() * dt)
                job["value"] = self._value(job)
                self.next_id += 1
                jobs.append(job)
        return jobs

    def _eo(self, now, sun, g):
        lat, lon = self._target()
        if sun_elevation_sin(sun, g, lat, lon) < 0.17:   # optical imagery needs the sun ~10 deg up
            return None
        units = int(pareto(self.rng, 1.5, 4, 64))
        gb = pareto(self.rng, 1.3, 20, 4000)
        return dict(units=units, mem=4 * units, storage_gb=gb, duration=pareto(self.rng, 1.6, 120, 3 * 3600),
                    out_gb=0.02 * gb, location=(lat, lon), deadline=now + loguniform(self.rng, 1800, 3 * 3600))

    def _edge(self, now, sun, g):
        lat, lon = self._hub()
        if self.rng.random() > diurnal(now, lon) / 1.6:
            return None
        units = int(pareto(self.rng, 1.4, 2, 64))
        dur = loguniform(self.rng, 300, 3600)
        return dict(units=units, mem=2 * units, storage_gb=1, duration=dur, out_gb=0, location=(lat, lon),
                    latency={"p50_ms": self.rng.choice([10, 20, 30])}, deadline=now + 60 + dur)

    def _bulk(self, now, sun, g):
        lat, lon = self._target()
        gb = pareto(self.rng, 1.2, 50, 20000)
        return dict(units=0, mem=0, storage_gb=gb, duration=0, out_gb=gb, location=(lat, lon),
                    deadline=now + loguniform(self.rng, 3600, 86400))

    def _batch(self, now, sun, g):
        units = int(pareto(self.rng, 1.3, 16, 64))
        dur = pareto(self.rng, 1.4, 1800, 12 * 3600)
        return dict(units=units, mem=4 * units, storage_gb=pareto(self.rng, 1.5, 10, 2000), duration=dur, out_gb=1,
                    location=None, deadline=now + min(86400, dur * self.rng.uniform(1.5, 4)))

    def _relay(self, now, sun, g):
        a, b = self._hub(), self._hub()
        if self.rng.random() > (diurnal(now, a[1]) + diurnal(now, b[1])) / 3.2:
            return None
        gc_km = RE * math.acos(max(-1, min(1, sum(x * y for x, y in zip(_unit(*a), _unit(*b))))))
        if gc_km < 2000:
            return None
        fiber_ms = gc_km / (C_KM_S / 1.47) * 1000 * 1.3   # fibre: n=1.47, route 30 % longer than great circle
        kind = self.rng.choice(["max_ms", "p50_ms"])
        dur = loguniform(self.rng, 60, 1800)
        gbps = pareto(self.rng, 1.5, 0.5, 10)
        return dict(units=0, mem=0, storage_gb=0, duration=dur, gbps=gbps, out_gb=gbps * dur / 8, location=a, dest=b,
                    latency={kind: round(fiber_ms * (0.9 if kind == "p50_ms" else 1.0))}, deadline=now + dur)

    def _value(self, job):
        ref = ref_cost(job)
        urgency = {"edge": 3.0, "relay": 2.5, "eo": 2.0, "batch": 1.0}.get(job["kind"])
        if urgency is None:
            urgency = 1 + 2 * math.exp(-(job["deadline"] - job["arrival"]) / 7200)
        job["ref_cost"] = ref
        return ref * urgency * math.exp(self.rng.gauss(0, 0.7))


def ref_cost(job):
    """What a job costs at anchor prices: the yardstick for bid density (value per unit of resources)."""
    hours = job["duration"] / 3600
    return (job["units"] * hours * REF["compute"] + job["mem"] * hours * REF["memory"]
            + job["storage_gb"] / 1000 * max(hours, 0.5) * REF["storage"]
            + job["out_gb"] * (REF["downlink"] if job["kind"] != "relay" else 10 * REF["isl"]))


def _unit(lat, lon):
    la, lo = math.radians(lat), math.radians(lon)
    return math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)


class MarketClearing:
    """Prices per resource x region by tatonnement; jobs served by bid density, placed on the cheapest satellite."""

    name = "market"
    router = "congestion"

    def __init__(self):
        self.p = {r: np.full(len(REGIONS), REF[r]) for r in RES}

    def order(self, jobs):
        return sorted(jobs, key=lambda j: -j["value"] / max(j["ref_cost"], 1e-9))

    def sat_prices(self, region, reserve):
        """Per-satellite compute, memory and storage price this tick: the regional price, never below the owner's offer."""
        return np.maximum(self.p["compute"][region], reserve), self.p["memory"][region], self.p["storage"][region]

    def pick(self, cost, cand, geo, job):
        return int(np.argmin(cost))

    def update(self, pressure):
        """Tatonnement on excess demand, with a floor that rises with load: an idle region sells at 30 % of the
        anchor, a full one cannot drop below it, and unmet demand pushes the price up from there without limit
        short of 100x."""
        for r in RES:
            z = pressure[r]
            ok = np.isfinite(z)
            step = np.exp(ETA * np.clip(z[ok] - TARGET_UTIL, -1, 2))
            floor = REF[r] * (0.3 + 0.7 * np.minimum(z[ok], 1))
            self.p[r][ok] = np.clip(self.p[r][ok] * step, floor, 100 * REF[r])


class FixedFCFS(MarketClearing):
    """Baseline: posted reference prices, first come first served, nearest feasible satellite, shortest-path routes."""

    name = "baseline"
    router = "distance"

    def order(self, jobs):
        return sorted(jobs, key=lambda j: j["arrival"])

    def sat_prices(self, region, reserve):
        return self.p["compute"][region], self.p["memory"][region], self.p["storage"][region]

    def pick(self, cost, cand, geo, job):
        if job.get("location") is None:
            return 0
        return int(np.argmax(geo.unit[cand] @ np.array(_unit(*job["location"]))))

    def update(self, pressure):
        pass


CLEARING = {"market": MarketClearing, "baseline": FixedFCFS}


class Engine:
    def __init__(self, con, clearing):
        self.con = con
        self.clear = clearing
        n = con.n_sats
        self.fleet = ph.Fleet(con)
        self.units_used = np.zeros(n)
        self.mem_used = np.zeros(n)
        self.sto_used = np.zeros(n)
        self.cap_units = self.fleet.units.copy()
        self.tasks, self.flows, self.pending = [], [], []
        self.prev_load = (np.zeros(len(con.isl_u)), np.zeros(n), np.zeros(len(con.gs_lat)))
        self.done = []          # (t, value, on_time, kind)
        self.revenue = 0.0
        self.net = None
        self.pressure = {r: np.zeros(len(REGIONS)) for r in RES}
        self.binding = {"power": 0.0, "thermal": 0.0}
        self.reserve = np.array([OWNER_MARKUP[o] for o in con.owner]) * 0.45

    # -- one tick -------------------------------------------------------------------------------------------------
    def tick(self, geo, dt, arrivals):
        now = geo.t
        cap, power_cap, thermal_cap = self.fleet.step(geo.sunlit, self.units_used, dt)
        self.cap_units = cap
        self.binding = {"power": float((power_cap < self.units_used + 1).mean()),
                        "thermal": float((thermal_cap < self.units_used + 1).mean())}
        self._progress_tasks(now, dt)
        self.net = Network(self.con, geo, self.fleet, self.prev_load, ROUTERS[self.clear.router])
        self.pending.extend(arrivals)
        unmet = {r: np.zeros(len(REGIONS)) for r in RES}
        self._clear_pending(geo, now, dt, unmet)
        self._move_bits(geo, now, dt, unmet)
        self.prev_load = self.net.loads()
        self._pressure(geo, unmet)
        self.clear.update(self.pressure)
        cutoff = now - 3600
        self.done = [d for d in self.done if d[0] > cutoff]

    def _progress_tasks(self, now, dt):
        rate = np.minimum(1.0, self.cap_units / np.maximum(self.units_used, 1e-9))
        keep = []
        for t in self.tasks:
            t["left"] -= dt * rate[t["sat"]]
            if t["left"] > 0:
                keep.append(t)
                continue
            s = t["sat"]
            self.units_used[s] -= t["units"]
            self.mem_used[s] -= t["mem"]
            job = t["job"]
            if job["out_gb"] > 0.01:
                self.sto_used[s] -= job["storage_gb"] - job["out_gb"]
                self.flows.append(dict(job=job, sat=s, gb_left=job["out_gb"], hold_gb=job["out_gb"]))
            else:
                self.sto_used[s] -= job["storage_gb"]
                self._finish(job, now)
        self.tasks = keep
        np.maximum(self.units_used, 0, out=self.units_used)
        np.maximum(self.sto_used, 0, out=self.sto_used)

    def _finish(self, job, now, ok=True):
        on_time = ok and now <= job["deadline"] + 1
        job["result"] = {"t": now, "on_time": on_time}
        self.done.append((now, job["value"], on_time, job["kind"]))

    def _clear_pending(self, geo, now, dt, unmet):
        keep = []
        free_u = self.cap_units - self.units_used
        free_m = self.fleet.mem - self.mem_used
        free_s = self.fleet.storage - self.sto_used
        reserve = self.reserve * (1 + ph.UNIT_W * self.fleet.energy_shadow_price(geo.sunlit) / 0.45)
        p_cpu, p_mem, p_sto = self.clear.sat_prices(geo.region, reserve)
        p_dl = float(np.median(self.clear.p["downlink"]))
        everywhere = np.arange(self.con.n_sats)
        for job in self.clear.order(self.pending):
            if now + job["duration"] > job["deadline"]:
                self._finish(job, now, ok=False)
                continue
            if job["kind"] == "relay":
                if not self._admit_relay(geo, job, now, unmet):
                    keep.append(job)
                continue
            loc = job.get("location")
            cand = geo.within_km(*loc, FOOTPRINT_KM) if loc is not None else everywhere
            if len(cand) == 0:
                keep.append(job)
                continue
            fit = cand[(free_u[cand] >= job["units"]) & (free_m[cand] >= job["mem"]) &
                       (free_s[cand] >= job["storage_gb"])]
            if len(fit) == 0:
                reg = geo.region[cand[0]]
                for r, amt in (("compute", job["units"]), ("memory", job["mem"]), ("storage", job["storage_gb"])):
                    unmet[r][reg] += amt
                keep.append(job)
                continue
            h = job["duration"] / 3600
            cost = (job["units"] * h * p_cpu[fit] + job["mem"] * h * p_mem[fit]
                    + job["storage_gb"] / 1000 * max(h, 0.5) * p_sto[fit] + job["out_gb"] * p_dl)
            i = self.clear.pick(cost, fit, geo, job)
            s, price = int(fit[i]), float(cost[i])
            if price > job["value"]:
                keep.append(job)    # waits for a cheaper tick or its deadline
                continue
            self.revenue += price
            job["placed"] = {"sat": s, "price": round(price, 4), "t": now}
            free_u[s] -= job["units"]
            free_m[s] -= job["mem"]
            free_s[s] -= job["storage_gb"]
            self.units_used[s] += job["units"]
            self.mem_used[s] += job["mem"]
            self.sto_used[s] += job["storage_gb"]
            if job["duration"] > 0:
                self.tasks.append(dict(job=job, sat=s, units=job["units"], mem=job["mem"], left=job["duration"]))
            else:
                self.flows.append(dict(job=job, sat=s, gb_left=job["out_gb"], hold_gb=job["storage_gb"]))
        self.pending = keep

    def _admit_relay(self, geo, job, now, unmet):
        a = geo.within_km(*job["location"], FOOTPRINT_KM)
        b = geo.within_km(*job["dest"], FOOTPRINT_KM)
        if len(a) == 0 or len(b) == 0:
            return False
        sa = int(a[np.argmax(geo.unit[a] @ np.array(_unit(*job["location"])))])
        sb = int(b[np.argmax(geo.unit[b] @ np.array(_unit(*job["dest"])))])
        path = self.net.paths_between([(sa, sb)])[0]
        if path is None:
            return False
        q, bound = next(iter(job["latency"].items()))
        lat_ms = 1000 * (self.net.latency_s(path, self.prev_load, 0.5 if q == "p50_ms" else 0.99)
                         + 2 * 560 / C_KM_S * 1.5)   # up and down slant at ~45 deg elevation
        if lat_ms > bound:
            return False
        reg = geo.region[sa]
        price = job["out_gb"] * self.clear.p["isl"][reg] * (len(path) - 1)
        if price > job["value"]:
            return False
        if self.net.residual(path) < job["gbps"]:
            unmet["isl"][reg] += job["gbps"]
            return False
        self.revenue += price
        job["placed"] = {"sat": sa, "price": round(price, 4), "t": now, "latency_ms": round(lat_ms, 1)}
        self.flows.append(dict(job=job, sat=sa, dst=sb, gb_left=job["out_gb"], hold_gb=0, start=now))
        return True

    def _move_bits(self, geo, now, dt, unmet):
        keep = []
        relay_pairs = [(f["sat"], f["dst"]) for f in self.flows if "dst" in f]
        relay_paths = dict(zip(relay_pairs, self.net.paths_between(relay_pairs)))
        for f in sorted(self.flows, key=self._flow_key):
            job = f["job"]
            path = relay_paths.get((f["sat"], f.get("dst"))) if "dst" in f else self.net.path_to_ground(f["sat"])
            left_s = max(job["deadline"] - now, dt)
            need = f["gb_left"] * 8 / left_s if "dst" not in f else job["gbps"]
            want = min(f["gb_left"] * 8 / dt, max(need, ph.DOWNLINK_GBPS))
            got = 0.0
            if path:
                got = max(0.0, min(want, self.net.residual(path)))
                if got > 0:
                    self.net.take(path, got)
                f["path"] = path
            if got < need:
                end = path[-1] - self.net.n if path and "dst" not in f else None
                if end is not None:
                    unmet["downlink"][self.con.gs_region[end]] += need - got
                unmet["isl"][geo.region[f["sat"]]] += need - got
            f["rate"] = got
            f["gb_left"] -= got * dt / 8
            if f["gb_left"] <= 1e-6 or ("dst" in f and now >= job["deadline"]):
                self.sto_used[f["sat"]] -= f["hold_gb"]
                self._finish(job, now, ok=f["gb_left"] <= 0.05 * job["out_gb"])
            elif now > job["deadline"] + 6 * 3600:
                self.sto_used[f["sat"]] -= f["hold_gb"]
                self._finish(job, now, ok=False)
            else:
                keep.append(f)
        self.flows = keep

    def _flow_key(self, f):
        j = f["job"]
        if self.clear.name == "baseline":
            return j["arrival"]
        return -j["value"] / max(j["ref_cost"], 1e-9)

    def _pressure(self, geo, unmet):
        """(used + unmet) / capacity per resource and region: the excess-demand signal prices move on."""
        R = len(REGIONS)
        reg = geo.region
        cnt = lambda w: np.bincount(reg, weights=w, minlength=R)
        cap_u = cnt(self.cap_units)
        out = {"compute": (cnt(np.minimum(self.units_used, self.cap_units)) + unmet["compute"]) / np.maximum(cap_u, 1),
               "memory": (cnt(self.mem_used) + unmet["memory"]) / cnt(self.fleet.mem),
               "storage": (cnt(self.sto_used) + unmet["storage"]) / cnt(self.fleet.storage)}
        isl_load = self.prev_load[0]
        ureg = reg[self.con.isl_u]
        isl_cap = np.bincount(ureg, weights=self.net.isl_cap_all, minlength=R)
        out["isl"] = (np.bincount(ureg, weights=isl_load, minlength=R) + unmet["isl"]) / np.maximum(isl_cap, 1e-6)
        gsr = self.con.gs_region
        gs_cap = np.bincount(gsr, weights=self.net.gs_cap, minlength=R)
        dl = (np.bincount(gsr, weights=self.prev_load[2], minlength=R) + unmet["downlink"]) / np.maximum(gs_cap, 1e-9)
        out["downlink"] = np.where(gs_cap > 0, dl, np.nan)
        self.pressure = out

    # -- reporting ------------------------------------------------------------------------------------------------
    def metrics(self):
        d = self.done
        n = len(d)
        on = sum(1 for x in d if x[2])
        return {"value_per_h": round(sum(x[1] for x in d if x[2])),
                "on_time": round(on / n, 3) if n else None,
                "jobs_per_h": n,
                "compute_util": round(float(np.minimum(self.units_used, self.cap_units).sum() / self.fleet.units.sum()), 3),
                "downlink_util": round(float(self.prev_load[2].sum() / self.net.gs_cap.sum()), 3) if self.net else 0,
                "pending": len(self.pending), "running": len(self.tasks), "flows": len(self.flows)}

    def global_pressure(self):
        """Capacity-weighted pressure per resource across the whole cluster."""
        out = {}
        for r in RES:
            z = self.pressure[r]
            z = z[np.isfinite(z)]
            out[r] = round(float(np.percentile(z, 75)), 3) if len(z) else 0.0
        return out
