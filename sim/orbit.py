"""Orbits, sun, eclipse, ground stations and regions for a 10,000-satellite cluster.

There is no public orbital plan for the Mitsubishi Electric mega-constellation (MOU 2026-11-06, no filings yet), so
the cluster reuses real filed geometry: Walker-delta shells with the altitudes, inclinations and plane counts of the
Starlink Gen1/Gen2 FCC filings, trimmed to 10,000 satellites. Circular orbits with J2 nodal precession; that is
accurate to a few km over a day, which is far below anything the market can notice. The browser runs the same
propagation (static/app.js) from GET /api/constellation, so the server never ships 10k positions per frame.
"""
import math

import numpy as np

MU = 398600.4418          # km^3/s^2, WGS-84
RE = 6378.137             # km
J2 = 1.08262668e-3
OMEGA_E = 7.2921159e-5    # rad/s, Earth rotation
C_KM_S = 299792.458

# name, altitude km, inclination deg, planes, sats per plane, Walker phasing F, owner per plane parity, hardware class
SHELLS = [
    ("S1", 550, 53.0, 72, 22, 17, ("A", "A"), "M"),
    ("S2", 540, 53.2, 72, 22, 17, ("B", "B"), "M"),
    ("S3", 570, 70.0, 36, 20, 11, ("C", "C"), "S"),
    ("S4", 560, 97.6, 8, 64, 1, ("D", "D"), "S"),
    ("S5", 525, 53.0, 28, 120, 1, ("A", "B"), "L"),
    ("S6", 530, 43.0, 28, 80, 1, ("C", "D"), "L"),
]

# Real teleport / ground-segment sites (KSAT, AWS Ground Station, SSC and gateway locations), 4 Ka antennas each.
GROUND_STATIONS = [
    ("Svalbard", 78.23, 15.41), ("Troll", -72.01, 2.53), ("Kiruna", 67.86, 20.96), ("Fairbanks", 64.86, -147.85),
    ("Inuvik", 68.32, -133.55), ("Punta Arenas", -52.94, -70.85), ("Hartebeesthoek", -25.89, 27.69),
    ("Awarua", -46.53, 168.38), ("Hawaii", 19.82, -155.47), ("Singapore", 1.35, 103.82), ("Dubai", 25.20, 55.27),
    ("Bahrain", 26.07, 50.56), ("Mumbai", 19.08, 72.88), ("Tokyo", 35.68, 139.69), ("Seoul", 37.57, 126.98),
    ("Sydney", -33.87, 151.21), ("Perth", -31.95, 115.86), ("Ohio", 40.0, -83.0), ("Oregon", 45.60, -121.18),
    ("Virginia", 38.95, -77.45), ("Sao Paulo", -23.55, -46.63), ("Santiago", -33.45, -70.67),
    ("Ireland", 53.35, -6.26), ("Stockholm", 59.33, 18.07), ("Frankfurt", 50.11, 8.68), ("Cape Town", -33.92, 18.42),
    ("Mauritius", -20.16, 57.50), ("Guam", 13.44, 144.79), ("Azores", 37.74, -25.67), ("Bangalore", 12.97, 77.59),
]
GS_MIN_ELEVATION_DEG = 25.0   # typical Ka-band gateway mask

REGIONS = ["N America", "S America", "Europe", "Africa", "Middle East", "S Asia", "E Asia", "N Asia", "Oceania",
           "N Atlantic", "S Atlantic", "Indian Ocean", "N Pacific", "S Pacific", "Polar"]


def region_of(lat, lon):
    """Vectorised lat/lon (deg) -> index into REGIONS. Coarse boxes; first match wins, oceans are the fallback."""
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    r = np.full(lat.shape, -1, int)

    def put(idx, mask):
        r[(r < 0) & mask] = idx

    put(14, np.abs(lat) > 66)
    put(2, (lat > 35) & (lat <= 66) & (lon > -12) & (lon < 40))
    put(4, (lat > 12) & (lat < 42) & (lon >= 35) & (lon < 62))
    put(3, (lat > -35) & (lat < 37) & (lon > -18) & (lon < 52))
    put(5, (lat > 5) & (lat < 35) & (lon >= 62) & (lon < 92))
    put(7, (lat >= 42) & (lat <= 66) & (lon >= 40) & (lon < 180))
    put(6, (((lat > 18) & (lat < 55)) | ((lat > -10) & (lat <= 18))) & (lon >= 92) & (lon < 146))
    put(8, (lat > -50) & (lat < -10) & (lon >= 110))
    put(0, (lat > 15) & (lat <= 66) & (lon > -170) & (lon < -52))
    put(1, (lat > -56) & (lat <= 15) & (lon > -82) & (lon < -34))
    atlantic = (lon > -70) & (lon < 20)
    put(9, atlantic & (lat >= 0))
    put(10, atlantic & (lat < 0))
    put(11, (lon >= 20) & (lon < 120) & (lat < 5))
    put(12, lat >= 0)
    put(13, lat < 0)
    return r


