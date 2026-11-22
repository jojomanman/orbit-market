"""Mesh routing over the time-varying graph. Pluggable: ROUTERS maps a name to a weight function.

Graph per tick: satellites 0..N-1, ground stations N..N+G-1, one sink N+G behind every ground station (so "to the
ground" is one shortest-path tree, not G of them). Edges carry three capacities that flows use up during a tick:
ISL (per link), satellite downlink terminal (per sat), ground station antennas (per station).
Weights come from the previous tick's load: link-state routing with periodic updates, as real meshes do it.
"""
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from . import physics as ph
from .orbit import C_KM_S

LAMBDA_USD_PER_GB_S = 0.1   # what a second of delay is worth per GB when choosing a route
CONGESTED = 1e3             # weight of a link at >= 98 % load: usable only if nothing else exists


def weight_price(net, pr, prev_load, gs_links):
    """Route cost per GB = sum of link prices + station price (LMP congestion term) + a small delay penalty, so data
    flows to the cheapest reachable ground station and hot links price themselves out."""
    isl_load, dl_load, gs_load = prev_load
    up = net.isl_idx
    d = net.isl_km_all[up]
    cap = net.isl_cap_all[up]
    w_isl = pr.isl[up] + LAMBDA_USD_PER_GB_S * ph.link_delay_s(d, cap, isl_load[up])
    w_isl = np.where(isl_load[up] >= 0.98 * cap, CONGESTED, w_isl)
    gsat, gst, gd = gs_links
    w_gs = pr.dl[gsat] + pr.gs[gst] + LAMBDA_USD_PER_GB_S * gd / C_KM_S
    w_gs = np.where((dl_load[gsat] >= 0.98 * net.dl_cap[gsat]) | (gs_load[gst] >= 0.98 * net.gs_cap[gst]),
                    CONGESTED, w_gs)
    return w_isl, w_gs


def weight_distance(net, pr, prev_load, gs_links):
    """Light time + processing only: the classic shortest path, blind to price and congestion (baseline)."""
    d = net.isl_km_all[net.isl_idx]
    return d / C_KM_S + ph.HOP_PROC_S, gs_links[2] / C_KM_S + ph.HOP_PROC_S


def weight_latency(net, pr, prev_load, gs_links):
    """Light time + processing + M/M/1 queueing at last tick's load, i.e. the delay a packet sent now would see; a
    link or station at >= 98 % load is usable only if nothing else exists. Congestion is priced in seconds, not
    dollars, so the path stays physically short and only bends around hot links."""
    isl_load, dl_load, gs_load = prev_load
    up = net.isl_idx
    cap = net.isl_cap_all[up]
    w_isl = np.where(isl_load[up] >= 0.98 * cap, CONGESTED, ph.link_delay_s(net.isl_km_all[up], cap, isl_load[up]))
    gsat, gst, gd = gs_links
    w_gs = np.where((dl_load[gsat] >= 0.98 * net.dl_cap[gsat]) | (gs_load[gst] >= 0.98 * net.gs_cap[gst]),
                    CONGESTED, gd / C_KM_S + ph.HOP_PROC_S)
    return w_isl, w_gs


ROUTERS = {"price": weight_price, "distance": weight_distance, "latency": weight_latency}


