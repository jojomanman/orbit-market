# Model

Every formula and constant in the simulation, with its source or the assumption behind it. Code: `sim/`.
"Assumption" marks a number chosen for the demo with a stated reason; those are the first things to argue about.

## Time

One tick = 60 simulated seconds every 2 wall seconds (30x). Server warms up 1 simulated hour before serving.

## Orbits (`sim/orbit.py`)

| | |
|---|---|
| Shells | Walker delta. Altitude, inclination and plane count from the Starlink Gen1/Gen2 FCC filings: S1 550 km 53.0 deg 72x22, S2 540 km 53.2 deg 72x22, S3 570 km 70 deg 36x20, S4 560 km 97.6 deg 8x64, S5 525 km 53 deg 28x120, S6 530 km 43 deg 28x80 = 10,000. Assumption: the Mitsubishi Electric cluster has no public orbital plan (MOU 2026-11-06), so filed real-world geometry stands in. |
| Propagation | Circular orbits, mean motion n = sqrt(mu/a^3), J2 nodal precession dRAAN/dt = -1.5 n J2 (Re/a)^2 cos i. Error a few km per day, below anything the market sees. Browser runs the same model. |
| Earth rotation | GMST, IAU 1982 linear term. |
| Sun | Astronomical Almanac low-precision solar position (~0.01 deg). |
| Eclipse | Cylindrical shadow: s = r.sun < 0 and distance from the sun axis < Re. |
| Ground stations | 30 real teleport sites (KSAT, AWS Ground Station, SSC, gateways), 4 Ka antennas each, 25 deg elevation mask. |
| Owners | A, B, C, D by shell and plane parity. Hardware classes S/M/L below. |

## Satellite physics (`sim/physics.py`)

| | Formula / value | Source or assumption |
|---|---|---|
| Classes | S 16 units, 64 GB, 2 TB, 1.4 kW array, 3 kWh. M 32/128/4000/2.6 kW/5 kWh. L 64/256/8000/4.5 kW/9 kWh | Assumption: sized so a full compute load is roughly orbit-average power-neutral, as space data-centre concepts are power-limited |
| Compute power | 25 W per unit at full load, bus 300/500/800 W, 60 W per optical terminal (4) | Assumption: server-class accelerator share |
| Solar | P = array rating when sunlit, 0 in eclipse (sun-tracking arrays) | |
| Battery | dSoC = (P_gen - P_load) dt / E; floor 1 - DoD with DoD 35 % | LEO Li-ion practice 20-40 % DoD for >30k cycles |
| Power cap | sunlit: array + stored energy; eclipse: stored energy over a 35-min eclipse | |
| Thermal | C dT/dt = P_load - eps sigma A (T^4 - T_sink^4); eps 0.85, T_sink 260 K sun / 230 K eclipse; radiator sized for full load at 45 C plus 15 %; throttle linear from 40 C to 45 C | Lumped single-node model |
| Owner offer | reserve = markup x $0.45/unit-h x (1 + 25 W x energy shadow price / 0.45); shadow price $0.4/kWh / margin^2 where margin = SoC above floor / DoD | Eclipse and low battery make compute dearer at the source |

## Links

| | | |
|---|---|---|
| Topology | +Grid inside each shell: 2 intra-plane, 2 cross-plane. Cross-plane off above 70 deg latitude; no link across the counter-rotating seam of the polar shell; ray must clear 80 km altitude | Iridium/Starlink practice |
| ISL rate | C = C_term min(1, (1500 km / d)^2); C_term 10/20/40 Gbps by class | Diffraction-limited optical: received power falls as 1/d^2 |
| Downlink | 2.5 Gbps per satellite terminal, 4 x 2.5 Gbps per station | Ka-band gateway order of magnitude |
| Latency | sum of hops: d/c + 0.1 ms processing + M/M/1 sojourn; quantile q: -ln(1-q)/(mu - lambda), mu = C/12 kbit | M/M/1 sojourn is exponential |
| Capacity | Flows take min(need, residual on every hop). Utilization = served / capacity <= 1. Shortfall is unmet demand, booked on the path's tightest good | |

## Routing (`sim/routing.py`, pluggable `ROUTERS`)

