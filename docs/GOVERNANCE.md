# Governance variants (network page, `/orbit/network`)

One request, one physical world, three ways to decide and pay. Only the decision step, its latency and the
payment differ, so the comparison row is fair. Everything lives in `static/network.html` (`PRESETS`, `VARIANTS`, `run()`).

## The requests
Five presets on one network, each routed the cheapest feasible way at that moment (no fixed hub). Physical legs live
in `PRESETS`; the decision steps of each rule run before them, so a slow decision can miss a sensor pass.

| Preset | Path | Why this path |
|---|---|---|
| Ships (Rotterdam, 15 min, night: SAR) | tasking up at Kiruna, laser to SAR B1; 2 GB raw by laser to Compute A1; 2 MB result straight down at Kiruna | compute sat next to the SAR sat, station in view |
| Wildfire (Queensland, 10 min) | standing order on Optical B3, onboard detection; 40 kB alert over 3 laser hops, down at Perth | no station in view for 14 min; hops beat waiting and GEO |
| Weather (nowcast every 10 min) | order by fiber to Perth, up to Compute C1; 40 GB raw by laser from Weather C3; 200 MB product down | raw stays in orbit, only the product uses the downlink |
| Disaster (flood, phone coverage) | order up via GEO; phones reach Comms A2 direct to device; SOS traffic back via GEO and Kiruna | no station in view of Comms A2 for 9 min |
| Batch (400 GB SAR archive, 8 h) | 400 GB down at Kiruna over 3 passes, fiber to DC Oregon (night side), results back by fiber | night-side GPU ~$0.90/h vs ~$4/h in orbit; deadline allows the downlink |

| Physical assumption | Value | Why |
|---|---|---|
| Ships: next SAR access over the port | T+45 s, then T+9:00 | Passes are discrete; a decision slower than the next pass waits for the one after |
| Ships: capture / ISL raw / onboard detection | 15 s / 4 s / 40 s | ~2 GB raw at a few Gbps optical ISL; small detector on a compute sat |
| Providers' summed reserve cost | Ships $264, Wildfire $96, Weather $180, Disaster $140, Batch $410 | Illustrative, same whoever wins; other leg times in `PRESETS` are order-of-magnitude assumptions |
| Night-side vs orbital GPU price | $0.90 vs $4 per GPU-hour | Assumption for the Batch preset, not a quote |

## Variants
| | Decision latency | Price | Source / assumption |
|---|---|---|---|
| Centralized | 1 s scheduler solve | cost x 1.40 | One owner with all state assigns the optimal pass (it catches T+45 s). No competing quote, so it prices with a margin; 40 % is an assumption. |
| Contract | 0.5 s RFQ + 3 s quote window + 0.5 s award/escrow | cost x 1.18, + 2 % fee | Owners quote on local info, lowest quote wins and is paid the 2nd-lowest (Vickrey 1961). Fee and per-leg escrow as in docs/MODEL.md. Like BGP plus peering agreements: no one sees the whole network, routes emerge from the shared rule. |
| Decentralized | 4 blocks: order, 2 bid blocks, match = 48 s; proof +1 block after delivery | cost x 1.15, + ~$2 gas | Ethereum slot time 12 s (ethereum.org, proof-of-stake docs). Gas: ~4 contract calls at ~$0.5 each, an order-of-magnitude assumption (L1 fees swing >10x; etherscan gas tracker). Stake + slashing replace trust in a counterparty. Open entry assumed to tighten bids slightly vs the contract. |

Ships result: centralized and contract deliver at 1:45; decentralized misses the 45 s pass and delivers at 10:00
(inside 15 min). Prices $370 / $318 / $306. The other presets have no pass to miss: the rules differ by decision
latency (1 s, 3.5 s, 48 s) and price only.

## Caveats
- The time gap comes entirely from the pass timing. With the next pass beyond ~1 min all three tie on time and differ
  only in price, trust and who can join. An L2 chain (~2 s blocks) would also catch the 45 s pass.
- Ethereum economic finality is ~2 epochs (~12.8 min); the page treats inclusion as enough to start work and pays out
  on the proof block. Waiting for finality before tasking would add ~13 min.
- Under load, contract routing on local info can be suboptimal and take time to settle, as BGP does after changes
  (Labovitz et al., "Delayed Internet Routing Convergence", SIGCOMM 2000: minutes). This calm single-request case
  does not show that.
- Margins (40 %, 18 %, 15 %) are assumptions to show the direction, not measurements.