class Network:
    def __init__(self, con, geo, fleet, prev_load, weight_fn, prices):
        n, g = con.n_sats, len(con.gs_lat)
        self.n, self.g, self.sink = n, g, n + g
        up = geo.isl_up
        self.isl_idx = np.nonzero(up)[0]
        u, v = con.isl_u[up], con.isl_v[up]
        d = geo.isl_km[up]
        term = np.minimum(fleet.isl_gbps[u], fleet.isl_gbps[v])
        self.isl_cap_all = np.zeros(len(con.isl_u))
        self.isl_cap_all[up] = ph.isl_capacity_gbps(d, term)
        self.isl_km_all = geo.isl_km
        self.dl_cap = np.full(n, ph.DOWNLINK_GBPS)
        self.gs_cap = np.full(g, ph.GS_ANTENNAS * ph.DOWNLINK_GBPS)
        self.isl_res = self.isl_cap_all.copy()
        self.dl_res = self.dl_cap.copy()
        self.gs_res = self.gs_cap.copy()
        gl = geo.gs_links
        gsat, gst, gd = gl[:, 0].astype(int), gl[:, 1].astype(int), gl[:, 2]
        w_isl, w_gs = weight_fn(self, prices, prev_load, (gsat, gst, gd))
        self.edge_key = {}
        for k, (a, b) in enumerate(zip(u.tolist(), v.tolist())):
            self.edge_key[(a, b)] = self.edge_key[(b, a)] = self.isl_idx[k]
        # Reverse graph for the tree to the ground: sink -> station -> satellite, ISLs both ways.
        rows = np.concatenate([u, v, np.full(g, self.sink), n + gst])
        cols = np.concatenate([v, u, n + np.arange(g), gsat])
        w = np.concatenate([w_isl, w_isl, np.full(g, 1e-6), w_gs])
        size = n + g + 1
        self.ground_graph = csr_matrix((w, (rows, cols)), shape=(size, size))
        self.isl_graph = csr_matrix((np.concatenate([w_isl, w_isl]), (np.concatenate([u, v]), np.concatenate([v, u]))),
                                    shape=(size, size))
        self._ground_dist, self._ground_pred = dijkstra(self.ground_graph, indices=self.sink,
                                                        return_predecessors=True)
        self.ground_cost = self._tree_price(prices)

    def _tree_price(self, pr):
        """$/GB from every satellite to the ground along the routing tree, whatever the router optimised."""
        n = self.n
        cost = np.full(n + self.g + 1, np.inf)
        cost[self.sink] = 0.0
        for g in range(self.g):
            cost[n + g] = pr.gs[g]
        pred = self._ground_pred
        for node in np.argsort(self._ground_dist[:n]).tolist():
            if not np.isfinite(self._ground_dist[node]):
                break
            nxt = pred[node]
            hop = pr.dl[node] if nxt >= n else pr.isl[self.edge_key[(node, int(nxt))]]
            cost[node] = cost[nxt] + hop
        return cost[:n]

    def path_to_ground(self, sat):
        """[sat, ..., sat, station_node] or None. pred points from each node back toward the sink."""
        if not np.isfinite(self._ground_dist[sat]):
            return None
        path = [sat]
        node = sat
        while True:
            node = self._ground_pred[node]
            if node < 0 or node == self.sink:
                break
            path.append(int(node))
            if len(path) > 200:
                return None
        return path if path[-1] >= self.n else None

    def paths_between(self, pairs):
        """ISL-only shortest paths for (src, dst) satellite pairs, one Dijkstra per distinct source."""
        if not pairs:
            return []
        srcs = sorted({s for s, _ in pairs})
        dist, pred = dijkstra(self.isl_graph, indices=srcs, return_predecessors=True)
        row = {s: i for i, s in enumerate(srcs)}
        out = []
        for s, t in pairs:
            i = row[s]
            if not np.isfinite(dist[i, t]):
                out.append(None)
                continue
            path = [t]
            while path[-1] != s and len(path) < 200:
                path.append(int(pred[i, path[-1]]))
            out.append(path[::-1] if path[-1] == s else None)
        return out

    def path_to_any(self, src, targets, extra):
        """ISL path from src to whichever target minimises path weight + extra[target] (e.g. its ground leg), or None."""
        dist, pred = dijkstra(self.isl_graph, indices=src, return_predecessors=True)
        total = dist[targets] + extra
        k = int(np.argmin(total))
        if not np.isfinite(total[k]):
            return None
        path = [int(targets[k])]
        while path[-1] != src:
            path.append(int(pred[path[-1]]))
        return path[::-1]

    def exit_path(self, src, gsat, gst, ground, onward, slack=1.5):
        """ISL path from src ending at a station node: the ground edge k (gsat[k] -> gst[k], weight ground[k]) that
        leaves the mesh soonest among those whose total delay, mesh + ground + onward[k] (fibre to the customer), is
        within slack x the least. A 37-hop in-plane crawl to the station next to the customer is never what an
        operator does when a 2-hop downlink and fibre arrive a few ms later: the mesh is the priced, scarce good."""
        dist, pred = dijkstra(self.isl_graph, indices=src, return_predecessors=True)
        mesh = dist[gsat] + ground
        total = mesh + onward
        best = np.min(total) if len(total) else np.inf
        if not np.isfinite(best):
            return None
        k = int(np.argmin(np.where(total <= slack * best, mesh, np.inf)))
        path = [int(gsat[k])]
        while path[-1] != src:
            path.append(int(pred[path[-1]]))
        return path[::-1] + [self.n + int(gst[k])]

    def hops(self, path):
        """Yield ('isl', idx) / ('dl', sat) / ('gs', station) for every capacity a path uses."""
        for a, b in zip(path, path[1:]):
            if b >= self.n:
                yield "dl", a
                yield "gs", b - self.n
            else:
                yield "isl", self.edge_key[(a, b)]

    def residual(self, path):
        res = {"isl": self.isl_res, "dl": self.dl_res, "gs": self.gs_res}
        return min((res[k][i] for k, i in self.hops(path)), default=0.0)

    def take(self, path, gbps):
        res = {"isl": self.isl_res, "dl": self.dl_res, "gs": self.gs_res}
        for k, i in self.hops(path):
            res[k][i] -= gbps

    def bottleneck_util(self, path):
        res = {"isl": (self.isl_res, self.isl_cap_all), "dl": (self.dl_res, self.dl_cap), "gs": (self.gs_res, self.gs_cap)}
        return max((1 - res[k][0][i] / max(res[k][1][i], 1e-9) for k, i in self.hops(path)), default=0.0)

    def blame(self, path, gbps, unmet):
        """Unserved rate counts as unmet demand on the path's tightest good."""
        res = {"isl": self.isl_res, "dl": self.dl_res, "gs": self.gs_res}
        hops = list(self.hops(path))
        if hops:
            k, i = min(hops, key=lambda h: res[h[0]][h[1]])
            unmet[k][i] += gbps

    def latency_s(self, path, prev_load, quantile):
        isl_load = prev_load[0]
        total = 0.0
        for k, i in self.hops(path):
            if k == "isl":
                total += float(ph.link_delay_s(self.isl_km_all[i], max(self.isl_cap_all[i], 1e-6), isl_load[i],
                                               quantile))
        return total

    def loads(self):
        return (self.isl_cap_all - self.isl_res, self.dl_cap - self.dl_res, self.gs_cap - self.gs_res)
