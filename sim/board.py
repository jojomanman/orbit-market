"""Live board: every customer job of one market as a card in the lifecycle column it is in right now.

Jonas 2026-11-22 14:34: a fast-updating kanban of jobs coming in and completing. The page polls this every few
hundred ms, so it returns ids, stage and a few labels only, capped per column, and is built at most once per sim
tick, between ticks (Sim.board_view).

Stages follow the engine's real order: a job waits in `pending` (Incoming on arrival, Quoted once it has been
priced and is still waiting for a bid it can afford or for capacity), is placed (Matched in its first tick), its
input goes up to the entry satellite (Uplink, estimated from input size at link rate: the sim does not model the
upload), it runs in `tasks` (Compute), then its result sits in `flows` (Relay until bits start moving on the mesh,
Downlink once they do). Point-to-point transfers skip compute and are Relay while they flow. Arrival to
placement mostly happens inside one tick, so Incoming is usually empty; the page walks a card through every
stage it passed between two ticks, using `o` (has a result to send down) and `p` (point-to-point transfer).
"""

import re

from sim import physics as ph

STAGES = ["incoming", "quoted", "matched", "uplink", "compute", "relay", "downlink", "delivered", "rejected"]
CAP = 12
WINDOW_S = 600   # sim seconds the rates average over

_names = {}


def short_name(org):
    if org["id"] not in _names:
        m = re.search(r"\(([A-Z][A-Z0-9&-]{1,11})\)", org["name"])   # an acronym, not "(with GUR)"
        name = m.group(1) if m else org["name"] if len(org["name"]) <= 18 else org["id"].replace("_", " ").upper()
        _names[org["id"]] = name
    return _names[org["id"]]


def build(engine, orgs, now, tick_s):
    """`now` is the start of the last tick: placements and results of that tick carry this time."""
    by_id = {o["id"]: o for o in orgs}
    cols = {s: [] for s in STAGES}

    # Lower key shows first: in queues and outcomes the newest, in the work stages the job closest to moving on, so
    # the visible cards are the ones about to slide right rather than a list that churns every tick.
    def add(stage, j, key):
        org = by_id.get(j.get("customer"))
        if org is None:
            return
        cols[stage].append((key, j, org))

    for j in list(engine.pending):
        add("quoted" if "why" in j else "incoming", j, -j["arrival"])
    for t in list(engine.tasks):
        j = t["job"]
        placed = j["placed"]["t"]
        up_s = max(j["storage_gb"] - j["out_gb"], 0) * 8 / ph.DOWNLINK_GBPS
        stage = "matched" if placed >= now else "uplink" if now - placed < up_s else "compute"
        add(stage, j, -placed if stage == "matched" else placed + up_s - now if stage == "uplink" else t["left"])
    for f in list(engine.flows):
        j = f["job"]
        placed = j.get("placed", {}).get("t", now)
        stage = "matched" if placed >= now else "downlink" if "down_t" in j and "dst" not in f else "relay"
        add(stage, j, -placed if stage == "matched" else f["gb_left"])
    for j in list(engine.finished)[-200:]:
        r = j["result"]
        if r["t"] >= now - 3 * tick_s:
            add("delivered" if r["on_time"] else "rejected", j, -r["t"])

    counts, cards, more = {}, [], {}
    for s, items in cols.items():
        items.sort(key=lambda x: x[0])
        counts[s] = len(items)
        more[s] = max(len(items) - CAP, 0)
        for _, j, org in items[:CAP]:
            p = j.get("placed") or {}
            cards.append({"id": f"{j['id']}-{j['arrival']:.0f}", "s": s, "c": short_name(org),
                          "k": j.get("ctype", j["kind"]).replace("_", " "),
                          "usd": round(p["price"] if p.get("price") is not None else j["value"], 2),
                          "dl": round(j["deadline"] - now), "why": j["result"]["why"] if "result" in j else None,
                          "o": int(j["out_gb"] > 0.01), "p": int(j["kind"] == "relay")})

    cut = now - WINDOW_S
    done = sum(1 for t, _, ok, _, _ in list(engine.done) if t >= cut and ok)
    usd = sum(u for t, u in list(engine.paid) if t >= cut)
    return {"t": now, "counts": counts, "more": more, "cards": cards,
            "per_min": {"jobs": round(done * 60 / WINDOW_S, 1), "usd": round(usd * 60 / WINDOW_S, 2)}}
