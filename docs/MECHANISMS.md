# Orbit market: how free markets for perishable, networked resources behave (2026-11-22)

For orbit-market-1122. Goods: compute, memory, storage, ISL bandwidth, downlink contact time, on 10,000 satellites,
several owners. Every good is indexed by (resource r, satellite or link s, time slot t) and is perishable: an unsold
CPU-second at t is gone at t+1.

Citations: written from my knowledge of the literature, not re-fetched in this run. Authors, year and venue are ones I
am confident of; items marked **[recall]** are details (exact constants, page numbers) I could not re-check in the time.
Links are DOI or publisher pages; where marked [recall] check before putting them on a slide.

Notation used throughout: p = price, D = demand, C = capacity, x = allocation, b = bid, v = value, u = utilization C_used/C.

---

## 1. Ten real markets

### 1.1 Electricity nodal pricing (LMP)
- **Formula.** The ISO solves economic dispatch: min Σ_i c_i(g_i) s.t. power balance, line limits |f_l| ≤ F_l, f = PTDF·(g − d).
  Price at node i is the dual of its balance constraint:
  `LMP_i = λ + λ·∂Loss/∂P_i + Σ_l PTDF_{l,i}·μ_l` (energy + loss + congestion; sign convention differs by ISO),
  μ_l ≥ 0 the shadow price of line l, nonzero only when l is at its limit.
- **Fit.** The closest analogue we have. Satellites = nodes, ISLs = lines, downlinks = interties to ground. A job's route
  price = sum of link shadow prices, exactly the congestion term. Loss term maps to energy per bit over a long ISL.
- **Drawback.** Needs a central solver with all bids/costs each interval (5 min in power, seconds for us); market power at
  constrained nodes (one owner controls a chokepoint satellite or ground station); price spikes need caps.
- Sources: Schweppe, Caramanis, Tabors, Bohn 1988, *Spot Pricing of Electricity*, Kluwer, doi:10.1007/978-1-4613-1683-1.
  Hogan 1992, "Contract networks for electric power transmission", J. Regulatory Economics 4:211-242, doi:10.1007/BF00133621.

### 1.2 AWS EC2 Spot
- **Formula.** 2009 to 2017: uniform-price sealed bids; with k spare instances the price is the k-th/(k+1)-th bid, users
  whose bid < price are interrupted. Ben-Yehuda et al. showed the published prices were consistent with a hidden random
  reserve price, not pure bids. Since Nov/Dec 2017: no bidding, AWS posts a price that "adjusts gradually based on
  long-term trends in supply and demand"; interruption with 2-minute notice. Exact formula not public **[unverified]**.
- **Fit.** Very good for the "spot tier": idle satellite capacity sold preemptibly, reclaimed when a priority job arrives
  (a satellite entering eclipse = capacity drop = interruption).
- **Drawback.** Preemption hurts long jobs (need checkpointing); opaque pricing loses the "open market" claim.
- Sources: Ben-Yehuda, Ben-Yehuda, Schuster, Tsafrir 2013, "Deconstructing Amazon EC2 Spot Instance Pricing", ACM Trans.
  Economics and Computation 1(3), doi:10.1145/2509413.2509416. AWS blog Dec 2017 "Amazon EC2 Spot Instances pricing
  model" (aws.amazon.com/blogs/compute/new-amazon-ec2-spot-pricing/) **[link recall]**.

### 1.3 Airline revenue management (EMSR, bid prices)
- **Formula.** Two classes (Littlewood 1972): protect y seats for full fare p_1, accept discount p_2 while
  `p_2 ≥ p_1·P(D_1 > y)`. EMSR-b generalises to n nested classes. Network bid prices: solve the deterministic LP
  `max Σ_j f_j x_j s.t. A x ≤ C, 0 ≤ x ≤ E[D]`, take duals π_i per leg, accept itinerary j iff `f_j ≥ Σ_{i∈j} π_i`.
- **Fit.** Excellent. Seats are perishable at departure exactly like CPU slots at t. A multi-hop job is a multi-leg
  itinerary; bid price control = "accept if value ≥ sum of slot/link prices along route and time". Forecast demand by
  region and time of day (diurnal).
