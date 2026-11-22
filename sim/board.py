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

# Display names that do not shorten by rule (their acronym is not in the name, or the short form is a word).
SHORT = {"jma": "JMA", "imd": "IMD", "bayer-crop-science": "Bayer", "wri-global-forest-watch": "WRI",
         "us-nro": "US NRO", "us-nga": "US NGA", "us-sda": "US SDA", "us-coast-guard": "US Coast Guard",
         "se-forsvarsmakten": "Swedish Forces", "eu-emsa": "EMSA", "esa": "ESA", "uk-mod": "UK MOD",
         "fr-cde": "French Space Cmd", "de-bundeswehr": "Bundeswehr", "ua-mod": "Ukraine MOD",
         "au-defence": "Australia Defence", "ca-dnd": "Canada DND", "in-dsa": "India DSA", "il-imod": "Israel MOD",
         "copernicus-ems": "Copernicus EMS", "intl-charter-space-disasters": "Disasters Charter",
         "verisk-extreme-event": "Verisk", "descartes-underwriting": "Descartes", "fermat-capital": "Fermat Capital",
         "ursa-space": "Ursa Space", "sp-global-commodity-insights": "S&P Global", "maersk": "Maersk", "msc": "MSC",
         "aws": "AWS", "microsoft-azure-space": "Microsoft Azure", "here-technologies": "HERE",
         "viasat-inmarsat-maritime": "Inmarsat"}
_names = {}


def short_name(org):
    """The customer's name as it fits a card: the name itself, else its acronym in brackets, else SHORT."""
    if org["id"] not in _names:
        base = org["name"].split(" (")[0].split(",")[0]
        m = re.search(r"\(([A-Z][A-Za-z&]{1,6})\)", org["name"])
        acronym = m.group(1) if m and sum(ch.isupper() for ch in m.group(1)) >= 2 else None
        _names[org["id"]] = base if len(base) <= 16 else SHORT.get(org["id"]) or acronym or base
    return _names[org["id"]]


def build(engine, orgs, now, tick_s):
    """`now` is the start of the last tick: placements and results of that tick carry this time."""
    by_id = {o["id"]: o for o in orgs}
    cols = {s: [] for s in STAGES}

    # Newest first in every column (lower key shows first): the cards that moved last tick stay in view in their
    # new column, so the page can slide them there instead of fading them out.
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
        add(stage, j, -placed)
    for f in list(engine.flows):
        j = f["job"]
        placed = j.get("placed", {}).get("t", now)
        stage = "matched" if placed >= now else "downlink" if "down_t" in j and "dst" not in f else "relay"
        add(stage, j, -(j.get("down_t") or placed))
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


# Live overview colour classes, by what the customer bought.
TYPE = {"image_area": 0, "monitor_track": 0, "process_onboard": 1, "train_model": 1, "store": 1,
        "relay_data": 2, "downlink_bulk": 3}
MAX_TASKS, MAX_FLOWS, TICKER = 900, 400, 14


def flows(engine, orgs, now, tick_s, n_sats):
    """Everything in motion in one market, for the main page's Live mode (Jonas 15:11: "the live view of all the
    ongoing trades and data transfer ... a lot going on at once"). Compact arrays, built once per tick like build():
    tasks [entry sat, uplink node or -1, type, progress], flows [path, Gbps, type, progress], trades placed this tick
    [lat, lon, type, sat, uplink node or -1] and a ticker of the newest placements. Ground nodes are n_sats + station
    index, as in api/state routes."""
    by_id = {o["id"]: o for o in orgs}
    gs = lambda j: n_sats + j["uplink_gs"] if "uplink_gs" in j else -1
    typ = lambda j: TYPE.get(j.get("ctype"), 1 if j["kind"] == "batch" else 2 if j["kind"] == "relay" else 0)
    tasks = [[int(t["sat"]), gs(t["job"]), typ(t["job"]), round(1 - t["left"] / max(t["job"]["duration"], 1e-9), 3)]
             for t in list(engine.tasks)[-MAX_TASKS:]]
    moving = sorted((f for f in list(engine.flows) if f.get("path") and f.get("rate", 0) > 0), key=lambda f: -f["rate"])
    fl = [[[int(n) for n in f["path"]], round(f["rate"], 3), typ(f["job"]),
           round(1 - f["gb_left"] / max(f["job"]["out_gb"], 1e-9), 3)] for f in moving[:MAX_FLOWS]]
    placed = [j for j in [t["job"] for t in list(engine.tasks)] + [f["job"] for f in list(engine.flows)]
              if j.get("placed", {}).get("t", -1) >= now and j.get("customer") in by_id]
    placed.sort(key=lambda j: -j["arrival"])
    new = [[round(j["location"][0], 2), round(j["location"][1], 2), typ(j), int(j["placed"]["sat"]), gs(j)]
           for j in placed if j.get("location")]
    ticker = [{"id": f"{j['id']}-{j['arrival']:.0f}", "c": short_name(by_id[j["customer"]]),
               "k": j.get("ctype", j["kind"]).replace("_", " "), "y": typ(j), "usd": round(j["placed"]["price"], 2),
               "sat": int(j["placed"]["sat"]), "gs": gs(j), "path": j["placed"].get("path"),
               "loc": [round(x, 2) for x in j["location"]] if j.get("location") else None, "t": now}
              for j in placed[:TICKER]]
    cut = now - WINDOW_S
    paid = [u for t, u in list(engine.paid) if t >= cut]
    return {"t": now, "tasks": tasks, "flows": fl, "new": new, "ticker": ticker,
            "gbps": round(sum(f["rate"] for f in moving), 1), "running": len(engine.tasks),
            "per_min": {"trades": round(len(paid) * 60 / WINDOW_S, 1), "usd": round(sum(paid) * 60 / WINDOW_S, 2)}}
