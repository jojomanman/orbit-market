"""Build static/land.json: an even dot grid over land (Natural Earth 110 m, public domain), for the globe and for
siting imaging targets. Run once; the output is committed.

Usage: python3 tools/land_dots.py ne_110m_land.geojson static/land.json
"""
import json
import math
import sys

import numpy as np
from matplotlib.path import Path

N = 40000   # Fibonacci-sphere points: ~110 km spacing


def main(src, dst):
    i = np.arange(N) + 0.5
    lat = np.degrees(np.arcsin(1 - 2 * i / N))
    lon = (np.degrees(math.pi * (1 + 5 ** 0.5) * i) + 180) % 360 - 180
    pts = np.stack([lon, lat], 1)
    land = np.zeros(N, bool)
    for feat in json.load(open(src))["features"]:
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            land |= Path(np.array(poly[0])).contains_points(pts)
    out = [[round(float(a), 2), round(float(b), 2)] for a, b in zip(lat[land], lon[land])]
    json.dump(out, open(dst, "w"), separators=(",", ":"))
    print(len(out), "land dots")


if __name__ == "__main__":
    main(*sys.argv[1:3])
