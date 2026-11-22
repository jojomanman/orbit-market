#!/usr/bin/env python3
"""orbit-market: one process that runs the cluster simulation and serves the API and the globe.

The server owns market state only (prices, jobs, loads, batteries); the browser propagates the 10,000 orbits itself
from GET /api/constellation, so a client poll is ~30 KB per second instead of 10k positions per frame. One tick =
TICK_SIM_S of simulated time every TICK_WALL_S of wall time; the market and the baseline run side by side on the
same arrivals. Paths are relative because Tailscale Funnel strips the /orbit prefix before it gets here.

Usage: server.py [--port 8510] [--warmup-hours 6]
"""
import argparse
import base64
import copy
import json
import math
import os
import random
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from sim.market import (DEFAULT_MODE, IMAGER_KM, MODES, REF, RES, WEATHER_AI_RATIO, WEATHER_AI_UNITS, WEATHER_RAW_MBPS, Demand,
                        Engine, ref_cost, sun_elevation_sin)
from sim import settlement
from sim.customers import Customers
from sim.orbit import REGIONS, Constellation, Geometry, gmst, passes, region_of, sun_vector

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
TICK_SIM_S = 60
TICK_WALL_S = 2.0
HISTORY = 24 * 60   # ticks of price history kept (one simulated day)
MAX_API_JOBS = 5000
URGENCIES = [60, 900, 3600, 6 * 3600, 86400]
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
         ".json": "application/json", ".svg": "image/svg+xml", ".jpg": "image/jpeg"}


