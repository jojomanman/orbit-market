"""Who gets paid when a job crosses several owners' satellites.

Jonas 2026-11-22 14:06: the offer includes forwarding, like an ISP. The customer pays ONE price to the entry
provider (the owner of the satellite that takes the job); owners settle the relaying among themselves. Pairs whose
traffic to each other is roughly balanced peer settlement-free; otherwise the entry owner pays the forwarder transit
per GB. Ground stations are always paid per GB. The price sits in escrow and is released per leg on receipt (compute
when the result exists, delivery when the bits land); a failed leg is refunded. The market keeps a 2 % fee.

Routing does not yet prefer own > peer > transit: the mesh computes one shared shortest-path tree per tick, so a
per-entry-owner preference would need one tree per owner (4x the Dijkstra cost).
"""
import math

FEE = 0.02
PEER_RATIO = 0.5          # traffic within 2:1 in both directions = peers
WINDOW_S = 24 * 3600      # traffic memory (exponential decay) that the peering test looks at
TRANSIT_MARKUP = 1.2      # transit per GB = the forwarder's own link price x this
STATION = "GS"            # ground station operators, settled as one party


class Ledger:
    def __init__(self, owner):
        self.owner = owner                  # owner letter per satellite
        self.traffic = {}                   # (entry, forwarder) -> GB of entry's customers carried, decayed
        self.transit = {}                   # (payer, payee) -> $ in the window, decayed
        self.revenue = {}                   # owner -> $ released from escrow, decayed
        self.fee = self.refunded = 0.0
        self.held = 0.0                     # $ in escrow right now

    def decay(self, dt):
        k = math.exp(-dt / WINDOW_S)
        for d in (self.traffic, self.transit, self.revenue):
            for key in d:
                d[key] *= k
        self.fee *= k
        self.refunded *= k

    def relation(self, a, b):
        if a == b:
            return "own"
        if b == STATION:
            return "transit"
        ab, ba = self.traffic.get((a, b), 0.0), self.traffic.get((b, a), 0.0)
        return "peer" if min(ab, ba) >= PEER_RATIO * max(ab, ba) > 0 else "transit"

    def open(self, job, usd, sat, compute_share):
        fee = FEE * usd
        net = usd - fee
        job["settle"] = {"entry": str(self.owner[sat]), "price": round(usd, 4), "fee": round(fee, 4),
                         "legs": {"compute": {"usd": net * compute_share, "state": "escrow"},
                                  "delivery": {"usd": net * (1 - compute_share), "state": "escrow"}},
                         "forwarders": {}}
        self.fee += fee
        self.held += net

    def release(self, job, leg, ok=True):
        st = job.get("settle")
        if not st or st["legs"][leg]["state"] != "escrow":
            return
        usd = st["legs"][leg]["usd"]
        self.held -= usd
        if ok:
            st["legs"][leg]["state"] = "paid"
            self.revenue[st["entry"]] = self.revenue.get(st["entry"], 0.0) + usd
        else:
            st["legs"][leg]["state"] = "refunded"
            self.refunded += usd

    def carry(self, job, path, net, gb, prices):
        """gb of this job crossed this path this tick: count each foreign carrier's traffic, accrue transit."""
        st = job.get("settle")
        if not st or gb <= 0:
            return
        entry = st["entry"]
        for a, b in zip(path, path[1:]):
            if b >= net.n:      # the sender's downlink terminal, then the station
                hops = [(str(self.owner[a]), prices.dl[a]), (STATION, prices.gs[b - net.n])]
            else:
                hops = [(str(self.owner[b]), prices.isl[net.edge_key[(a, b)]])]
            for carrier, p in hops:
                if carrier == entry:
                    continue
                self.traffic[(entry, carrier)] = self.traffic.get((entry, carrier), 0.0) + gb
                rel = self.relation(entry, carrier)
                usd = 0.0 if rel == "peer" else gb * float(p) * (1.0 if carrier == STATION else TRANSIT_MARKUP)
                f = st["forwarders"].setdefault(carrier, {"gb": 0.0, "usd": 0.0})
                f["gb"] += gb
                f["usd"] += usd
                f["relation"] = rel
                if usd:
                    self.transit[(entry, carrier)] = self.transit.get((entry, carrier), 0.0) + usd

    def view(self):
        owners = sorted({str(o) for o in set(self.owner.tolist())})
        pairs = [{"entry": a, "forwarder": b, "relation": self.relation(a, b),
                  "gb_24h": round(self.traffic.get((a, b), 0.0), 1), "transit_usd_24h": round(self.transit.get((a, b), 0.0), 2)}
                 for a in owners for b in owners + [STATION] if a != b and (a, b) in self.traffic]
        net = {o: self.revenue.get(o, 0.0) - sum(v for (a, _), v in self.transit.items() if a == o)
               + sum(v for (_, b), v in self.transit.items() if b == o) for o in owners}
        return {"fee": FEE, "peer_ratio": PEER_RATIO, "transit_markup": TRANSIT_MARKUP,
                "fee_usd_24h": round(self.fee, 2), "escrow_usd": round(self.held, 2),
                "refunded_usd_24h": round(self.refunded, 2),
                "revenue_usd_24h": {o: round(self.revenue.get(o, 0.0), 2) for o in owners},
                "net_usd_24h": {o: round(v, 2) for o, v in net.items()},
                "station_usd_24h": round(sum(v for (_, b), v in self.transit.items() if b == STATION), 2),
                "pairs": pairs}


def job_view(st):
    """A job's settlement for the API: the customer's one price and the inter-owner settlements behind it."""
    return {"entry": st["entry"], "price_usd": st["price"], "fee_usd": st["fee"],
            "legs": {k: {"usd": round(v["usd"], 4), "state": v["state"]} for k, v in st["legs"].items()},
            "forwarders": {k: {"gb": round(v["gb"], 3), "usd": round(v["usd"], 4), "relation": v["relation"]}
                           for k, v in st["forwarders"].items()}}