- **Drawback.** Requires demand forecasts; bid prices from the LP are asymptotically optimal but myopic between
  re-solves; customers game by waiting for cheaper classes (strategic buyers).
- Sources: Littlewood 1972 (AGIFORS; reprinted J. Revenue & Pricing Mgmt 2005). Belobaba 1989, "Application of a
  probabilistic decision model to airline seat inventory control", Operations Research 37(2):183-197. Talluri & van Ryzin
  1998, "An analysis of bid-price controls for network revenue management", Management Science 44(11):1577-1593.

### 1.4 Spectrum auctions (combinatorial clock)
- **Formula.** Clock phase: auctioneer posts price per product, bidders state packages; for each product with excess
  demand `p_{t+1} = p_t·(1+ε)` (ε typically 5 to 10 %). Then a sealed supplementary round of package bids, winner
  determination by integer program, payments = core-selecting (VCG-nearest core point).
- **Fit.** Good for big reservations: "an orbit plane's compute for 6 h + 3 ground passes" as a package with
  complementarities (compute useless without downlink).
- **Drawback.** Slow (many rounds), winner determination NP-hard, complex for bidders. Not for second-scale jobs.
- Sources: Ausubel, Cramton, Milgrom 2006, "The clock-proxy auction", in Cramton, Shoham, Steinberg (eds.),
  *Combinatorial Auctions*, MIT Press. Day & Milgrom 2008, "Core-selecting package auctions", Int. J. Game Theory 36.
  Milgrom & Segal 2020, "Clock auctions and radio spectrum reallocation", J. Political Economy 128(1).

### 1.5 Ad auctions (GSP vs VCG)
- **Formula.** K slots with click rates α_1 ≥ … ≥ α_K, bidders ranked by b_i·q_i (q = quality).
  GSP: slot k pays per click `b_{k+1}·q_{k+1}/q_k`. VCG: slot k pays in total
  `Σ_{j=k+1}^{K+1} b_j·(α_{j-1} − α_j)` (harm imposed on those below). VCG is truthful; GSP is not, but its locally
  envy-free equilibrium gives VCG revenue.
- **Fit.** Good for one decision per tick per scarce slot: "which jobs get this ground station pass", slots ranked by
  quality (pass elevation = data rate).
- **Drawback.** GSP invites bid shading; VCG revenue can be low and is vulnerable to collusion/shill bids with
  complements (owner bidding against itself).
- Sources: Edelman, Ostrovsky, Schwarz 2007, "Internet advertising and the generalized second-price auction",
  American Economic Review 97(1):242-259. Varian 2007, "Position auctions", Int. J. Industrial Organization 25(6).
  Vickrey 1961 J. Finance 16(1); Clarke 1971 Public Choice 11; Groves 1973 Econometrica 41(4).

### 1.6 Ride-hailing surge
- **Formula.** Platform posts a multiplier m(z) on base fare with z = open requests / available drivers in a zone, piecewise
  increasing, capped. Uber's exact function is not public **[unverified]**. Theory: Banerjee, Johari, Riquelme model a
  queue per region; optimal state-dependent price ≈ threshold policy, and a well-chosen static price is near optimal in
  the large-market limit, dynamic pricing gives robustness to mis-estimated demand. Castillo, Knoepfle, Weyl: without
  surge, the "wild goose chase" (drivers sent far, utilisation collapses).
- **Fit.** Direct: satellites move, so supply in a region is time-varying and predictable; surge per ground cell =
  price on demand density over visible satellites. Wild goose chase = routing jobs over many hops to a far idle satellite
  burns ISL capacity: the same failure.
- **Drawback.** Price jumps feel unfair; zone boundaries create arbitrage; supply here cannot respond (satellites do not
  "drive" to demand), only demand can shift in time.
- Sources: Banerjee, Johari, Riquelme 2015, "Pricing in ride-sharing platforms: a queueing-theoretic approach", ACM EC.
  Castillo, Knoepfle, Weyl 2017, "Surge pricing solves the wild goose chase", ACM EC. Cachon, Daniels, Lobel 2017,
  M&SOM 19(3).