class Sim:
    def __init__(self, warmup_hours, demand):
        self.lock = threading.Lock()
        self.t = time.time() - warmup_hours * 3600
        self.con = Constellation(self.t)
        land = json.load(open(os.path.join(STATIC, "land.json")))
        self.demand = Demand(land)
        self.customers = Customers(os.path.join(ROOT, "data", "customers"), land, self.demand.rng) \
            if demand == "customers" else None
        self.engines = {k: Engine(self.con, k) for k in MODES}
        self.injected = []
        self.api_jobs = {}
        self.history = {k: [] for k in MODES}
        self.region_hist = deque(maxlen=HISTORY)   # (t, compute by region, downlink by region) of the posted mode
        self.snapshots = {}
        self.geo = None
        self.weather = "off"
        self.constellation = json.dumps(self.con.static_json()).encode()
        self.featured = None
        self.gs_names = [g["name"] for g in self.con.static_json()["ground_stations"]]
        self.imagers = np.nonzero(np.isin(self.con.shell, (2, 3)))[0]
        for _ in range(int(warmup_hours * 3600 / TICK_SIM_S)):
            self.step()

    def step(self):
        geo = Geometry(self.con, self.t)
        if self.customers is None:
            arrivals = self.demand.arrivals(self.t, TICK_SIM_S, geo.sun, gmst(self.t))
        else:
            if int(self.t) % 600 < TICK_SIM_S:
                self.customers.reload()
            ev = self.demand.event if self.demand.event and self.t < self.demand.event["until"] else None
            arrivals = self.customers.arrivals(self.t, TICK_SIM_S, ev, self.price_index(), self.demand._poisson)
            for j in arrivals:
                j["id"] = self.demand.next_id
                j["ref_cost"] = ref_cost(j)
                self.demand.next_id += 1
        with self.lock:
            arrivals += self.injected
            self.injected = []
        # The default engine keeps the originals so API jobs can be looked up; the others get untouched copies.
        copies = {k: copy.deepcopy(arrivals) for k in self.engines if k != DEFAULT_MODE}
        for k, eng in self.engines.items():
            eng.weather = self.weather
            eng.tick(geo, TICK_SIM_S, copies.get(k, arrivals))
        if self.customers is not None:
            p = self.engines[DEFAULT_MODE]
            self.customers.record(p.cust_done, p.cust_paid)
        self.t += TICK_SIM_S
        self.geo = geo
        self._snapshot(geo)

    def _snapshot(self, geo):
        modes = {k: {"label": e.mode.label, **e.metrics()} for k, e in self.engines.items()}
        ev = self.demand.event
        common = {"t": self.t, "speed": TICK_SIM_S / TICK_WALL_S, "gmst": gmst(self.t),
                  "sun": geo.sun.round(5).tolist(), "regions": REGIONS, "ref": REF, "modes": modes,
                  "scenario": {"event": ev if ev and self.t < ev["until"] else None, "weather": self.weather,
                               "weather_metrics": self.engines[DEFAULT_MODE].weather_metrics(),
                               "weather_compare": self.weather_compare()}}
        for k, e in self.engines.items():
            rp = e.region_prices(geo)
            if k == DEFAULT_MODE:
                self.region_hist.append((self.t, rp["compute"], rp["downlink"]))
            h = {"t": self.t, **{r: _median([x for x in rp[r] if x is not None]) for r in RES}}
            self.history[k].append(h)
            del self.history[k][:-HISTORY]
            util = np.clip(np.minimum(e.units_used, e.cap_units) / e.fleet.units, 0, 1)
            flags = (util * 127).astype(np.uint8) | np.where(geo.sunlit, 0, 128).astype(np.uint8)
            flows = sorted((f for f in e.flows if f.get("path") and f.get("rate", 0) > 0),
                           key=lambda f: -f["rate"])[:60]
            snap = {**common, "mode": k, "prices": rp, "resources": e.resource_state(),
                    "sats": base64.b64encode(flags.tobytes()).decode(),
                    "routes": [{"id": f["job"]["id"], "path": f["path"], "load": round(f.get("load", 0), 2)}
                               for f in flows],
                    "history": h}
            self.snapshots[k] = json.dumps(snap, default=_np).encode()

    def price_index(self):
        """Posted compute and downlink prices vs anchor, geometric mean: what elastic customers react to."""
        e = self.engines[DEFAULT_MODE]
        if e.pr is None:
            return 1.0
        return math.sqrt(float(np.median(e.pr.cpu)) / REF["compute"] * (float(np.median(e.pr.gs)) + float(np.median(e.pr.dl))) /
                         REF["downlink"])

    def weather_compare(self):
        """Raw stream vs onboard AI for the weather fleet at this tick's posted prices: bandwidth and cost per hour."""
        e = self.engines[DEFAULT_MODE]
        if e.pr is None:
            return None
        n = len(e.wx_sats)
        raw_gbps = n * WEATHER_RAW_MBPS / 1000
        ground = e.net.ground_cost[e.wx_sats]
        p_dl = float(np.median(ground[np.isfinite(ground)])) if np.isfinite(ground).any() else REF["downlink"]
        p_cpu = float(np.median(e.pr.cpu[e.wx_sats]))
        gb_h = raw_gbps / 8 * 3600
        return {"raw": {"gbps": round(raw_gbps, 1), "usd_h": round(gb_h * p_dl)},
                "ai": {"gbps": round(raw_gbps / WEATHER_AI_RATIO, 2),
                       "usd_h": round(gb_h / WEATHER_AI_RATIO * p_dl + n * WEATHER_AI_UNITS * p_cpu),
                       "units": n * WEATHER_AI_UNITS},
                "saved": round(1 - 1 / WEATHER_AI_RATIO, 3)}

    def run(self):
        while True:
            start = time.monotonic()
            self.step()
            time.sleep(max(0.0, TICK_WALL_S - (time.monotonic() - start)))

    def submit(self, req):
        job = parse_job(req, self.t) if "image" not in req else self.image_job(req)
        with self.lock:
            if len(self.api_jobs) >= MAX_API_JOBS:
                self.api_jobs.pop(next(iter(self.api_jobs)))
            job["id"] = f"api-{self.demand.next_id}"
            self.demand.next_id += 1
            self.injected.append(job)
            self.api_jobs[job["id"]] = job
        return job

    def next_featured(self):
        """Follow one real request of the default market, just matched (most arrivals match within their tick):
        prefer a customer job that computes and then downlinks, so every step of its life shows."""
        e = self.engines[DEFAULT_MODE]
        fresh = [t["job"] for t in list(e.tasks) if t["job"].get("customer") and t["job"]["placed"]["t"] >= self.t - 2 * TICK_SIM_S]
        full = [j for j in fresh if j["out_gb"] > 0 and j.get("location")]
        pool = full or fresh or [t["job"] for t in list(e.tasks)]
        self.featured = random.choice(pool) if pool else None
        return self.featured_view()

    def featured_view(self):
        """The featured job as a transaction: request, quotes, route with owners, timings, settlement."""
        j = self.featured
        if j is None:
            return {"job": None}
        org = next((o for o in self.customers.orgs if o["id"] == j.get("customer")), None) if self.customers else None
        p = j.get("placed") or {}
        path = p.get("path") or []
        n = self.con.n_sats
        hops = [{"sat": int(x), "owner": str(self.con.owner[x])} for x in path if x < n]
        station = next((self.gs_names[x - n] for x in path if x >= n), None)
        status = ("delivered" if j["result"]["on_time"] else "failed") if "result" in j else \
            "downlink" if "down_t" in j else "compute" if "placed" in j else "pending"
        out = {"id": f"0x{abs(hash((j['id'], j['arrival']))) & 0xffffffffffff:012x}", "status": status,
               "why": org["name"] if org else "API", "what": org["what"][:120] if org else "",
               "type": j.get("ctype", j["kind"]).replace("_", " "),
               "region": REGIONS[int(region_of_point(*j["location"]))] if j.get("location") else None,
               "spec": {"cpu": j["units"], "mem_gb": round(j["mem"], 1), "storage_gb": round(j["storage_gb"], 1),
                        "run_s": round(j["duration"]), "out_gb": round(j["out_gb"], 2)},
               "offer": {"max_usd": round(j["value"], 2), "deadline_s": round(j["deadline"] - j["arrival"])},
               "t": {"now": self.t, "arrival": j["arrival"], "matched": p.get("t"), "computed": j.get("computed_t"),
                     "down": j.get("down_t"), "done": j["result"]["t"] if "result" in j else None},
               "speed": TICK_SIM_S / TICK_WALL_S,
               "quotes": [{"sat": s, "owner": str(self.con.owner[s]), "usd": u} for s, u in j.get("quotes", [])],
               "sat": p.get("sat"), "price_usd": p.get("price"), "path": [int(x) for x in path],
               "hops": hops, "station": station, "why_failed": j["result"]["why"] if "result" in j else None}
        if "settle" in j:
            out["settlement"] = settlement.job_view(j["settle"])
        return out

    def customer_jobs(self, cid, mode):
        """One customer's live and recently finished jobs in one engine: where, which satellite, which route."""
        e = self.engines[mode]
        live = [(j, "waiting") for j in list(e.pending)] + [(t["job"], "computing") for t in list(e.tasks)] \
            + [(f["job"], "downlink") for f in list(e.flows)]
        done = [(j, "served" if j["result"]["on_time"] else "missed") for j in list(e.finished)]
        out = []
        for j, status in live + done[::-1]:
            if j.get("customer") != cid:
                continue
            p = j.get("placed") or {}
            out.append({"id": j["id"], "status": status, "loc": j.get("location"),
                        "sat": None if p.get("sat") is None else int(p["sat"]),
                        "path": [int(x) for x in p["path"]] if p.get("path") else None, "usd": p.get("price")})
            if len(out) >= 40:
                break
        return out

    # -- use case: an image of a location -------------------------------------------------------------------------
    def quote(self, lat, lon):
        """Price of 'an image of this point, delivered within W' for each urgency W.

        Options are real passes of the imaging satellites (S3 radar, S4 optical which needs the sun up). An option
        within 15 min pays today's prices in the target's region; a later one is priced at the seasonal-naive
        forecast (the same region, same hour yesterday), so waiting can buy a quieter hour. Cheapest option wins."""
        now = self.t
        opts = passes(self.con, self.imagers, now, lat, lon, max(URGENCIES), IMAGER_KM, step_s=30)
        region = int(region_of_point(lat, lon))
        e = self.engines[DEFAULT_MODE]
        out = []
        for w in URGENCIES:
            best = None
            for tp, sat in opts:
                if tp + IMAGE_PROC_S > now + w:
                    break
                if self.con.shell[sat] == 3 and sun_elev_sin(tp, lat, lon) < 0.17:
                    continue
                cpu, dl = self.forecast(region, tp)
                price = IMAGE_UNITS * IMAGE_PROC_S / 3600 * cpu + IMAGE_OUT_GB * dl + IMAGE_RAW_GB / 1000 * 0.5 \
                    * float(np.median(e.pr.sto))
                if best is None or price < best["price_usd"]:
                    best = {"price_usd": round(price, 2), "pass_t": tp, "sat": sat,
                            "delivery_s": round(tp - now + IMAGE_PROC_S + 2)}
            out.append({"within_s": w, **(best or {"price_usd": None})})
        return {"lat": lat, "lon": lon, "region": REGIONS[region], "t": now, "options": out}

    def quote_job(self, req, mode):
        """Live quote for any job: cheapest feasible satellite at this tick's prices, with a breakdown per good and
        the urgency premium (today's price minus the cheapest seasonal-naive forecast inside the deadline)."""
        job = parse_job(req, self.t)
        e, geo = self.engines[mode], self.geo
        cand = e._candidates(geo, job)
        cand = np.arange(self.con.n_sats) if cand is None else cand
        free = ((e.cap_units[cand] - e.units_used[cand] >= job["units"]) &
                (e.fleet.mem[cand] - e.mem_used[cand] >= job["mem"]) &
                (e.fleet.storage[cand] - e.sto_used[cand] >= job["storage_gb"]))
        ground = e.net.ground_cost[cand]
        ok = free & (np.isfinite(ground) | (job["out_gb"] == 0))
        if not ok.any():
            return {"feasible": False, "reason": "no satellite in reach has the capacity now" if len(cand) else
                    "no satellite over that point now"}
        h = job["duration"] / 3600
        pr = e.pr
        parts = {"compute": job["units"] * h * pr.cpu[cand], "memory": job["mem"] * h * pr.mem[cand],
                 "storage": job["storage_gb"] / 1000 * max(h, 0.5) * pr.sto[cand]}
        total_all = sum(parts.values()) + job["out_gb"] * np.where(np.isfinite(ground), ground, 0)
        i = int(np.argmin(np.where(ok, total_all, np.inf)))
        sat = int(cand[i])
        out = {k: float(v[i]) for k, v in parts.items()}
        path = e.net.path_to_ground(sat) if job["out_gb"] > 0 else None
        if path:
            down = float(pr.dl[path[-2]] + pr.gs[path[-1] - self.con.n_sats])
            out["downlink"] = job["out_gb"] * down
            out["route"] = job["out_gb"] * (float(ground[i]) - down)
        now_price = sum(out.values())
        region = int(geo.region[sat])
        horizon = job["deadline"] - self.t - job["duration"]
        cheapest = now_price
        if horizon > 900 and self.region_hist:
            for t in np.arange(self.t + 900, self.t + horizon, 900):
                cpu, dl = self.forecast(region, float(t))
                f = job["units"] * h * cpu + job["out_gb"] * dl + out["memory"] + out["storage"]
                cheapest = min(cheapest, f)
        out["urgency_premium"] = max(0.0, now_price - cheapest)
        total = min(now_price, job["price_cap"]) if job.get("priority") else now_price
        order = np.argsort(np.where(ok, total_all, np.inf))[:8]
        quotes = [{"sat": int(cand[k]), "owner": str(self.con.owner[cand[k]]), "usd": round(float(total_all[k]), 4)}
                  for k in order if ok[k]]
        if getattr(e.mode, "vickrey", False) and len(quotes) > 1:
            total = min(quotes[1]["usd"], job["value"]) if quotes[0]["usd"] <= job["value"] else total
        return {"feasible": True, "mode": mode, "sat": sat, "region": REGIONS[region], "path": path,
                "total_usd": round(total, 4), "market_usd": round(now_price, 4), "quotes": quotes,
                "per_hour_usd": round(total / max(job["duration"] / 3600, 1 / 60), 4) if job.get("rate") else None, "wait_price_usd": round(cheapest, 4),
                "breakdown": {k: round(v, 4) for k, v in out.items()}, "anchor_usd": round(job["ref_cost"], 4)}

    def forecast(self, region, tp):
        hist = self.region_hist
        if not hist or tp - self.t <= 900:
            cur = hist[-1] if hist else None
        else:
            target = tp - 86400
            cur = min(hist, key=lambda h: abs(h[0] - target)) if hist[0][0] <= target + 1800 else hist[-1]
        cpu = cur[1][region] if cur and cur[1][region] is not None else REF["compute"]
        dls = [x for x in cur[2] if x is not None] if cur else []
        return cpu, (min(dls) if dls else REF["downlink"])

    def image_job(self, req):
        p = _point(req, "image")
        deadline = _num(req, "deadline_s", 60, 86400)
        q = self.quote(*p)
        opt = next((o for o in q["options"] if o["within_s"] >= deadline and o["price_usd"] is not None), None)
        if opt is None:
            raise ValueError("no pass over that point within the deadline")
        value = _num(req, "max_price_usd", 0, 1e7, default=opt["price_usd"] * 1.5)
        job = dict(kind="image", units=IMAGE_UNITS, mem=4 * IMAGE_UNITS, storage_gb=IMAGE_RAW_GB,
                   duration=IMAGE_PROC_S, out_gb=IMAGE_OUT_GB, location=p, sat=opt["sat"],
                   start_after=opt["pass_t"], arrival=self.t, deadline=self.t + deadline, value=value,
                   quote_usd=opt["price_usd"])
        job["ref_cost"] = ref_cost(job)
        return job

    def set_scenario(self, req):
        if "event" in req:
            ev = req["event"]
            if ev is None:
                self.demand.event = None
            else:
                lat, lon = _point({"p": ev}, "p")
                hours = _num(ev, "hours", 0.5, 24, 3)
                self.demand.event = {"lat": lat, "lon": lon, "until": self.t + hours * 3600,
                                     "name": str(ev.get("name", "event"))[:40]}
        if "weather" in req:
            if req["weather"] not in ("off", "raw", "ai"):
                raise ValueError("weather must be off, raw or ai")
            self.weather = req["weather"]
        return {"event": self.demand.event, "weather": self.weather}

    def sats_summary(self):
        m = self.engines[DEFAULT_MODE]
        out = {}
        for o in sorted(set(self.con.owner.tolist())):
            mask = self.con.owner == o
            out[o] = {"satellites": int(mask.sum()), "compute_units": int(m.fleet.units[mask].sum()),
                      "units_in_use": round(float(m.units_used[mask].sum())),
                      "classes": {c: int((mask & (self.con.cls == c)).sum()) for c in "SML"}}
        return {"total": self.con.n_sats, "owners": out, "shells": json.loads(self.constellation)["shells"]}


