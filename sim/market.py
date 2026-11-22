"""Demand, pricing and clearing for the satellite compute market.

Goods are (resource, satellite or link): compute, memory and storage on each satellite, each inter-satellite link,
each satellite's downlink terminal and each ground station. A job's price is the sum of the goods it uses on its
cheapest feasible option (satellite, route, ground station), so position, sun, eclipse and load all show up in it.
Several engines see the same orbits and the same job stream and differ only in their pricing mode (MODES), so the
page compares mechanisms on equal terms. Mechanisms and sources: docs/MECHANISMS.md; every constant: docs/MODEL.md.
Utilization is always served/capacity (<= 1); demand beyond capacity is reported separately as unmet.
"""
import math
import random
from collections import deque

import numpy as np

from . import physics as ph
from .orbit import C_KM_S, REGIONS, RE, Geometry
from .routing import ROUTERS, Network
from .settlement import Ledger

RES = ["compute", "memory", "storage", "isl", "downlink"]
# Anchor prices, sourced in docs/MODEL.md "Anchor prices". compute: an L-class satellite (~500 kg with its 4.5 kW array,
# battery and radiators, launched at ~$2,500/kg, plus ~$2.5M hardware and bus) amortised over 5 years at ~50 % sold
# costs ~$2.7 per unit-hour, ~10x a terrestrial GPU-backed vCPU (~$0.25/h); the anchor is 30x terrestrial so the owners'
# floor (L_MULT) sits at that cost. Memory and storage carry the same 30x; downlink and contact minutes follow AWS
# Ground Station; mesh transit sits at cloud inter-region egress.
REF = {"compute": 7.5,      # $/unit-h
       "memory": 0.15,      # $/GB-h
       "storage": 3.0,      # $/TB-h
       "isl": 0.05,         # $/GB per hop
       "downlink": 2.0,     # $/GB, station + terminal
       "contact": 10.0}     # $/station contact minute, AWS Ground Station wideband reserved