### 1.7 Continuous double auction (stock exchange)
- **Formula.** Limit order book; a trade occurs when best bid ≥ best ask, at the resting order's price. Gode & Sunder:
  even zero-intelligence traders (random bids subject only to budget) reach ~97 to 99 % allocative efficiency.
  Budish, Cramton, Shim propose frequent batch auctions: every Δt clear all orders at one uniform price where supply
  meets demand.
- **Fit.** Good for owner-to-owner trading of future slots ("I sell 10 CPU-min on sat 4411 at t+300 s") and for an open
  ecosystem story. Batch every tick suits a simulation.
- **Drawback.** One book per (resource, satellite, slot) = millions of thin books, no liquidity. Needs bundling into
  fungible products (e.g. "compute in region R in 5-min window") to be liquid.
- Sources: Smith 1962, "An experimental study of competitive market behavior", J. Political Economy 70(2).
  Gode & Sunder 1993, J. Political Economy 101(1):119-137. Budish, Cramton, Shim 2015, "The high-frequency trading arms
  race: frequent batch auctions as a market design response", Quarterly J. Economics 130(4).

### 1.8 Road congestion pricing (Pigou, Vickrey)
- **Formula.** Pigou toll = marginal external cost: `τ = q·t'(q)` for link travel time t(q). Vickrey bottleneck: a
  time-varying toll equal to the queueing delay cost each arrival would have suffered removes the queue entirely with the
  same throughput. Without tolls, selfish routing costs at most 4/3 of optimum for linear latencies.
- **Fit.** ISL queueing latency is the road delay; a job with a latency bound is a driver with high value of time.
  Marginal-cost link tolls make selfish source routing efficient.
- **Drawback.** Needs a correct delay model t(q); tolls are regressive (cheap jobs priced out); Braess paradox:
  adding links can raise selfish latency.
- Sources: Pigou 1920, *The Economics of Welfare*. Vickrey 1969, "Congestion theory and transport investment", AER
  59(2) P&P. Beckmann, McGuire, Winsten 1956, *Studies in the Economics of Transportation*. Roughgarden & Tardos 2002,
  "How bad is selfish routing?", J. ACM 49(2).

### 1.9 Kelly proportional fairness / network utility maximisation
- **Formula.** System: `max Σ_r U_r(x_r) s.t. Σ_{r∋l} x_r ≤ C_l`. User r pays w_r per unit time and receives
  `x_r = w_r / Σ_{l∈r} μ_l`; link price μ_l from load. Primal rate control
  `dx_r/dt = κ·(w_r − x_r·Σ_{l∈r} μ_l(y_l))`, y_l = Σ_{r∋l} x_r. Dual (Low & Lapsley):
  `μ_l(t+1) = [μ_l(t) + γ·(y_l − C_l)]^+`, each source picks `x_r = U_r'^{-1}(Σ_{l∈r} μ_l)`. Converges to the social
  optimum for strictly concave U. With price-anticipating users the efficiency loss is at most 25 %.
- **Fit.** The mathematical core of what we want: route price = sum of link shadow prices; fully decentralised (each
  satellite updates its own μ from its own load).
- **Drawback.** Assumes elastic, divisible traffic; deadline/latency-bounded, all-or-nothing jobs break concavity;
  convergence takes many iterations while the topology changes.
- Sources: Kelly 1997, "Charging and rate control for elastic traffic", Eur. Trans. Telecommunications 8(1). Kelly,
  Maulloo, Tan 1998, "Rate control for communication networks: shadow prices, proportional fairness and stability",
  J. Operational Research Society 49(3):237-252. Low & Lapsley 1999, "Optimization flow control I", IEEE/ACM Trans.
  Networking 7(6). Johari & Tsitsiklis 2004, "Efficiency loss in a network resource allocation game", Mathematics of
  Operations Research 29(3).

### 1.10 Tycoon / proportional share cloud market (+ TCP, Borg)
- **Formula.** Each host sells capacity C; user i bids b_i (money per unit time) on it and gets `x_i = b_i/Σ_j b_j · C`.
  Users spread budgets across hosts (best response has closed form). Price per unit = Σ_j b_j / C, emerges per host with
  no auctioneer.