IMAGE_UNITS, IMAGE_PROC_S, IMAGE_RAW_GB, IMAGE_OUT_GB = 16, 30, 20.0, 0.5
TOKENS_PER_UNIT_S = 40     # assumption: one accelerator share serves a ~8B model at ~40 tokens/s
MIN_RTT_MS = 8             # 2 x slant range (~1,200 km at 25 deg elevation) / c


def region_of_point(lat, lon):
    return region_of(np.array([lat]), np.array([lon]))[0]


def sun_elev_sin(t, lat, lon):
    return sun_elevation_sin(sun_vector(t), gmst(t), lat, lon)


def _median(xs):
    return round(float(np.median(xs)), 5) if xs else None


def _np(o):
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(type(o))


def _num(d, key, lo, hi, default=None):
    v = d.get(key, default)
    if v is None:
        raise ValueError(f"{key} required")
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"{key} must be a number in [{lo}, {hi}]")
    return float(v)


def _point(d, key):
    p = d.get(key)
    if p is None:
        return None
    if not isinstance(p, dict):
        raise ValueError(f"{key} must be {{lat, lon}}")
    return _num(p, "lat", -90, 90), _num(p, "lon", -180, 180)


def parse_job(req, now):
    """spec/job-request.schema.json -> internal job. Raises ValueError with a message the client can act on."""
    if not isinstance(req, dict):
        raise ValueError("body must be a JSON object")
    cpu = _num(req, "cpu", 0, 64, 0)
    job = dict(units=cpu, mem=_num(req, "memory_gb", 0, 256, 0), storage_gb=_num(req, "storage_gb", 0, 8000, 0),
               duration=_num(req, "duration_s", 0, 86400, 0), out_gb=0.0, location=_point(req, "location"),
               arrival=now, kind="api")
    rate = req.get("rate")
    if rate is not None:
        # Rate reservation: tokens/s held for duration_s on a satellite in view, priced per hour.
        if not isinstance(rate, dict):
            raise ValueError("rate must be {tokens_per_s, p95_ms}")
        tps = _num(rate, "tokens_per_s", 1, 64 * TOKENS_PER_UNIT_S)
        p95 = _num(rate, "p95_ms", 1, 10000)
        if p95 < MIN_RTT_MS:
            raise ValueError(f"p95_ms below the ~{MIN_RTT_MS} ms round trip to a satellite overhead")
        job.update(units=math.ceil(tps / TOKENS_PER_UNIT_S), kind="edge", rate={"tokens_per_s": tps, "p95_ms": p95})
        job["mem"] = max(job["mem"], 4 * job["units"])
        if job["location"] is None or job["duration"] <= 0:
            raise ValueError("a rate reservation needs location and duration_s")
        cpu = job["units"]
    deadline = _num(req, "deadline_s", 30, 86400, job["duration"] + 60 if rate else None)
    job["deadline"] = now + deadline
    net = req.get("network")
    if net is not None:
        if not isinstance(net, dict):
            raise ValueError("network must be an object")
        job["out_gb"] = _num(net, "gb", 0.001, 100000)
        to = net.get("to", "ground")
        if to != "ground":
            job["dest"] = _point(net, "to")
            if job["location"] is None or job["duration"] <= 0:
                raise ValueError("a point-to-point transfer needs location and duration_s")
            job["kind"] = "relay"
            job["gbps"] = job["out_gb"] * 8 / job["duration"]
            lat = net.get("latency_ms") or {"max": 500}
            if not isinstance(lat, dict) or len(lat) != 1 or next(iter(lat)) not in ("max", "p50"):
                raise ValueError("latency_ms must be {max: n} or {p50: n}")
            job["latency"] = {next(iter(lat)) + "_ms": _num(lat, next(iter(lat)), 1, 10000)}
        elif cpu == 0:
            job["storage_gb"] = max(job["storage_gb"], job["out_gb"])
            if job["location"] is None:
                raise ValueError("data to downlink needs a location (where it was collected)")
    if cpu == 0 and job["out_gb"] == 0:
        raise ValueError("ask for cpu or network")
    if cpu > 0 and job["duration"] <= 0:
        raise ValueError("duration_s required with cpu")
    if job["duration"] > deadline:
        raise ValueError("duration_s exceeds deadline_s")
    job["value"] = _num(req, "max_price_usd", 0, 1e7)
    if req.get("priority") == "reserve":
        # Emergency-services subscription: served ahead of the spot queue, never charged above its cap.
        job["priority"] = True
        job["price_cap"] = _num(req, "price_cap_usd", 0, 1e7)
    start = _num(req, "start_in_s", 0, 7 * 86400, 0)
    if start:
        # Advance reservation of a future slot; the deadline counts from the slot start.
        if job["kind"] == "relay":
            raise ValueError("start_in_s is not supported for point-to-point transfers")
        job["start_after"] = now + start
        job["deadline"] += start
    job["ref_cost"] = ref_cost(job)
    return job