UPLINK_CAP = 2.2            # uplink rates stop at 2.2x idle: AWS on-demand $22/min vs the $10 reserved anchor
# Imaging sensor time, $/km2 by delivery window: Umbra SAR via SkyWatch lists ~$39-190/km2, priority at the top;
# archive-speed optical tasking ~$10-25/km2. Orders have a 25 km2 minimum. Log-interpolated between the two windows.
TASKING = ((900.0, 190.0), (86400.0, 25.0))   # (delivery window s, $/km2)
TASKING_MIN_KM2 = 25.0
OWNER_MARKUP = {"A": 1.0, "B": 0.9, "C": 1.1, "D": 1.0}
FOOTPRINT_KM = 940.0        # ground radius seen above 25 deg elevation from ~550 km
IMAGER_KM = 320.0           # imaging swath reach: 30 deg off-nadir from ~560 km
L_MULT = 0.3                # price floor as a multiple of the anchor (owners' cost floor)
RETRY_S = 180               # a waiting job re-bids every 3 simulated minutes
BOOK_PREMIUM = 0.1          # a firm future slot costs today's price on that satellite + 10 % (assumed)
WEATHER_SHELLS = (2, 3)     # S3 (70 deg) and S4 (sun-synchronous) carry weather instruments
WEATHER_RAW_MBPS = 200.0    # raw imager + hyperspectral sounder stream per satellite
WEATHER_AI_RATIO = 25.0     # onboard PC compression + product extraction (IASI PC-score heritage)
WEATHER_AI_UNITS = 4        # compute units the onboard model holds while it runs
WEATHER_VALUE_PER_GB = 3 * REF["downlink"]  # what a met agency pays per GB delivered within 30 min
EVENT_RADIUS_DEG = 2.5

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
    """Poisson arrivals per job kind, heavy-tailed sizes, regional daily cycles. Rates are per simulated second.

    event: a disaster at a point (set by the server) adds urgent imaging, responder compute and damage-data downlink
    there, bid at several times the normal value, for its duration."""

    RATES = {"eo": 1.0, "edge": 3.0, "bulk": 0.12, "batch": 1.2, "relay": 0.3}
    EVENT_RATES = {"eo": 2.0, "edge": 1.5, "bulk": 0.25}

    def __init__(self, land_points, seed=1):
        self.rng = random.Random(seed)
        self.land = land_points
        self.next_id = 1
        self.event = None
        w = np.array([h[2] for h in HUBS], float)
        self.hub_w = w / w.sum()

    def arrivals(self, now, dt, sun, gmst_rad):
        jobs = []
        plan = [(k, r * (1.3 if k in ("edge", "relay") else 1), False) for k, r in self.RATES.items()]
        if self.event and now < self.event["until"]:
            plan += [(k, r, True) for k, r in self.EVENT_RATES.items()]
        for kind, rate, at_event in plan:
            for _ in range(self._poisson(rate * dt)):
                job = getattr(self, "_" + kind)(now, sun, gmst_rad, self._event_point() if at_event else None)
                if job is None:
                    continue
                job.update(id=self.next_id, kind=kind, arrival=now + self.rng.random() * dt)
                job["value"] = self._value(job) * (4.0 if at_event else 1.0)
                if at_event:
                    job["tag"] = "event"
                    job["deadline"] = min(job["deadline"], now + 3600)
                self.next_id += 1
                jobs.append(job)
        return jobs

    def _event_point(self):
        e = self.event
        return (e["lat"] + self.rng.gauss(0, EVENT_RADIUS_DEG / 2),
                e["lon"] + self.rng.gauss(0, EVENT_RADIUS_DEG / 2 / max(0.2, math.cos(math.radians(e["lat"])))))

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

    def _eo(self, now, sun, g, pt=None):
        lat, lon = pt or self._target()
        if pt is None and sun_elevation_sin(sun, g, lat, lon) < 0.17:   # optical imagery needs the sun ~10 deg up
            return None
        units = int(pareto(self.rng, 1.5, 4, 64))
        gb = pareto(self.rng, 1.3, 20, 4000)
        return dict(units=units, mem=4 * units, storage_gb=gb, duration=pareto(self.rng, 1.6, 120, 3 * 3600),
                    out_gb=0.02 * gb, location=(lat, lon), deadline=now + loguniform(self.rng, 1800, 3 * 3600))

    def _edge(self, now, sun, g, pt=None):
        lat, lon = pt or self._hub()
        if pt is None and self.rng.random() > diurnal(now, lon) / 1.6:
            return None
        units = int(pareto(self.rng, 1.4, 2, 64))
        dur = loguniform(self.rng, 300, 3600)
        return dict(units=units, mem=2 * units, storage_gb=1, duration=dur, out_gb=0, location=(lat, lon),
                    latency={"p50_ms": self.rng.choice([10, 20, 30])}, deadline=now + 60 + dur)

    def _bulk(self, now, sun, g, pt=None):
        lat, lon = pt or self._target()
        gb = pareto(self.rng, 1.2, 50, 20000)
        return dict(units=0, mem=0, storage_gb=gb, duration=0, out_gb=gb, location=(lat, lon),
                    deadline=now + loguniform(self.rng, 3600, 86400))

    def _batch(self, now, sun, g, pt=None):
        units = int(pareto(self.rng, 1.3, 16, 64))
        dur = pareto(self.rng, 1.4, 1800, 12 * 3600)
        return dict(units=units, mem=4 * units, storage_gb=pareto(self.rng, 1.5, 10, 2000), duration=dur, out_gb=1,
                    location=None, deadline=now + min(86400, dur * self.rng.uniform(1.5, 4)))

    def _relay(self, now, sun, g, pt=None):
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
            + job["out_gb"] * (REF["downlink"] if job["kind"] != "relay" else 10 * REF["isl"])
            + (uplink_rates(input_gb(job), REF["downlink"] / 2)[2] if job["kind"] != "relay" and job.get("location") else 0)
            + job.get("sensor_usd", 0))


def tasking_usd(km2, window_s):
    """Sensor time for one image order: area (at least the 25 km2 minimum) x the $/km2 for its delivery window."""
    (w0, p0), (w1, p1) = TASKING
    f = min(max(math.log(max(window_s, 1) / w0) / math.log(w1 / w0), 0), 1)
    return max(km2, TASKING_MIN_KM2) * p0 * (p1 / p0) ** f


def input_gb(job):
    """The sim keeps input and output in storage_gb; what goes up is the part that does not come back down."""
    return max(job["storage_gb"] - job["out_gb"], 0)


def uplink_rates(gb, gs_price):
    """Uplink at one station: per GB plus per contact minute (at the downlink link rate), both on the station's own
    load curve (gs_price is the station's per-GB price, REF downlink / 2 when idle), capped where AWS on-demand
    wideband ($22/min) caps ground-station time: past that a buyer books another provider. Returns minutes, $/min, $ total."""
    minutes = gb * 8 / ph.DOWNLINK_GBPS / 60
    gs_price = min(gs_price, UPLINK_CAP * REF["downlink"] / 2)
    per_min = REF["contact"] * gs_price / (REF["downlink"] / 2)
    return minutes, per_min, gb * gs_price + minutes * per_min


def bundle_size(job):
    """Sum of quantity/capacity over the goods a job takes, normalised to one L-class satellite and one terminal-hour."""
    return (job["units"] / 64 + job["mem"] / 256 + job["storage_gb"] / 8000
            + job["out_gb"] / (ph.DOWNLINK_GBPS * 3600 / 8)) or 1e-3


def _unit(lat, lon):
    la, lo = math.radians(lat), math.radians(lon)
    return math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)


# -- pricing modes ------------------------------------------------------------------------------------------------
class Prices:
    """Per-good prices for one tick, in $ per natural unit (see REF)."""

    def __init__(self, cpu, mem, sto, isl, dl, gs):
        self.cpu, self.mem, self.sto, self.isl, self.dl, self.gs = cpu, mem, sto, isl, dl, gs