- **Fit.** Trivially decentralised per satellite; each owner's satellite just divides its CPU by bids. Good "organic"
  baseline.
- **Drawback.** No guarantees: a job gets a share, not a deadline; share shrinks as others bid. Users must keep
  rebidding. Efficiency of Nash equilibrium ≥ 75 % (same game as Johari-Tsitsiklis).
- Related: TCP Reno/Vegas are distributed dual algorithms for NUM with specific utilities (Low 2003). Google Borg uses no
  prices: priority bands (production > batch > best-effort), preemption of lower bands, quota at admission; the model for
  our priority classes.
- Sources: Lai, Rasmusson, Adar, Zhang, Huberman 2005, "Tycoon: an implementation of a distributed, market-based
  resource allocation system", Multiagent and Grid Systems 1(3). Feldman, Lai, Zhang 2005, "A price-anticipating resource
  allocation mechanism for distributed shared clusters", ACM EC. Low 2003, "A duality model of TCP and queue management
  algorithms", IEEE/ACM ToN 11(4). Verma et al. 2015, "Large-scale cluster management at Google with Borg", EuroSys.

---

## 2. Candidate algorithms for our market

Common model. Goods: g = (r, s, t), r ∈ {cpu, mem, storage, isl, downlink, energy}, s = satellite or link, t = slot (e.g.
10 s). Capacity C_g from physics (power in sun/eclipse, link budget, contact windows). A job j has value v_j (its bid or
max price), demand vector over r, feasible set F_j of (satellite, start time, route) options defined by position/data
location, deadline d_j and latency bound L_j. Choosing option k uses goods G_{j,k}.
Job price for option k: `P_{j,k} = Σ_{g ∈ G_{j,k}} q_{j,g}·p_g` (q = quantity). For network: `Σ_{links l on route} bits·p_l`
= LMP congestion term. Job takes `k* = argmin_k P_{j,k}` and is served iff `v_j ≥ P_{j,k*}`.
This one rule handles all four special features:
- **Position**: options exist only for satellites visible from / routable to the data location; far options carry more
  link prices.
- **Perishability**: prices are per slot; unused slots expire, price at t resets toward floor.
- **Deadlines**: a long deadline means many time options, so it finds cheap slots (night side, after eclipse); a 2 s job
  has one or two options and pays the current scarcity price. Price difference by deadline emerges, not set by hand.
- **Mesh congestion**: link prices rise with load, routes avoid hot links (congestion-aware routing = shortest path with
  edge weight p_l + latency penalty).

### A. Online exponential posted prices (AAP / online knapsack threshold)  **[recommended first]**
- **Rule.** Each good has utilization u_g ∈ [0,1]. With value density per unit bounded in [L, U]:
  `p_g(u) = L` for `u ≤ c`, `p_g(u) = (L/e)·(U·e/L)^u` for `u > c`, with `c = 1/(1 + ln(U/L))`.
  At u = 1 the price is U, so nothing that cannot pay the maximum is admitted into the last unit.
  For links the same with u = load/capacity (Awerbuch-Azar-Plotkin routing uses `cost_l = μ^{u_l} − 1`, μ ~ 2n).
- **Optimality.** Competitive ratio `ln(U/L) + 1` vs offline optimum for online knapsack with small items
  (Zhou, Chakrabarty, Lukose 2008), and `O(log n)` throughput-competitive for online routing (AAP 1993). Best possible
  order for adversarial arrivals. Against stochastic arrivals it is pessimistic; a learned threshold does better.
- **Drawbacks.** Needs [L, U] (set from observed bid range); posted price is set by the platform formula, so price
  discovery is weak; with several owners, each could run its own curve (fine, that is competition). Fairness: low-value
  jobs are shut out at high load by design.
- **Cost.** O(options × route length) per job, no iteration: runs at 10k satellites in real time.
- Sources: Awerbuch, Azar, Plotkin 1993, "Throughput-competitive on-line routing", IEEE FOCS. Zhou, Chakrabarty, Lukose
  2008, "Budget constrained bidding in keyword auctions and online knapsack problems", WINE (LNCS 5385). Buchbinder &
  Naor 2009, "The design of competitive online algorithms via a primal-dual approach", Foundations and Trends in TCS 3(2).

