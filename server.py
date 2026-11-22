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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from sim.market import CLEARING, REF, RES, Demand, Engine, ref_cost
from sim.orbit import REGIONS, Constellation, Geometry, gmst

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
TICK_SIM_S = 60
TICK_WALL_S = 1.0
HISTORY = 24 * 60   # ticks of price history kept (one simulated day)
MAX_API_JOBS = 5000
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
         ".json": "application/json", ".svg": "image/svg+xml"}


class Sim:
    def __init__(self, warmup_hours):
        self.lock = threading.Lock()
        self.t = time.time() - warmup_hours * 3600
        self.con = Constellation(self.t)
        self.demand = Demand(json.load(open(os.path.join(STATIC, "land.json"))))
        self.engines = {k: Engine(self.con, CLEARING[k]()) for k in ("market", "baseline")}
        self.injected = []
        self.api_jobs = {}
        self.history = []
        self.snapshot = b"{}"
        self.constellation = json.dumps(self.con.static_json()).encode()
        for _ in range(int(warmup_hours * 3600 / TICK_SIM_S)):
            self.step()

    def step(self):
        geo = Geometry(self.con, self.t)
        arrivals = self.demand.arrivals(self.t, TICK_SIM_S, geo.sun, gmst(self.t))
        with self.lock:
            arrivals += self.injected
            self.injected = []
        for k, eng in self.engines.items():
            # The market keeps the originals so API jobs can be looked up; the baseline gets copies.
            eng.tick(geo, TICK_SIM_S, arrivals if k == "market" else copy.deepcopy(arrivals))
        self.t += TICK_SIM_S
        self._snapshot(geo)

    def _snapshot(self, geo):
        m, b = self.engines["market"], self.engines["baseline"]
        gp = m.global_pressure()
        prices = {r: [round(float(x), 4) for x in m.clear.p[r]] for r in RES}
        self.history.append({"t": self.t, **{r: round(float(np.median(m.clear.p[r])), 4) for r in RES}})
        del self.history[:-HISTORY]
        util = np.clip(np.minimum(m.units_used, m.cap_units) / m.fleet.units, 0, 1)
        flags = (util * 127).astype(np.uint8) | np.where(geo.sunlit, 0, 128).astype(np.uint8)
        flows = sorted(m.flows, key=lambda f: -f.get("rate", 0))[:80]
        snap = {
            "t": self.t, "speed": TICK_SIM_S / TICK_WALL_S, "gmst": gmst(self.t), "sun": geo.sun.round(5).tolist(),
            "regions": REGIONS, "ref": REF, "prices": prices,
            "pressure": {r: [None if not math.isfinite(x) else round(float(x), 3) for x in m.pressure[r]] for r in RES},
            "global_pressure": gp, "binding": max(gp, key=gp.get), "limits": m.binding,
            "metrics": {"market": m.metrics(), "baseline": b.metrics()},
            "sats": base64.b64encode(flags.tobytes()).decode(),
            "routes": [{"id": f["job"]["id"], "path": f["path"], "load": round(f.get("load", 0), 2)}
                       for f in flows if f.get("path") and f.get("rate", 0) > 0],
            "history": self.history[-1],
        }
        self.snapshot = json.dumps(snap).encode()

    def run(self):
        while True:
            start = time.monotonic()
            self.step()
            time.sleep(max(0.0, TICK_WALL_S - (time.monotonic() - start)))

    def submit(self, req):
        job = parse_job(req, self.t)
        with self.lock:
            if len(self.api_jobs) >= MAX_API_JOBS:
                self.api_jobs.pop(next(iter(self.api_jobs)))
            job["id"] = f"api-{self.demand.next_id}"
            self.demand.next_id += 1
            self.injected.append(job)
            self.api_jobs[job["id"]] = job
        return job

    def sats_summary(self):
        m = self.engines["market"]
        out = {}
        for o in sorted(set(self.con.owner.tolist())):
            mask = self.con.owner == o
            out[o] = {"satellites": int(mask.sum()), "compute_units": int(m.fleet.units[mask].sum()),
                      "units_in_use": round(float(m.units_used[mask].sum())),
                      "classes": {c: int((mask & (self.con.cls == c)).sum()) for c in "SML"}}
        return {"total": self.con.n_sats, "owners": out, "shells": json.loads(self.constellation)["shells"]}


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
    deadline = _num(req, "deadline_s", 30, 86400)
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
    job["ref_cost"] = ref_cost(job)
    return job


def job_view(job):
    out = {"id": job["id"], "deadline": job["deadline"], "max_price_usd": job["value"],
           "quote_at_anchor_usd": round(job["ref_cost"], 4)}
    if "placed" in job:
        out["placed"] = job["placed"]
    if "result" in job:
        out["status"] = "done" if job["result"]["on_time"] else "missed"
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
        path = self.path.split("?")[0]
        sim = self.sim
        if path == "/api/state":
            return self._send(200, sim.snapshot)
        if path == "/api/constellation":
            return self._send(200, sim.constellation)
        if path == "/api/prices":
            m = sim.engines["market"]
            return self._json(200, {"t": sim.t, "units": {"compute": "$/unit-h", "memory": "$/GB-h",
                                                         "storage": "$/TB-h", "isl": "$/GB per hop",
                                                         "downlink": "$/GB"},
                                    "regions": REGIONS,
                                    "prices": {r: [round(float(x), 4) for x in m.clear.p[r]] for r in RES},
                                    "history": sim.history})
        if path == "/api/sats":
            return self._json(200, sim.sats_summary())
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
        if self.path.split("?")[0] != "/api/jobs":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 16384:
            return self._json(400, {"error": "body must be 1..16384 bytes of JSON"})
        try:
            job = self.sim.submit(json.loads(self.rfile.read(n)))
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": str(e)})
        self._json(202, job_view(job))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8510)
    ap.add_argument("--warmup-hours", type=float, default=6)
    a = ap.parse_args()
    Handler.sim = Sim(a.warmup_hours)
    threading.Thread(target=Handler.sim.run, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
