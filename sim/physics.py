"""Satellite hardware, power, thermal and link physics. Every number is either a physical law or a named assumption
listed in docs/MODEL.md; change it there and here together.
"""
import math

import numpy as np

from .orbit import C_KM_S

SOLAR_CONSTANT = 1361.0   # W/m^2 (only the ratio matters: arrays are rated at 1 AU, sun-tracking)
SIGMA = 5.670374e-8       # Stefan-Boltzmann

# Hardware classes. "unit" = one compute unit (vCPU + its share of accelerator) drawing UNIT_W at full load.
CLASSES = {
    #      units  mem GB  storage GB  array W  battery Wh  bus W  ISL Gbps  thermal mass J/K
    "S": dict(units=16, mem=64, storage=2000, array=1400, battery=3000, bus=300, isl=10, heat_cap=80e3),
    "M": dict(units=32, mem=128, storage=4000, array=2600, battery=5000, bus=500, isl=20, heat_cap=150e3),
    "L": dict(units=64, mem=256, storage=8000, array=4500, battery=9000, bus=800, isl=40, heat_cap=300e3),
}
UNIT_W = 25.0             # W per compute unit at full load (server-class accelerator share)
ISL_TERMINAL_W = 60.0     # W per active optical terminal
DOD_LIMIT = 0.35          # usable depth of discharge for >30k LEO cycles (Li-ion practice 20-40 %)
T_MAX = 318.15            # K, electronics limit (45 C)
T_THROTTLE = 313.15       # K, start throttling 5 K below the limit
T_SINK_SUN = 260.0        # K, effective radiator sink with Earth IR + albedo
T_SINK_ECLIPSE = 230.0
EMISSIVITY = 0.85
ISL_D_REF_KM = 1500.0     # range at which an optical ISL closes its full rate (power-limited beyond)
DOWNLINK_GBPS = 2.5       # per Ka antenna / per satellite downlink terminal
GS_ANTENNAS = 4
PKT_BITS = 12000          # 1500-byte packets for queueing delay
HOP_PROC_S = 1e-4         # per-hop switching/processing


class Fleet:
    """Per-satellite capacities; one instance per market engine holds that engine's battery and temperature."""

    def __init__(self, con):
        cls = con.cls
        col = lambda k: np.array([CLASSES[c][k] for c in cls], float)
        self.units, self.mem, self.storage = col("units"), col("mem"), col("storage")
        self.array_w, self.battery_wh, self.bus_w = col("array"), col("battery"), col("bus")
        self.isl_gbps, self.heat_cap = col("isl"), col("heat_cap")
        full = self.bus_w + self.units * UNIT_W + 4 * ISL_TERMINAL_W
        # Radiator sized for full load at T_MAX with the sunlit sink plus 15 % margin: steady full load sits just under
        # the throttle point, so thermal binds only with hot transients.
        self.rad_area = 1.15 * full / (EMISSIVITY * SIGMA * (T_MAX ** 4 - T_SINK_SUN ** 4))
        self.soc = np.full(con.n_sats, 0.9)
        self.temp = np.full(con.n_sats, 300.0)

    def step(self, sunlit, units_used, dt):
        """Advance battery and temperature by dt seconds under the given load; return usable compute units."""
        load_w = self.bus_w + units_used * UNIT_W + 4 * ISL_TERMINAL_W
        gen_w = np.where(sunlit, self.array_w, 0.0)
        floor = 1 - DOD_LIMIT
        self.soc = np.clip(self.soc + (gen_w - load_w) * dt / 3600 / self.battery_wh, floor, 1.0)
        sink = np.where(sunlit, T_SINK_SUN, T_SINK_ECLIPSE)
        q = load_w - EMISSIVITY * SIGMA * self.rad_area * (self.temp ** 4 - sink ** 4)
        self.temp = self.temp + q * dt / self.heat_cap
        # Power cap: in sun the array (plus battery above floor), in eclipse the stored energy spread over a
        # typical 35-min LEO eclipse.
        stored_w = (self.soc - floor) * self.battery_wh * 3600 / 2100
        avail_w = np.where(sunlit, self.array_w + stored_w, stored_w)
        power_units = np.clip((avail_w - self.bus_w - 4 * ISL_TERMINAL_W) / UNIT_W, 0, self.units)
        thermal_frac = np.clip((T_MAX - self.temp) / (T_MAX - T_THROTTLE), 0, 1)
        return np.minimum(power_units, self.units * thermal_frac), power_units, self.units * thermal_frac

    def energy_shadow_price(self, sunlit):
        """Marginal value of one Wh to the owner, $/Wh: near zero with a full battery in sun, steep near the floor."""
        floor = 1 - DOD_LIMIT
        margin = np.clip((self.soc - floor) / DOD_LIMIT, 0.02, 1)
        base = 0.4e-3   # $/Wh = $0.40/kWh, the cost of the solar Wh amortised over array life (assumption)
        return np.where(sunlit & (self.soc > 0.97), 0.1 * base, base / margin ** 2)


def isl_capacity_gbps(d_km, terminal_gbps):
    """Optical ISL rate: received power falls as 1/d^2 (diffraction-limited beam); full rate up to ISL_D_REF_KM."""
    return terminal_gbps * np.minimum(1.0, (ISL_D_REF_KM / np.maximum(d_km, 1.0)) ** 2)


def link_delay_s(d_km, cap_gbps, load_gbps, quantile=None):
    """One hop: light time + processing + M/M/1 sojourn. quantile=None -> mean, 0.5 -> p50, 0.99 -> p99.

    M/M/1 sojourn time is exponential with rate (mu - lambda), so its q-quantile is -ln(1-q)/(mu - lambda)."""
    mu = cap_gbps * 1e9 / PKT_BITS
    lam = np.minimum(load_gbps, 0.999 * cap_gbps) * 1e9 / PKT_BITS
    scale = 1.0 if quantile is None else -math.log(1 - quantile)
    return d_km / C_KM_S + HOP_PROC_S + scale / np.maximum(mu - lam, 1e-3)