### B. Dual decomposition / tatonnement prices (Kelly-Low, LMP-style)
- **Rule.** Every tick each satellite and link updates its own price from its own excess demand:
  `p_g(t+1) = max(p_floor, p_g(t) + γ·(D_g(t) − C_g(t)) / C_g(t))` (normalised), or multiplicative
  `p_g(t+1) = p_g(t)·exp(γ·(D_g − C_g)/C_g)`. Jobs choose k* by the common rule above; D_g = sum of chosen quantities.
- **Optimality.** For concave utilities and fixed capacities, converges to the welfare optimum (Low & Lapsley 1999);
  multiplicative tatonnement converges for Fisher markets beyond gross substitutes (Cheung, Cole, Devanur 2013).
  Arrow-Block-Hurwicz 1959: stable under gross substitutes; Scarf 1960: can cycle without it.
- **Drawbacks.** Converges over many ticks; satellites move every second so it chases a moving target; oscillation if γ
  large (herding onto the newly cheap satellite); indivisible jobs make it approximate only; owners can withhold capacity
  to lift their own price (as generators do at LMP nodes).
- **Cost.** O(goods) per tick, fully local. Very cheap.
- Sources: Low & Lapsley 1999 (above). Arrow, Block, Hurwicz 1959, Econometrica 27(1). Scarf 1960, Int. Economic Review
  1(3). Cheung, Cole, Devanur 2013, "Tatonnement beyond gross substitutes? Gradient descent to the rescue", STOC (journal
  version GEB 2020) **[recall: GEB year]**. Palomar & Chiang 2006, "A tutorial on decomposition methods for network
  utility maximization", IEEE JSAC 24(8).

### C. Frequent batch combinatorial auction, greedy (LOS)  **[recommended second]**
- **Rule.** Collect all jobs arriving in the tick. Each job is a bundle bid (b_j, S_j) on its cheapest feasible option.
  Sort by `b_j / sqrt(|S_j|)` (|S_j| = normalised bundle size, e.g. Σ q_g/C_g), accept greedily if still feasible.
  Payment of winner j = critical value: `|S_j|^{1/2} · b_i/|S_i|^{1/2}` where i is the first job after j in the order
  that is rejected only because j won (0 if none).
