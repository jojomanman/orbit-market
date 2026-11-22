# Orbit Market

An open compute market on a 10,000-satellite cluster. Satellites go in; out comes an API like cloud compute, except
that place and time are part of the product: a job asks for CPU, memory, storage and network (bits to move, a latency
bound as max or p50, a deadline from seconds to a day, where its data is), and the price of every resource is set by
the market per region and per tick.

Live: https://vps.taild101c7.ts.net:10000/orbit/

## Run

    python3 server.py --port 8510 --warmup-hours 1
    open http://127.0.0.1:8510/

Python 3.11+, numpy, scipy. The browser needs WebGL; three.js is vendored in `static/vendor` (MIT).

## What it shows

- Five pricing modes on the same orbits and the same jobs, switchable on the page and via `?mode=`:
  RFQ reverse auction (default: every satellite that can serve a job quotes from its own load curve, lowest wins,
  paid the second-lowest quote), posted exponential prices, per-tick batch auction, tatonnement, fixed price + FIFO.
- Demand from 100 named customers in five domains (`data/customers/`, each with sources and a budget basis).
- Scenarios: disaster at a clicked point, weather data sent raw vs shrunk onboard by AI.
- A price calculator with use-case presets and customer jobs: quote breakdown, the satellites' quotes, Simulate.

## API

| | |
|---|---|
| `POST /api/jobs` | submit a job ([spec/job-request.schema.json](spec/job-request.schema.json)); returns id and status |
| `GET /api/jobs/<id>` | waiting, running (satellite, price), done or missed |
| `GET /api/prices` | price per resource and region, units, 24 h history |
| `GET /api/state` | everything the page shows, once per tick |
| `POST /api/quote` | live quote for a job body: breakdown, satellite quotes, price if it waits |
| `GET /api/quote?lat=&lon=` | image of a point: price per urgency from real passes |
| `POST /api/scenario` | `{"event": {"lat", "lon", "hours"}}`, `{"event": null}`, `{"weather": "off" \| "raw" \| "ai"}` |
| `GET /api/customers` | customers, spend today, served and missed, budget basis |
| `GET /api/sats` | fleet by owner and shell |
| `GET /api/constellation` | orbital elements to propagate the fleet yourself |

    curl -X POST https://vps.taild101c7.ts.net:10000/orbit/api/jobs -d '{"cpu": 8, "memory_gb": 32,
      "duration_s": 600, "deadline_s": 3600, "location": {"lat": 59.3, "lon": 18.1},
      "network": {"gb": 2, "to": "ground"}, "max_price_usd": 50}'

Rate reservation: `{"rate": {"tokens_per_s": 500, "p95_ms": 50}, "duration_s": 3600, "location": {...},
"max_price_usd": 40}`. Emergency subscription: add `"priority": "reserve", "price_cap_usd": N`.

## Open spec

`spec/job-request.schema.json` (what a buyer asks for) and `spec/provider-offer.schema.json` (what an owner publishes:
satellites, live capacity, its load curve). Any owner that speaks the offer format can sell into the market; any buyer
that speaks the request format can buy from every owner at once.

## Test

    python3 -m unittest tests/test_sanity.py

## Model

Every formula, constant and source: [docs/MODEL.md](docs/MODEL.md). Mechanisms and literature:
[docs/MECHANISMS.md](docs/MECHANISMS.md). Routing and clearing are pluggable
(`sim/routing.py: ROUTERS`, `sim/market.py: MODES`).
