"""Automatic download of EGMS Ortho maps (Europe) as an independent check.

The European Ground Motion Service (https://egms.land.copernicus.eu) publishes Sentinel-1 ground motion for
Europe, calibrated with GNSS. Its Ortho (level 3) product gives vertical (U) and east-west (E) velocity on a
100 m grid, in 100 km tiles named after the European grid (EPSG:3035), e.g. EGMS_L3_E52N26_100km_U_2019_2023_1.

Downloads need a free personal key: sign in at egms.land.copernicus.eu; every download link in the Explorer ends
with ``?id=<key>``. Without it the service answers "You need a valid download token" (HTTP 401). The key is used
only for the download and never stored.
"""

from __future__ import annotations

import io
import math
import zipfile
from pathlib import Path

from .aoi import AOI
from .notes import INFO, Note

API = "https://egms.land.copernicus.eu/insar-api/archive/download"
RELEASES = ("2020_2024_1", "2019_2023_1", "2018_2022_1", "2015_2021_1")  # newest first
TILE_M = 100_000


class EGMSError(RuntimeError):
    """EGMS refused the download; the message says why in plain language."""


def in_europe(aoi: AOI) -> bool:
    lon, lat = aoi.center
    return -32 <= lon <= 45 and 27 <= lat <= 72  # EGMS covers the Copernicus participating states


def tiles(aoi: AOI) -> list[str]:
    """EGMS 100 km tile names (EPSG:3035 grid) covering the area, e.g. ['E52N26']."""
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    w, s, e, n = aoi.bounds
    xs, ys = [], []
    for i in range(11):  # densified edges: the projected box is not a rectangle
        for lon, lat in ((w + (e - w) * i / 10, s), (w + (e - w) * i / 10, n), (w, s + (n - s) * i / 10),
                         (e, s + (n - s) * i / 10)):
            x, y = tr.transform(lon, lat)
            xs.append(x)
            ys.append(y)
    cols = range(math.floor(min(xs) / TILE_M), math.floor(max(xs) / TILE_M) + 1)
    rows = range(math.floor(min(ys) / TILE_M), math.floor(max(ys) / TILE_M) + 1)
    return [f"E{c:02d}N{r:02d}" for r in rows for c in cols]


def tile_name(tile: str, component: str, release: str) -> str:
    return f"EGMS_L3_{tile}_100km_{component}_{release}"


def _fetch(url: str, params: dict):
    import requests

    return requests.get(url, params=params, timeout=300, headers={"User-Agent": "OpenSAR-Explorer"})


def _tif_from_zip(data: bytes, folder: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [m for m in z.namelist() if m.lower().endswith((".tif", ".tiff"))]
        if not names:
            raise EGMSError("The EGMS download contained no GeoTIFF.")
        target = folder / Path(names[0]).name
        target.write_bytes(z.read(names[0]))
        return target


def _mosaic(paths: list[Path], target: Path) -> Path:
    if len(paths) == 1:
        return paths[0]
    import rasterio
    from rasterio.merge import merge

    srcs = [rasterio.open(p) for p in paths]
    try:
        arr, transform = merge(srcs)
        meta = srcs[0].meta.copy()
        meta.update(height=arr.shape[1], width=arr.shape[2], transform=transform, compress="deflate")
        with rasterio.open(target, "w", **meta) as dst:
            dst.write(arr)
    finally:
        for s in srcs:
            s.close()
    return target


def download(aoi: AOI, key: str, folder: str | Path, components=("U",), releases=RELEASES,
             fetch=None) -> tuple[dict[str, Path], list[Note]]:
    """Download (or reuse) the EGMS Ortho maps covering the area: {component: GeoTIFF}, newest release first."""
    fetch = fetch or _fetch
    key = "".join((key or "").split())
    if not key:
        raise EGMSError("An EGMS download key is needed (free: sign in at egms.land.copernicus.eu).")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = tiles(aoi)
    for release in releases:
        out: dict[str, Path] = {}
        for comp in components:
            parts = []
            for tile in names:
                name = tile_name(tile, comp, release)
                cached = sorted(folder.glob(f"{name}*.tif*"))
                if cached:
                    parts.append(cached[0])
                    continue
                r = fetch(f"{API}/{name}.zip", {"id": key})
                if r.status_code in (401, 403):
                    raise EGMSError("EGMS did not accept the download key (HTTP "
                                    f"{r.status_code}). Copy it again from a download link in the EGMS Explorer "
                                    "(the part after '?id='); keys can expire.")
                if r.status_code == 404:
                    continue  # tile not in this release (e.g. sea), or release not published
                if r.status_code != 200:
                    raise EGMSError(f"EGMS answered HTTP {r.status_code} for {name}.")
                parts.append(_tif_from_zip(r.content, folder))
            if parts:
                label = "+".join(names) if len(names) > 1 else names[0]
                out[comp] = _mosaic(parts, folder / f"{tile_name(label, comp, release)}.tif")
        if out:
            period = release.rsplit("_", 1)[0].replace("_", "-")
            return out, [Note("validate", INFO, f"EGMS Ortho {period} downloaded for tile(s) {', '.join(names)} "
                                                f"({', '.join('vertical' if c == 'U' else 'east-west' for c in out)}).")]
    raise EGMSError(f"No EGMS Ortho tile was found for this area ({', '.join(names)}). EGMS covers the "
                    "Copernicus participating states in Europe.")