- `price` (all market modes): edge weight per GB = link price + station price + $0.1 per GB-second of delay; links at >= 98 % load weigh 1000. One Dijkstra from a sink behind every station gives every satellite its cheapest route to the ground (LMP congestion term). Uses the previous tick's load, like link-state routing.
- `distance` (fixed baseline): light time + processing only.
- Open questions for the team: multipath splitting; inter-shell links; predicting the graph a few minutes ahead (it is deterministic); store-and-forward when no route exists.

## Pricing modes (`sim/market.py`, `MODES`; sources in docs/MECHANISMS.md)

| Mode | Rule |
|---|---|
| Posted (default) | Per good: p(u) = L for u <= c, (L/e)(U e/L)^u above, c = 1/(1 + ln(U/L)). L = 0.3 x anchor, U = 99th percentile of recent bid densities (value / anchor cost), clipped 3..200. Prices move within the tick after every sale. Served iff value >= price of the cheapest feasible option. Arrival order. |
| Batch auction | Per tick, rank by value / sqrt(bundle size); greedy if it fits; winner pays its critical value (density of the first job it displaced x sqrt(own size)), floor L x anchor. |
| Tatonnement | Per good: m <- clip(m + 0.15 (D - C)/C, L, 100), D = served + unmet. |
| Fixed + FIFO | Anchor prices, arrival order, nearest satellite, distance routing. |
| Priority reserve | Subscription flag on a job: served ahead of the spot queue, charged min(market price, cap). |

Anchor prices: compute $0.60/unit-h (an L satellite at ~$1.5M built and launched, 64 units, 5 years amortises to ~$0.5), memory $0.006/GB-h, storage $0.30/TB-h, ISL $0.002/GB per hop, downlink $0.50/GB (order of AWS Ground Station per-minute pricing). All assumptions.

## Demand

Default: 100 named customers in `data/customers/*.json` (five domains, researched with sources and a budget basis per org).
`sim/customers.py`: Poisson per job type at jobs_per_day x need multipliers (seasonal months parsed from the text; event
multipliers while a disaster runs, 70 % of their imaging then lands on the event); daily cycle by local time
1 + 0.6 cos(2 pi (h - 21)/24) (IXP traffic curves); elasticity: rate x price_index^-e; job size spread Pareto(2.5)
around the stated per-job size; stops for the day at annual budget / 365. Types above 3,000 jobs/day are merged
k-to-1 to keep one process fast. `--demand synthetic` switches to the earlier generic generator.

## Scenarios

| | |
|---|---|
| Disaster | Event at a point for 3 h: customers with an event need multiply demand; synthetic mode adds imaging 2/s, responder compute 1.5/s, damage downlink 0.25/s at 4x value. |
| Weather raw vs AI | S3 + S4 (1,232 sats) make 200 Mbit/s each of imager + hyperspectral sounder data. Raw: all of it goes down. AI: onboard principal-component compression and product extraction cut it 25x (EUMETSAT distributes IASI as PC scores at roughly this order; assumption), holding 4 compute units per satellite. Delivered within 30 min is worth $1.5/GB. Panel shows bandwidth and cost per hour of both at the current prices. |
| Image of a location | Real passes of S3 (radar, any light) and S4 (optical, sun > 10 deg) within 320 km (30 deg off-nadir). 16 units x 2 min onboard, 20 GB raw held, 0.5 GB product down. Options within 15 min priced at today's regional prices, later ones at the same region and hour yesterday (seasonal-naive forecast); cheapest option per urgency wins. |
| Presets | `static/presets.json`: per-job resources and values are order-of-magnitude assumptions for discussion, not quotes. |

## Settlement (`sim/settlement.py`, `GET api/settlement`, `settlement` in `GET api/jobs/<id>`)

The customer pays one price to the entry provider (owner of the satellite that takes the job), forwarding included,
like an ISP. 2 % market fee; the rest sits in escrow per leg: compute (the winning quote's non-transport share) is released when the
result exists, delivery when the bits land; a failed leg is refunded. Owners settle relaying among themselves:
a pair whose carried traffic is within 2:1 both ways over a 24 h decaying window peers settlement-free, otherwise
the entry owner pays the forwarder its posted link price per GB (transit). Ground stations are always paid per GB.
Assumed: fee, 2:1 ratio. Not yet: routing preference own > peer > transit (one shared tree per tick).

## Known gaps

Advance reservation only via the API (`start_in_s`); customer jobs still start now or re-bid every 3 min. Imaging sensor time is not a priced good. Inter-shell links absent. Ground-station antennas are pooled per station, not scheduled per pass.
