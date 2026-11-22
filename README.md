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

## API

| | |
|---|---|
| `POST /api/jobs` | submit a job ([spec/job-request.schema.json](spec/job-request.schema.json)); returns id and status |
| `GET /api/jobs/<id>` | waiting, running (satellite, price), done or missed |
| `GET /api/prices` | price per resource and region, units, 24 h history |
| `GET /api/state` | everything the page shows, once per tick |
| `GET /api/sats` | fleet by owner and shell |
| `GET /api/constellation` | orbital elements to propagate the fleet yourself |

    curl -X POST https://vps.taild101c7.ts.net:10000/orbit/api/jobs -d '{"cpu": 8, "memory_gb": 32,
      "duration_s": 600, "deadline_s": 3600, "location": {"lat": 59.3, "lon": 18.1},
      "network": {"gb": 2, "to": "ground"}, "max_price_usd": 50}'

## Model

Every formula, constant and source: [docs/MODEL.md](docs/MODEL.md). Routing and clearing are pluggable
(`sim/routing.py: ROUTERS`, `sim/market.py: CLEARING`); the page compares the market with fixed prices and
first-come-first-served on the same orbits and the same jobs.