def julian_date(unix_s):
    return unix_s / 86400.0 + 2440587.5


def gmst(unix_s):
    """Greenwich mean sidereal time, rad (IAU 1982 linear term; error < 1 s over decades)."""
    d = julian_date(unix_s) - 2451545.0
    return math.radians((280.46061837 + 360.98564736629 * d) % 360.0)


def sun_vector(unix_s):
    """Unit vector to the Sun in ECI (Astronomical Almanac low-precision formula, ~0.01 deg)."""
    n = julian_date(unix_s) - 2451545.0
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians((280.460 + 0.9856474 * n) % 360 + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    return np.array([math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam)])


class Constellation:
    """Static elements of every satellite plus the +Grid inter-satellite link topology."""

    def __init__(self, epoch_unix):
        self.epoch = epoch_unix
        cols = {k: [] for k in ("shell", "plane", "slot", "a", "inc", "raan0", "u0", "owner", "cls")}
        self.shells = []
        isl = []
        base = 0
        for si, (name, alt, inc, planes, per, f, owners, cls) in enumerate(SHELLS):
            a = RE + alt
            n = math.sqrt(MU / a ** 3)
            raan_dot = -1.5 * n * J2 * (RE / a) ** 2 * math.cos(math.radians(inc))
            self.shells.append(dict(name=name, alt_km=alt, inc_deg=inc, planes=planes, per_plane=per, phasing=f,
                                    cls=cls, n=n, raan_dot=raan_dot, first=base))
            for p in range(planes):
                for s in range(per):
                    cols["shell"].append(si)
                    cols["plane"].append(p)
                    cols["slot"].append(s)
                    cols["a"].append(a)
                    cols["inc"].append(math.radians(inc))
                    cols["raan0"].append(2 * math.pi * p / planes)
                    cols["u0"].append(2 * math.pi * s / per + 2 * math.pi * f * p / (planes * per))
                    cols["owner"].append(owners[p % 2])
                    cols["cls"].append(cls)
                    i = base + p * per + s
                    isl.append((i, base + p * per + (s + 1) % per, 0))
                    # Polar shells counter-rotate across the seam; no cross-plane link there.
                    if p + 1 < planes or inc < 80:
                        isl.append((i, base + ((p + 1) % planes) * per + s, 1))
            base += planes * per
        for k, v in cols.items():
            setattr(self, k, np.array(v))
        self.n_sats = base
        self.shell_n = np.array([s["n"] for s in self.shells])[self.shell]
        self.shell_raan_dot = np.array([s["raan_dot"] for s in self.shells])[self.shell]
        isl = np.array(isl)
        self.isl_u, self.isl_v, self.isl_cross = isl[:, 0], isl[:, 1], isl[:, 2].astype(bool)
        gs = np.array([(lat, lon) for _, lat, lon in GROUND_STATIONS])
        self.gs_lat, self.gs_lon = gs[:, 0], gs[:, 1]
        la, lo = np.radians(self.gs_lat), np.radians(self.gs_lon)
        self.gs_ecef = RE * np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], 1)
        self.gs_region = region_of(self.gs_lat, self.gs_lon)

    def eci(self, unix_s):
        t = unix_s - self.epoch
        raan = self.raan0 + self.shell_raan_dot * t
        u = self.u0 + self.shell_n * t
        cu, su, cr, sr, ci, si = np.cos(u), np.sin(u), np.cos(raan), np.sin(raan), np.cos(self.inc), np.sin(self.inc)
        return self.a[:, None] * np.stack([cr * cu - sr * su * ci, sr * cu + cr * su * ci, su * si], 1)

    def static_json(self):
        """What the browser needs to propagate the same orbits itself."""
        return {
            "epoch": self.epoch,
            "shells": [{k: s[k] for k in ("name", "alt_km", "inc_deg", "planes", "per_plane", "phasing", "cls",
                                          "n", "raan_dot", "first")} for s in self.shells],
            "owners": "".join(self.owner.tolist()),
            "ground_stations": [{"name": n, "lat": la, "lon": lo} for n, la, lo in GROUND_STATIONS],
            "regions": REGIONS,
        }