- **Optimality.** sqrt(m)-approximation of welfare (m = number of goods), truthful for single-minded bidders
  (Lehmann, O'Callaghan, Shoham 2002). In practice near-optimal when bundles are small relative to capacity.
- **Drawbacks.** Bids are needed (who sets them? simulated jobs have a value draw, fine); per-tick batching adds up to
  one tick of latency (bad for sub-second jobs: give them an instant posted-price lane); truthful only if each job
  wants one bundle; owner collusion and shill bids remain possible.
- **Cost.** O(n log n) per tick plus feasibility checks.
- Sources: Lehmann, O'Callaghan, Shoham 2002, "Truth revelation in approximately efficient combinatorial auctions",
  J. ACM 49(5). Budish, Cramton, Shim 2015 (batching). Nisan & Ronen 2001, "Algorithmic mechanism design", GEB 35.

### D. Proportional share per satellite (Tycoon)
- **Rule.** `x_i = b_i / Σ_j b_j · C_s` on satellite s; price `p_s = Σ_j b_j / C_s`. Jobs spread budget over satellites
  in their feasible set; finish time follows from rate x_i.
- **Optimality.** Nash efficiency ≥ 75 % (Johari-Tsitsiklis 2004); proportional fair if bids are price-taking.
- **Drawbacks.** No deadline or latency guarantee; burst arrivals dilute everyone; good as "organic, no auctioneer"
  contrast, weak for second-scale jobs.

### E. Priority classes + spot (Borg/EC2 hybrid)
- **Rule.** Three lanes: reserved (bought ahead, never preempted), on-demand (posted price, method A), spot (pays
  `p_spot = α·p_spot + (1−α)·p_clear`, smoothed clearing price, preempted when a higher lane needs the slot).
- **Optimality.** No welfare bound by itself; it is a product design that wraps A/B/C. Raises utilisation toward 100 %.
- **Drawbacks.** Preemption cost (checkpointing); reserved capacity idle when unused unless resold as spot.

### F. Contract-net / Bertsekas auction for job-to-satellite assignment
- **Rule.** Each unassigned job bids for its best satellite slot k: price rises by `bid increment = (best net value −
  second-best net value) + ε`; satellite awards to highest bidder, previous holder rebids.
- **Optimality.** Terminates within n·ε of the optimal assignment (Bertsekas 1988). Contract-net (Smith 1980) is the
  agent-protocol version used in multi-robot task allocation.
- **Drawbacks.** One-to-one assignment only; iterations grow with 1/ε; messaging over the mesh.
- Sources: Bertsekas 1988, "The auction algorithm: a distributed relaxation method for the assignment problem", Annals of
  Operations Research 14. Smith 1980, "The contract net protocol", IEEE Trans. Computers C-29(12). Gerkey & Mataric 2004,
  "A formal analysis and taxonomy of task allocation in multi-robot systems", IJRR 23(9).

### Drawbacks side by side (one line each)
- Gaming: A low (price taker), B medium (capacity withholding), C medium (shills), D high (rebidding), F low.
- Convergence latency: A none, B many ticks, C one tick, D continuous, F iterative.
- Fairness: A shuts out low value at peak; D proportional; C favours high density; E by class.
- Owner collusion: all price-based schemes are exposed at chokepoints (ground stations, polar crossings): cap prices or
  monitor offer withholding as power ISOs do.

---

## 3. Recommendation for the demo

**First: A (exponential posted prices per good) + route price = Σ link prices.** Streaming, no iteration, one formula,
provable bound, scales to 10k satellites, shows scarcity pricing and congestion immediately.
**Second, as comparison: C (per-tick batch greedy auction).** Real bids and price discovery vs posted price; same
feasibility code. Baseline both against fixed price + first-come-first-served. Optionally show B by replacing p_g(u) with
the tatonnement update; it is a one-line change.

Code it like this (per tick Δt, e.g. 10 s):

```
# goods: dict g=(res, sat_or_link, slot) -> {cap, used}
# L, U: min/max value per unit seen (rolling percentiles 1 and 99 of bids)
c = 1 / (1 + ln(U/L))
price(g):
    u = used[g] / cap[g]
    return L if u <= c else (L/e) * (U*e/L) ** u

job_price(job, opt) = sum(q * price(g) for g, q in opt.goods)
    # opt.goods: cpu/mem/storage on target sat for slots [start, start+dur),
    #            isl bits on every link of route (data source -> sat -> sink),
    #            downlink bits on the ground-station contact slot
options(job): sats reachable with latency(route) <= job.L and start+dur <= job.deadline
              route = shortest path, edge weight = price(link) * bits + lambda_lat * delay(link)

posted (A):  k = argmin job_price; accept iff job.value >= job_price(job,k); used += q; charge job_price
batch  (C):  for each job in tick: k_j = argmin job_price; size_j = sum(q/cap) over k_j
             order by value_j / sqrt(size_j); accept if all goods still fit
             pay_j = sqrt(size_j) * value_i / sqrt(size_i), i = first job rejected because j took its goods (else 0)
baseline:    fixed price per unit; accept in arrival order if fits
metrics:     welfare = sum(value of served), revenue, served %, deadline-miss %, p50/p99 latency,
             utilisation per resource, binding resource = argmax over resource of mean(u) at the top price decile
```

Perishability: slots in the past are dropped; capacity of future slots comes from physics (eclipse lowers cpu cap,
contact windows define downlink cap). Owners: each owner can carry its own L, U (or markup), and the job picks the
cheapest across owners, which is the open-ecosystem competition story.

## Unverified / to check before slides
- AWS post-2017 spot formula (not public). Uber surge function (not public).
- Exact link of the AWS blog; GEB year of Cheung-Cole-Devanur journal version; the threshold constant c in Zhou et al.
  (my recall of the published function; the ln(U/L)+1 ratio I am confident of).
- No satellite-specific market paper is cited here: I did not search for one in this run.