def job_view(job):
    out = {"id": job["id"], "deadline": job["deadline"], "max_price_usd": job["value"],
           "quote_at_anchor_usd": round(job["ref_cost"], 4)}
    if "placed" in job:
        out["placed"] = job["placed"]
    if "quote_usd" in job:
        out["quote_usd"] = job["quote_usd"]
    if "booked" in job:
        out["booked"] = job["booked"]
    if "settle" in job:
        out["settlement"] = settlement.job_view(job["settle"])
    if "result" in job:
        out["status"] = "done" if job["result"]["on_time"] else job["result"]["why"] or "missed"
    else:
        out["status"] = "running" if "placed" in job else "waiting"
    return out


class Handler(BaseHTTPRequestHandler):
    sim = None
    server_version = "orbit-market"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "max-age=300")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
        sim = self.sim
        mode = q.get("mode") if q.get("mode") in MODES else DEFAULT_MODE
        if path == "/api/state":
            return self._send(200, sim.snapshots.get(mode, b"{}"))
        if path == "/api/constellation":
            return self._send(200, sim.constellation)
        if path == "/api/prices":
            e = sim.engines[mode]
            return self._json(200, {"t": sim.t, "mode": mode, "units": {
                "compute": "$/unit-h", "memory": "$/GB-h", "storage": "$/TB-h", "isl": "$/GB per hop",
                "downlink": "$/GB"}, "regions": REGIONS, "prices": e.region_prices(sim.geo),
                "history": sim.history[mode]})
        if path == "/api/sats":
            return self._json(200, sim.sats_summary())
        if path == "/api/featured":
            return self._json(200, sim.featured_view() if q.get("next") is None and sim.featured else sim.next_featured())
        if path == "/api/settlement":
            return self._json(200, {"t": sim.t, "mode": mode, **sim.engines[mode].ledger.view()})
        if path == "/api/scenario":
            return self._json(200, {"event": sim.demand.event, "weather": sim.weather})
        if path == "/api/customers":
            c = sim.customers
            return self._json(200, {"customers": c.table() if c else [], "problems": c.problems if c else []})
        if path.startswith("/api/customers/") and path.endswith("/jobs"):
            return self._json(200, {"jobs": sim.customer_jobs(path.split("/")[3], mode)})
        if path == "/api/quote":
            try:
                lat, lon = float(q.get("lat", "x")), float(q.get("lon", "x"))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "lat in [-90, 90] and lon in [-180, 180] required"})
            return self._json(200, sim.quote(lat, lon))
        if path.startswith("/api/jobs/"):
            job = sim.api_jobs.get(path.rsplit("/", 1)[1])
            return self._json(200, job_view(job)) if job else self._json(404, {"error": "unknown job"})
        if path == "/" or path == "":
            path = "/index.html"
        full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        with open(full, "rb") as f:
            self._send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/jobs", "/api/scenario", "/api/quote"):
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 16384:
            return self._json(400, {"error": "body must be 1..16384 bytes of JSON"})
        try:
            req = json.loads(self.rfile.read(n))
            if not isinstance(req, dict):
                raise ValueError("body must be a JSON object")
            if path == "/api/scenario":
                return self._json(200, self.sim.set_scenario(req))
            if path == "/api/quote":
                mode = self.path.partition("mode=")[2]
                return self._json(200, self.sim.quote_job(req, mode if mode in MODES else DEFAULT_MODE))
            job = self.sim.submit(req)
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": str(e)})
        self._json(202, job_view(job))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8510)
    ap.add_argument("--warmup-hours", type=float, default=6)
    ap.add_argument("--demand", choices=["customers", "synthetic"], default="customers")
    a = ap.parse_args()
    Handler.sim = Sim(a.warmup_hours, a.demand)
    threading.Thread(target=Handler.sim.run, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
