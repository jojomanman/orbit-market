# Governance variants (network page, `/orbit/network`)

One request, one physical world, three ways to decide and pay. Only the decision step, its latency and the
payment differ, so the comparison row is fair. Everything lives in `static/network.html` (`VARIANTS`, `run()`).

## The request
Ship detection at the Port of Rotterdam, delivered within 15 min, at night under cloud, so it needs SAR.
Path after the decision is the same for all three: tasking up via Kiruna and the GEO relay, SAR capture, raw image
over ISL to a compute satellite, onboard detection, a 2 MB result down via the relay and Svalbard, fiber to the user.

| Physical assumption | Value | Why |
|---|---|---|
| Next SAR access over the port | B2 at T+45 s, then C2 at T+9:00 | Passes are discrete; a decision slower than the next pass waits for the one after |
| Capture / ISL raw transfer / onboard detection | 15 s / 4 s / 40 s | ~2 GB raw at a few Gbps optical ISL; small detector on a compute sat |
| Each radio hop / fiber leg | 0.3 s / 0.05 s | GEO round trip ~0.24 s; fiber negligible |
| Providers' summed reserve cost | $264 | Capture dominates; same cost whoever wins |

## Variants
| | Decision latency | Price | Source / assumption |
|---|---|---|---|
| Centralized | 1 s scheduler solve | cost x 1.40 | One owner with all state assigns the optimal pass (it catches T+45 s). No competing quote, so it prices with a margin; 40 % is an assumption. |
| Contract | 0.5 s RFQ + 3 s quote window + 0.5 s award/escrow | cost x 1.18, + 2 % fee | Owners quote on local info, lowest quote wins and is paid the 2nd-lowest (Vickrey 1961). Fee and per-leg escrow as in docs/MODEL.md. Like BGP plus peering agreements: no one sees the whole network, routes emerge from the shared rule. |
| Decentralized | 4 blocks: order, 2 bid blocks, match = 48 s; proof +1 block after delivery | cost x 1.15, + ~$2 gas | Ethereum slot time 12 s (ethereum.org, proof-of-stake docs). Gas: ~4 contract calls at ~$0.5 each, an order-of-magnitude assumption (L1 fees swing >10x; etherscan gas tracker). Stake + slashing replace trust in a counterparty. Open entry assumed to tighten bids slightly vs the contract. |

Result: centralized and contract deliver at 1:44; decentralized misses the 45 s pass and delivers at 9:59 (still
inside 15 min). Prices $370 / $318 / $306.

## Caveats
- The time gap comes entirely from the pass timing. With the next pass beyond ~1 min all three tie on time and differ
  only in price, trust and who can join. An L2 chain (~2 s blocks) would also catch the 45 s pass.
- Ethereum economic finality is ~2 epochs (~12.8 min); the page treats inclusion as enough to start work and pays out
  on the proof block. Waiting for finality before tasking would add ~13 min.
- Under load, contract routing on local info can be suboptimal and take time to settle, as BGP does after changes
  (Labovitz et al., "Delayed Internet Routing Convergence", SIGCOMM 2000: minutes). This calm single-request case
  does not show that.
- Margins (40 %, 18 %, 15 %) are assumptions to show the direction, not measurements.