class Posted:
    """A. Online exponential posted price per good (Awerbuch-Azar-Plotkin; Zhou-Chakrabarty-Lukose 2008).

    p(u) = L for u <= c, (L/e)(U e/L)^u above, c = 1/(1 + ln(U/L)); L = owners' floor, U = the 99th percentile of
    recent bid densities, so the last unit of any good goes only to a job that would pay the top of the market.
    Jobs are price takers in arrival order: take the cheapest feasible option, served iff value >= its price."""

    name, label, router = "posted", "Posted", "price"
    vickrey = False

    def __init__(self, n_sats, n_isl, n_gs):
        self.densities = deque([5.0], maxlen=3000)

    @property
    def U(self):
        return float(np.clip(np.percentile(self.densities, 99), 3.0, 200.0))

    def curve(self, u):
        L, U = L_MULT, self.U
        c = 1 / (1 + math.log(U / L))
        u = np.clip(u, 0, 1)
        return np.where(u <= c, L, (L / math.e) * (U * math.e / L) ** u)

    def observe(self, job):
        self.densities.append(job["value"] / max(job["ref_cost"], 1e-9))

    def prices(self, eng, geo, reserve):
        u = eng.utilization()
        return Prices(np.maximum(REF["compute"] * self.curve(u["cpu"]), reserve),
                      REF["memory"] * self.curve(u["mem"]), REF["storage"] * self.curve(u["sto"]),
                      REF["isl"] * self.curve(u["isl"]),
                      REF["downlink"] / 2 * self.curve(u["dl"]), REF["downlink"] / 2 * self.curve(u["gs"]))

    def reprice(self, eng, pr, s, reserve):
        """After a sale on satellite s its goods are fuller: move its prices up the curve within the tick."""
        pr.cpu[s] = max(REF["compute"] * float(self.curve(eng.units_used[s] / max(eng.cap_units[s], 1e-9))), reserve[s])
        pr.mem[s] = REF["memory"] * float(self.curve(eng.mem_used[s] / eng.fleet.mem[s]))
        pr.sto[s] = REF["storage"] * float(self.curve(eng.sto_used[s] / eng.fleet.storage[s]))

    def order(self, jobs):
        return sorted(jobs, key=lambda j: j["arrival"])

    def accept(self, job, price):
        return job["value"] >= price

    def update(self, eng):
        pass


class ReverseAuction(Posted):
    """Request for quote, matched on arrival (Jonas 2026-11-22: no global tick, competition between satellites).

    Every satellite that can physically serve the job quotes from its own load curve, the posted exponential
    with its owner's floor (L x owner markup, never below its energy-aware reserve). Lowest quote wins and is paid
    the second-lowest quote (Vickrey reverse auction: quoting true cost is each owner's best strategy), capped by
    the bid; a lone quote is paid as quoted. Rejected if no quote is within the bid."""

    name, label = "rfq", "RFQ"
    vickrey = True

    def prices(self, eng, geo, reserve):
        pr = super().prices(eng, geo, reserve)
        markup = eng.owner_markup
        pr.cpu = np.maximum(pr.cpu * markup, reserve)
        pr.mem = pr.mem * markup
        pr.sto = pr.sto * markup
        return pr

    def reprice(self, eng, pr, s, reserve):
        super().reprice(eng, pr, s, reserve)
        m = eng.owner_markup[s]
        pr.cpu[s] = max(pr.cpu[s] * m, reserve[s])
        pr.mem[s] *= m
        pr.sto[s] *= m


class BatchAuction(Posted):
    """C. Per-tick batch auction, greedy by value/sqrt(bundle size) (Lehmann-O'Callaghan-Shoham 2002).

    Each tick all bids are ranked, winners take capacity in that order, and a winner pays its critical value: the
    density of the first job it displaced times sqrt(its own size), never below the owners' floor. Placement uses
    the posted curve only as a signal for which satellite is least loaded."""

    name, label = "batch", "Batch"

    def order(self, jobs):
        return sorted(jobs, key=lambda j: -j["value"] / math.sqrt(bundle_size(j)))

    def accept(self, job, price):
        return job["value"] >= L_MULT * job["ref_cost"]


class Tatonnement(Posted):
    """B. Tatonnement per good (Low-Lapsley 1999 dual update): p <- max(L, p + gamma (D - C)/C) every tick.

    D = served + unmet demand on that good. Prices are state, not a function of load, so they chase moving
    satellites with a lag; that lag is the point of the comparison."""

    name, label = "tatonnement", "Adjust"
    GAMMA = 0.15

    def __init__(self, n_sats, n_isl, n_gs):
        super().__init__(n_sats, n_isl, n_gs)
        self.m = {k: np.ones(n) for k, n in (("cpu", n_sats), ("mem", n_sats), ("sto", n_sats), ("isl", n_isl),
                                              ("dl", n_sats), ("gs", n_gs))}

    def prices(self, eng, geo, reserve):
        m = self.m
        return Prices(np.maximum(REF["compute"] * m["cpu"], reserve), REF["memory"] * m["mem"],
                      REF["storage"] * m["sto"], REF["isl"] * m["isl"], REF["downlink"] / 2 * m["dl"],
                      REF["downlink"] / 2 * m["gs"])

    def reprice(self, eng, pr, s, reserve):
        pass

    def update(self, eng):
        d = eng.demand_ratio()
        for k, z in d.items():
            self.m[k] = np.clip(self.m[k] + self.GAMMA * (z - 1), L_MULT, 100)