class Geometry:
    """Everything position-dependent for one instant: shared by every market engine in that tick."""

    def __init__(self, con, unix_s):
        self.t = unix_s
        r = con.eci(unix_s)
        self.r = r
        sun = sun_vector(unix_s)
        self.sun = sun
        # Cylindrical Earth shadow: behind the terminator plane and inside the Earth's radius from the sun axis.
        s = r @ sun
        perp = np.linalg.norm(r - s[:, None] * sun[None, :], axis=1)
        self.sunlit = ~((s < 0) & (perp < RE))
        th = gmst(unix_s)
        c, sn = math.cos(th), math.sin(th)
        ecef = np.stack([c * r[:, 0] + sn * r[:, 1], -sn * r[:, 0] + c * r[:, 1], r[:, 2]], 1)
        self.ecef = ecef
        rn = np.linalg.norm(ecef, axis=1)
        self.lat = np.degrees(np.arcsin(ecef[:, 2] / rn))
        self.lon = np.degrees(np.arctan2(ecef[:, 1], ecef[:, 0]))
        self.unit = ecef / rn[:, None]
        self.region = region_of(self.lat, self.lon)
        # Inter-satellite links: length, and whether the pointing geometry allows them right now.
        d = r[con.isl_u] - r[con.isl_v]
        self.isl_km = np.linalg.norm(d, axis=1)
        mid_lat = np.abs(self.lat[con.isl_u])
        # Cross-plane optical terminals lose lock at high latitude (relative azimuth rate too high, Iridium/Starlink
        # practice ~70 deg). The ray must also clear 80 km of atmosphere.
        grazing = self._ray_min_alt(r[con.isl_u], r[con.isl_v])
        self.isl_up = (~con.isl_cross | (mid_lat < 70)) & (grazing > RE + 80)
        # Ground contacts: elevation of each sat over each ground station.
        vis = []
        for g in range(len(con.gs_ecef)):
            gv = con.gs_ecef[g]
            dv = ecef - gv
            dist = np.linalg.norm(dv, axis=1)
            sin_el = (dv @ (gv / RE)) / dist
            idx = np.nonzero(sin_el > math.sin(math.radians(GS_MIN_ELEVATION_DEG)))[0]
            for i in idx:
                vis.append((i, g, dist[i]))
        self.gs_links = np.array(vis) if vis else np.zeros((0, 3))

    @staticmethod
    def _ray_min_alt(a, b):
        d = b - a
        t = np.clip(-(a * d).sum(1) / np.maximum((d * d).sum(1), 1e-9), 0, 1)
        return np.linalg.norm(a + t[:, None] * d, axis=1)

    def within_km(self, lat, lon, radius_km):
        """Indices of satellites whose ground point is within radius_km (great circle) of lat/lon."""
        la, lo = math.radians(lat), math.radians(lon)
        p = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        return np.nonzero(self.unit @ p > math.cos(radius_km / RE))[0]


def passes(con, sats, t0, lat, lon, horizon_s, reach_km, step_s=30):
    """(time, satellite) of the first sample of every pass of `sats` within reach_km of a ground point, by time.

    Brute force over a time grid: 24 h x 1,200 imaging satellites is ~3.5M positions, well under a second."""
    times = t0 + np.arange(0, horizon_s, step_s)
    la, lo = math.radians(lat), math.radians(lon)
    out = []
    cos_reach = math.cos(reach_km / RE)
    a, inc, raan0, u0 = con.a[sats], con.inc[sats], con.raan0[sats], con.u0[sats]
    n, rd = con.shell_n[sats], con.shell_raan_dot[sats]
    was = np.zeros(len(sats), bool)
    for t in times:
        dt = t - con.epoch
        raan = raan0 + rd * dt - gmst(t)
        u = u0 + n * dt
        cu, su, cr, sr, ci, si = np.cos(u), np.sin(u), np.cos(raan), np.sin(raan), np.cos(inc), np.sin(inc)
        x, y, z = cr * cu - sr * su * ci, sr * cu + cr * su * ci, su * si
        near = (x * math.cos(la) * math.cos(lo) + y * math.cos(la) * math.sin(lo) + z * math.sin(la)) > cos_reach
        for i in np.nonzero(near & ~was)[0]:
            out.append((float(t), int(sats[i])))
        was = near
    return out