class Fixed(Posted):
    """Baseline: anchor prices everywhere, first come first served, nearest satellite, shortest-delay routes."""

    name, label, router = "fixed", "Fixed", "distance"

    def prices(self, eng, geo, reserve):
        n, e, g = eng.con.n_sats, len(eng.con.isl_u), len(eng.con.gs_lat)
        return Prices(np.full(n, REF["compute"]), np.full(n, REF["memory"]), np.full(n, REF["storage"]),
                      np.full(e, REF["isl"]), np.full(n, REF["downlink"] / 2), np.full(g, REF["downlink"] / 2))

    def reprice(self, eng, pr, s, reserve):
        pass


MODES = {c.name: c for c in (ReverseAuction, Posted, BatchAuction, Tatonnement, Fixed)}
DEFAULT_MODE = "rfq"


# -- engine -------------------------------------------------------------------------------------------------------
class Engine:
    def __init__(self, con, mode):
        self.con = con
        n, e, g = con.n_sats, len(con.isl_u), len(con.gs_lat)
        self.mode = MODES[mode](n, e, g)
        self.fleet = ph.Fleet(con)
        self.units_used, self.mem_used, self.sto_used = np.zeros(n), np.zeros(n), np.zeros(n)
        self.cap_units = self.fleet.units.copy()
        self.tasks, self.flows, self.pending = [], [], []
        self.prev_load = (np.zeros(e), np.zeros(n), np.zeros(g))
        self.unmet = self._zero_unmet()
        self.done = deque()       # (t, value, on_time, kind, why it failed)
        self.finished = deque(maxlen=5000)   # recent finished jobs, for the per-customer view
        self.lat_ms = deque()     # (t, ms) of admitted latency-bound jobs
        self.delivery_s = deque() # (t, s from arrival to delivery) of served jobs
        self.paid = deque()       # (t, $)
        self.net = None
        self.pr = None
        self.weather = "off"
        self.wx_backlog = np.zeros(n)
        self.wx_flows = {}
        self.wx_stats = deque()   # (t, raw GB made, GB delivered, $ paid)
        self.wx_sats = np.nonzero(np.isin(con.shell, WEATHER_SHELLS))[0]
        self.owner_markup = np.array([OWNER_MARKUP[o] for o in con.owner])
        self.reserve_base = self.owner_markup * 0.45
        self.cust_done, self.cust_paid = [], []   # this tick, for per-customer stats
        self.ledger = Ledger(con.owner)
        self.bookings = []        # advance reservations not yet started: their capacity is held from spot jobs

    def _zero_unmet(self):
        n, e, g = self.con.n_sats, len(self.con.isl_u), len(self.con.gs_lat)
        return {"cpu": np.zeros(n), "mem": np.zeros(n), "sto": np.zeros(n), "isl": np.zeros(e), "dl": np.zeros(n),
                "gs": np.zeros(g)}

    # -- state the pricing modes read -----------------------------------------------------------------------------
    def capacities(self):
        net = self.net
        return {"cpu": np.maximum(self.cap_units, 1e-9), "mem": self.fleet.mem, "sto": self.fleet.storage,
                "isl": np.maximum(net.isl_cap_all, 1e-9) if net else np.ones(len(self.con.isl_u)),
                "dl": np.full(self.con.n_sats, ph.DOWNLINK_GBPS),
                "gs": np.full(len(self.con.gs_lat), ph.GS_ANTENNAS * ph.DOWNLINK_GBPS)}

    def used(self):
        isl, dl, gs = self.prev_load
        return {"cpu": np.minimum(self.units_used, self.cap_units), "mem": self.mem_used, "sto": self.sto_used,
                "isl": isl, "dl": dl, "gs": gs}

    def utilization(self):
        c, u = self.capacities(), self.used()
        return {k: np.clip(u[k] / c[k], 0, 1) for k in c}

    def demand_ratio(self):
        c, u = self.capacities(), self.used()
        return {k: (u[k] + self.unmet[k]) / c[k] for k in c}

    # -- one tick -------------------------------------------------------------------------------------------------
    def tick(self, geo, dt, arrivals):
        now = geo.t
        wx_units = np.zeros(self.con.n_sats)
        if self.weather == "ai":
            wx_units[self.wx_sats] = WEATHER_AI_UNITS
        cap, _, _ = self.fleet.step(geo.sunlit, self.units_used + wx_units, dt)
        self.cap_units = np.maximum(cap - wx_units, 0)
        self._progress_tasks(now, dt)
        reserve = self.reserve_base * (1 + ph.UNIT_W * self.fleet.energy_shadow_price(geo.sunlit) / 0.45)
        self.pr = self.mode.prices(self, geo, reserve)
        self.net = Network(self.con, geo, self.fleet, self.prev_load, ROUTERS[self.mode.router], self.pr)
        for j in arrivals:
            self.mode.observe(j)
            if j.get("start_after", 0) > now and j.get("sat") is None:
                self._book(j)
        self.bookings = [b for b in self.bookings if "placed" not in b and "result" not in b]
        self.pending.extend(arrivals)
        self.cust_done, self.cust_paid = [], []
        self.ledger.decay(dt)
        self.unmet = self._zero_unmet()
        self._weather(now, dt)
        self._clear_pending(geo, now, dt, reserve)
        self._move_bits(geo, now, dt)
        self.prev_load = self.net.loads()
        self.mode.update(self)
        cutoff = now - 3600
        for q in (self.done, self.lat_ms, self.delivery_s, self.paid, self.wx_stats):
            while q and q[0][0] < cutoff:
                q.popleft()

    def _progress_tasks(self, now, dt):
        rate = np.minimum(1.0, self.cap_units / np.maximum(self.units_used, 1e-9))
        keep = []
        for t in self.tasks:
            t["left"] -= dt * rate[t["sat"]]
            if t["left"] > 0:
                keep.append(t)
                continue
            s, job = t["sat"], t["job"]
            self.ledger.release(job, "compute")
            job["computed_t"] = now
            self.units_used[s] -= job["units"]
            self.mem_used[s] -= job["mem"]
            if job["out_gb"] > 0.01:
                self.sto_used[s] -= job["storage_gb"] - job["out_gb"]
                self.flows.append(dict(job=job, sat=s, gb_left=job["out_gb"], hold_gb=job["out_gb"]))
            else:
                self.sto_used[s] -= job["storage_gb"]
                self._finish(job, now)
        self.tasks = keep
        np.maximum(self.units_used, 0, out=self.units_used)
        np.maximum(self.mem_used, 0, out=self.mem_used)
        np.maximum(self.sto_used, 0, out=self.sto_used)

    def _finish(self, job, now, ok=True):
        on_time = ok and now <= job["deadline"] + 1
        # Why a job failed: priced out (no quote within its bid), no capacity in reach, or placed but late.
        why = None if on_time else "late" if "placed" in job else job.get("why", "capacity")
        job["result"] = {"t": now, "on_time": on_time, "why": why}
        for leg in ("compute", "delivery"):
            self.ledger.release(job, leg, on_time)
        if on_time:
            self.delivery_s.append((now, now - job["arrival"]))
        self.done.append((now, job["value"], on_time, job["kind"], why))
        if job.get("customer"):
            self.cust_done.append((job["customer"], on_time))
            self.finished.append(job)

    def _weather(self, now, dt):
        """Weather satellites make raw data every tick; raw goes down as is, 'ai' shrinks it onboard first."""
        if self.weather == "off":
            return
        made = WEATHER_RAW_MBPS * dt / 8000
        down = made / (WEATHER_AI_RATIO if self.weather == "ai" else 1)
        s = self.wx_sats
        room = np.maximum(self.fleet.storage[s] - self.sto_used[s], 0)
        add = np.minimum(down, room)
        self.wx_backlog[s] += add
        self.sto_used[s] += add
        self.wx_stats.append((now, made * len(s), 0.0, 0.0))
        for sat in s[self.wx_backlog[s] > 1e-6].tolist():
            f = self.wx_flows.get(sat)
            if f is None:
                job = dict(id=f"wx-{sat}", kind="weather", arrival=now, units=0, mem=0, storage_gb=0, duration=0,
                           out_gb=0, value=0, ref_cost=1, deadline=now + 1800)
                f = self.wx_flows[sat] = dict(job=job, sat=sat, hold_gb=0, weather=True)
                self.flows.append(f)
            f["gb_left"] = self.wx_backlog[sat]
            f["job"]["deadline"] = now + 1800
            f["job"]["value"] = WEATHER_VALUE_PER_GB * f["gb_left"]
            f["job"]["ref_cost"] = REF["downlink"] * f["gb_left"]

    def _candidates(self, geo, job):
        if job.get("sat") is not None:
            return np.array([job["sat"]])
        loc = job.get("location")
        if loc is None:
            return None
        return geo.within_km(*loc, IMAGER_KM if job["kind"] == "image" else FOOTPRINT_KM)

    def _cost(self, job, sats, ground):
        h = job["duration"] / 3600
        pr = self.pr
        return (job["units"] * h * pr.cpu[sats] + job["mem"] * h * pr.mem[sats]
                + job["storage_gb"] / 1000 * max(h, 0.5) * pr.sto[sats] + job["out_gb"] * ground + self._uplink(job)
                + job.get("sensor_usd", 0))

    def _uplink(self, job):
        """The input's way up from the station nearest the customer, the same for every candidate satellite; the
        station is kept on the job so the walkthrough plans the uplink from it."""
        if job["kind"] == "relay" or not job.get("location") or not input_gb(job):
            return 0.0
        g = int(np.argmax(self.con.gs_ecef @ np.array(_unit(*job["location"]))))
        job["uplink_gs"] = g
        return uplink_rates(input_gb(job), float(self.pr.gs[g]))[2]

    def _held(self, t0, t1):
        """Units and memory per satellite that bookings hold somewhere in [t0, t1)."""
        hu, hm = np.zeros(self.con.n_sats), np.zeros(self.con.n_sats)
        for b in self.bookings:
            if b["start_after"] < t1 and b["start_after"] + b["duration"] > t0:
                hu[b["sat"]] += b["units"]
                hm[b["sat"]] += b["mem"]
        return hu, hm

    def _book(self, job):
        """Advance reservation: orbits are predictable, so pick the cheapest satellite that will be in reach at the
        slot and has the capacity free of other bookings; the price is fixed now, the job is served first at the
        slot. Without a fit the job stays an ordinary spot job that starts bidding at the slot."""
        if self.pr is None:
            return
        t0 = job["start_after"]
        cand = self._candidates(Geometry(self.con, t0), job)
        cand = np.arange(self.con.n_sats) if cand is None else cand
        hu, hm = self._held(t0, t0 + job["duration"])
        fit = cand[(self.fleet.units[cand] - hu[cand] >= job["units"]) & (self.fleet.mem[cand] - hm[cand] >= job["mem"])]
        if not len(fit):
            return
        ground = self.net.ground_cost[np.isfinite(self.net.ground_cost)]
        cost = self._cost(job, fit, float(np.median(ground)) if len(ground) else REF["downlink"]) * (1 + BOOK_PREMIUM)
        i = int(np.argmin(cost))
        if cost[i] > job["value"]:
            return
        job.update(sat=int(fit[i]), priority=True, price_cap=float(cost[i]))
        job["booked"] = {"sat": int(fit[i]), "usd": round(float(cost[i]), 4), "start": t0}
        self.bookings.append(job)

    def _clear_pending(self, geo, now, dt, reserve):
        keep, winners = [], []
        pr, mode = self.pr, self.mode
        free_u = self.cap_units - self.units_used
        free_m = self.fleet.mem - self.mem_used
        free_s = self.fleet.storage - self.sto_used
        ground = self.net.ground_cost
        batch = isinstance(mode, BatchAuction)
        queue = mode.order(self.pending)
        queue = [j for j in queue if j.get("priority")] + [j for j in queue if not j.get("priority")]
        for job in queue:
            if now + job["duration"] > job["deadline"]:
                self._finish(job, now, ok=False)
                continue
            if job.get("next_try", 0) > now or job.get("start_after", 0) > now:
                keep.append(job)
                continue
            job["next_try"] = now + RETRY_S
            if job["kind"] == "relay":
                if not self._admit_relay(geo, job, now):
                    keep.append(job)
                continue
            cand = self._candidates(geo, job)
            fu, fm = free_u, free_m
            if self.bookings and "booked" not in job:
                hu, hm = self._held(now, now + max(job["duration"], dt))
                fu, fm = free_u - hu, free_m - hm
            if cand is None:
                fit = np.nonzero((fu >= job["units"]) & (fm >= job["mem"]) & (free_s >= job["storage_gb"]))[0]
            else:
                fit = cand[(fu[cand] >= job["units"]) & (fm[cand] >= job["mem"]) &
                           (free_s[cand] >= job["storage_gb"])] if len(cand) else cand
            if job["out_gb"] > 0 and len(fit):
                fit = fit[np.isfinite(ground[fit])]
            if len(fit) == 0:
                if cand is not None and len(cand):
                    s0 = int(cand[0])
                    self.unmet["cpu"][s0] += job["units"]
                    self.unmet["mem"][s0] += job["mem"]
                    self.unmet["sto"][s0] += job["storage_gb"]
                if batch and winners:
                    self._displaced(job, winners, cand)
                job["why"] = "capacity"
                keep.append(job)
                continue
            cost = self._cost(job, fit, ground[fit])
            if mode.name != "fixed":
                i = int(np.argmin(cost))
            elif job.get("location"):
                i = int(np.argmax(geo.unit[fit] @ np.array(_unit(*job["location"]))))
            else:
                i = 0
            s, price = int(fit[i]), float(cost[i])
            job["route_share"] = min(1.0, job["out_gb"] * float(ground[s]) / price) if price > 0 else 0.0
            job["up_share"] = min(1.0 - job["route_share"], self._uplink(job) / price) if price > 0 else 0.0
            if getattr(mode, "vickrey", False) and price <= job["value"]:
                price = min(float(np.partition(cost, 1)[1]) if len(cost) > 1 else price, job["value"])
            if job.get("priority"):
                price = min(price, job["price_cap"])
            if not mode.accept(job, price):
                job["why"] = "price"
                if job["deadline"] - now > RETRY_S + job["duration"]:
                    keep.append(job)    # re-bids later; long deadlines wait for a cheaper tick
                else:
                    self._finish(job, now, ok=False)
                continue
            job["placed"] = {"sat": s, "price": round(price, 4), "t": now}
            job["quotes"] = [[int(fit[k]), round(float(cost[k]), 4)] for k in np.argsort(cost)[:4]]
            if batch:
                job["crit"] = 0.0
                winners.append((job, s))
            else:
                self._pay(job, now, price, s)
            free_u[s] -= job["units"]
            free_m[s] -= job["mem"]
            free_s[s] -= job["storage_gb"]
            self.units_used[s] += job["units"]
            self.mem_used[s] += job["mem"]
            self.sto_used[s] += job["storage_gb"]
            mode.reprice(self, pr, s, reserve)
            if job["kind"] == "edge":
                d = float(np.linalg.norm(geo.ecef[s] - RE * np.array(_unit(*job["location"]))))
                self.lat_ms.append((now, 2000 * d / C_KM_S))
            if job["duration"] > 0:
                self.tasks.append(dict(job=job, sat=s, left=job["duration"]))
            else:
                self.flows.append(dict(job=job, sat=s, gb_left=job["out_gb"], hold_gb=job["storage_gb"]))
        for job, s in winners:
            pay = max(job["crit"], L_MULT * job["ref_cost"])
            job["placed"]["price"] = round(pay, 4)
            self._pay(job, now, pay, s)
        self.pending = keep

    def _pay(self, job, now, usd, sat):
        self.paid.append((now, usd))
        # The winner's own quote says how much of the price is uplink and route + station; the rest is compute.
        up, route = job.get("up_share", 0.0), job.get("route_share", 1.0 if job["kind"] == "relay" else 0.0)
        self.ledger.open(job, usd, sat, {"uplink": up, "compute": 1 - up - route, "delivery": route})
        self.ledger.release(job, "uplink")      # the input is on board once compute starts
        if job.get("customer"):
            self.cust_paid.append((job["customer"], usd))

    @staticmethod
    def _displaced(job, winners, cand):
        """Critical value: the latest winner that took capacity this job could have used pays at its density."""
        dens = job["value"] / math.sqrt(bundle_size(job))
        for w, s in reversed(winners[-50:]):
            if cand is None or s in cand:
                w["crit"] = max(w["crit"], dens * math.sqrt(bundle_size(w)))
                return

    def _admit_relay(self, geo, job, now):
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
        price = job["out_gb"] * float(sum(self.pr.isl[i] for k, i in self.net.hops(path) if k == "isl"))
        if not self.mode.accept(job, price):
            return False
        if self.net.residual(path) < job["gbps"]:
            self.net.blame(path, job["gbps"], self.unmet)
            return False
        self._pay(job, now, price, sa)
        self.lat_ms.append((now, lat_ms))
        job["placed"] = {"sat": sa, "price": round(price, 4), "t": now, "latency_ms": round(lat_ms, 1)}
        self.flows.append(dict(job=job, sat=sa, dst=sb, gb_left=job["out_gb"], hold_gb=0, start=now))
        return True

    def _move_bits(self, geo, now, dt):
        """Allocate this tick's link capacity to flows in mode order; nothing can take more than what is left."""
        keep = []
        relay_pairs = [(f["sat"], f["dst"]) for f in self.flows if "dst" in f]
        relay_paths = dict(zip(relay_pairs, self.net.paths_between(relay_pairs)))
        fixed = self.mode.name == "fixed"
        order = sorted(self.flows, key=(lambda f: f["job"]["arrival"]) if fixed else
                       (lambda f: -f["job"]["value"] / max(f["job"]["ref_cost"], 1e-9)))
        wx_down = wx_paid = 0.0
        for f in order:
            job = f["job"]
            relay = "dst" in f
            path = relay_paths.get((f["sat"], f["dst"])) if relay else self.net.path_to_ground(f["sat"])
            left_s = max(job["deadline"] - now, dt)
            need = job["gbps"] if relay else f["gb_left"] * 8 / left_s
            want = min(f["gb_left"] * 8 / dt, max(need, ph.DOWNLINK_GBPS))
            got = 0.0
            if path:
                got = max(0.0, min(want, self.net.residual(path)))
                if got > 0:
                    self.net.take(path, got)
                    job.setdefault("down_t", now)
                    self.ledger.carry(job, path, self.net, got * dt / 8, self.pr)
                f["path"] = path
                f["load"] = self.net.bottleneck_util(path)
                if "placed" in job:
                    job["placed"]["path"] = path
                if got < need:
                    self.net.blame(path, need - got, self.unmet)
            f["rate"] = got
            gb = got * dt / 8
            f["gb_left"] -= gb
            if f.get("weather"):
                s = f["sat"]
                self.wx_backlog[s] = max(0.0, self.wx_backlog[s] - gb)
                self.sto_used[s] = max(0.0, self.sto_used[s] - gb)
                wx_down += gb
                wx_paid += gb * float(self.net.ground_cost[s]) if np.isfinite(self.net.ground_cost[s]) else 0
                if self.weather == "off" or self.wx_backlog[s] <= 1e-6:
                    self.wx_flows.pop(s, None)
                    continue
                keep.append(f)
                continue
            if f["gb_left"] <= 1e-6 or (relay and now >= job["deadline"]):
                self.sto_used[f["sat"]] -= f["hold_gb"]
                self._finish(job, now, ok=f["gb_left"] <= 0.05 * max(job["out_gb"], 1e-9))
            elif now > job["deadline"] + 6 * 3600:
                self.sto_used[f["sat"]] -= f["hold_gb"]
                self._finish(job, now, ok=False)
            else:
                keep.append(f)
        if self.weather != "off" and self.wx_stats:
            t, made, _, _ = self.wx_stats[-1]
            self.wx_stats[-1] = (t, made, wx_down, wx_paid)
        np.maximum(self.sto_used, 0, out=self.sto_used)
        self.flows = keep

    # -- reporting ------------------------------------------------------------------------------------------------
    def region_prices(self, geo):
        """Capacity-weighted mean price of the goods over each region right now; None where a region has no station."""
        R = len(REGIONS)
        reg = geo.region
        pr = self.pr

        def mean(values, region, w):
            s = np.bincount(region, weights=values * w, minlength=R)
            n = np.bincount(region, weights=w, minlength=R)
            return [round(float(a / b), 5) if b > 0 else None for a, b in zip(s, n)]

        isl_up = self.net.isl_cap_all > 0
        return {"compute": mean(pr.cpu, reg, self.fleet.units), "memory": mean(pr.mem, reg, self.fleet.mem),
                "storage": mean(pr.sto * 1, reg, self.fleet.storage),
                "isl": mean(pr.isl[isl_up], reg[self.con.isl_u[isl_up]], np.ones(int(isl_up.sum()))),
                "downlink": mean(pr.gs + float(np.median(pr.dl)), self.con.gs_region, np.ones(len(pr.gs)))}

    def resource_state(self):
        """Per resource: utilization of the busiest 10 % of its goods (<= 1) and total demand / capacity."""
        c, u = self.capacities(), self.used()
        names = {"compute": "cpu", "memory": "mem", "storage": "sto", "isl": "isl", "downlink": "gs"}
        out = {}
        for r, k in names.items():
            ok = c[k] > 1e-6
            util = np.clip(u[k][ok] / c[k][ok], 0, 1)
            top = np.sort(util)[-max(1, len(util) // 10):]
            out[r] = {"util": round(float(util.mean()), 3), "hot": round(float(top.mean()), 3),
                      "demand": round(float((u[k][ok].sum() + self.unmet[k][ok].sum()) / c[k][ok].sum()), 3)}
        return out

    def metrics(self):
        d = self.done
        n = len(d)
        on = sum(1 for x in d if x[2])
        lat = sorted(x[1] for x in self.lat_ms)
        dly = sorted(x[1] for x in self.delivery_s)
        rs = self.resource_state()
        share = lambda why: round(sum(1 for x in d if x[4] == why) / n, 3) if n else None
        return {"welfare_per_h": round(sum(x[1] for x in d if x[2])),
                "revenue_per_h": round(sum(x[1] for x in self.paid)),
                "served": round(on / n, 3) if n else None,
                "priced_out": share("price"),
                "missed": round(sum(1 for x in d if x[4] in ("capacity", "late")) / n, 3) if n else None,
                "p99_latency_ms": round(lat[int(0.99 * (len(lat) - 1))], 1) if lat else None,
                "p99_delivery_s": round(dly[int(0.99 * (len(dly) - 1))]) if dly else None,
                "binding": max(rs, key=lambda r: (rs[r]["hot"], rs[r]["demand"])),
                "jobs_per_h": n, "waiting": len(self.pending), "running": len(self.tasks), "flows": len(self.flows)}

    def weather_metrics(self):
        if not self.wx_stats:
            return None
        hours = max((self.wx_stats[-1][0] - self.wx_stats[0][0]) / 3600, 1 / 60)
        made = sum(x[1] for x in self.wx_stats)
        down = sum(x[2] for x in self.wx_stats)
        return {"raw_gbps": round(made * 8 / hours / 3600, 1), "down_gbps": round(down * 8 / hours / 3600, 2),
                "paid_per_h": round(sum(x[3] for x in self.wx_stats) / hours), "backlog_gb":
                round(float(self.wx_backlog.sum()), 1), "sats": len(self.wx_sats)}
