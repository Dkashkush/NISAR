"""Quantitative comparison of two referenced rate maps on the same grid."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import xarray as xr
from pyproj import Transformer
from scipy import ndimage

from .processing import RateMap


@dataclass
class Hotspot:
    sensor: str
    lon: float
    lat: float
    area_km2: float
    peak_mm_yr: float
    mean_mm_yr: float
    seen_by_other: str  # "yes", "no", or "no data" (other sensor decorrelated there)


@dataclass
class Comparison:
    n_aoi_pixels: int
    pixel_km2: float
    coverage_a: float  # fraction of AOI pixels with a valid rate
    coverage_b: float
    coverage_both: float
    only_a: float
    only_b: float
    coh_mean_a: float
    coh_mean_b: float
    n_joint: int
    pearson_r: float
    slope: float  # b vs a, total least squares
    bias_mm_yr: float  # mean(a - b)
    rmsd_mm_yr: float
    rmsd_detrended_mm_yr: float  # after removing a best-fit plane from the difference
    ramp_mm_yr: float  # range of that plane across the AOI
    range_a_mm_yr: tuple[float, float]
    range_b_mm_yr: tuple[float, float]
    noise_a_mm_yr: float
    noise_b_mm_yr: float
    sign_suspect: bool
    hotspots: list[Hotspot] = field(default_factory=list)
    difference: xr.DataArray | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("difference", None)
        return d


def _tls_slope(a: np.ndarray, b: np.ndarray) -> float:
    """Total-least-squares slope of b against a (both have noise, so plain regression would be biased)."""
    if a.size < 3:
        return float("nan")
    cov = np.cov(np.vstack([a - a.mean(), b - b.mean()]))
    w, v = np.linalg.eigh(cov)
    vec = v[:, np.argmax(w)]
    return float(vec[1] / vec[0]) if abs(vec[0]) > 1e-12 else float("nan")


def _plane(diff: np.ndarray, xx: np.ndarray, yy: np.ndarray, ok: np.ndarray):
    A = np.column_stack([np.ones(ok.sum()), xx[ok], yy[ok]])
    coef, *_ = np.linalg.lstsq(A, diff[ok], rcond=None)
    return coef[0] + coef[1] * xx + coef[2] * yy


def find_hotspots(rm: RateMap, other: RateMap, inside: np.ndarray, min_rate_mm: float,
                  min_area_km2: float = 0.25, top: int = 3) -> list[Hotspot]:
    """Connected areas moving faster than ``min_rate_mm`` (either direction), largest first."""
    r = rm.rate.values * 1000
    o = other.rate.values * 1000
    res = abs(float(rm.rate.x[1] - rm.rate.x[0])) if rm.rate.x.size > 1 else 1.0
    px_km2 = res * res / 1e6
    min_pixels = max(4, int(np.ceil(min_area_km2 / px_km2)))
    to_ll = Transformer.from_crs(rm.rate.rio.crs, "EPSG:4326", always_xy=True)
    out: list[Hotspot] = []
    for sign in (-1, 1):
        mask = inside & np.isfinite(r) & (sign * r > min_rate_mm)
        labels, n = ndimage.label(mask)
        for k in range(1, n + 1):
            region = labels == k
            if region.sum() < min_pixels:
                continue
            vals = r[region]
            peak = float(vals[np.argmax(np.abs(vals))])
            rows, cols = np.nonzero(region)
            lon, lat = to_ll.transform(float(rm.rate.x.values[int(cols.mean())]),
                                       float(rm.rate.y.values[int(rows.mean())]))
            ov = o[region]
            frac_valid = np.isfinite(ov).mean()
            if frac_valid < 0.3:
                seen = "no data"
            else:
                seen = "yes" if np.nanmean(sign * ov) > 0.5 * min_rate_mm else "no"
            out.append(Hotspot(rm.sensor, float(lon), float(lat), float(region.sum() * px_km2), peak,
                               float(vals.mean()), seen))
    out.sort(key=lambda h: -h.area_km2)
    return out[:top]


def compare(a: RateMap, b: RateMap, inside: np.ndarray) -> Comparison:
    ra, rb = a.rate.values * 1000, b.rate.values * 1000  # mm/yr
    va, vb = inside & np.isfinite(ra), inside & np.isfinite(rb)
    joint = va & vb
    n = int(inside.sum()) or 1
    res = abs(float(a.rate.x[1] - a.rate.x[0])) if a.rate.x.size > 1 else 1.0
    xa, xb = ra[joint], rb[joint]

    if joint.sum() >= 3 and np.std(xa) > 0 and np.std(xb) > 0:
        r = float(np.corrcoef(xa, xb)[0, 1])
    else:
        r = float("nan")
    diff = np.where(joint, ra - rb, np.nan)
    xx, yy = np.meshgrid(a.rate.x.values / 1000, a.rate.y.values / 1000)
    if joint.sum() >= 10:
        plane = _plane(diff, xx, yy, joint)
        pv = plane[inside]
        ramp = float(pv.max() - pv.min())
        rmsd_d = float(np.sqrt(np.nanmean((diff - plane)[joint] ** 2)))
    else:
        ramp, rmsd_d = float("nan"), float("nan")

    def rng(v, m):
        return (float(np.nanpercentile(v[m], 2)), float(np.nanpercentile(v[m], 98))) if m.any() else (np.nan, np.nan)

    noise = max(a.noise_rate, b.noise_rate) * 1000 if np.isfinite(a.noise_rate) and np.isfinite(b.noise_rate) else 5.0
    thresh = max(10.0, 3 * noise)
    hot = find_hotspots(a, b, inside, thresh) + find_hotspots(b, a, inside, thresh)

    coh_a = a.coherence.values[inside]
    coh_b = b.coherence.values[inside]
    return Comparison(
        n_aoi_pixels=n, pixel_km2=res * res / 1e6,
        coverage_a=float(va.sum() / n), coverage_b=float(vb.sum() / n), coverage_both=float(joint.sum() / n),
        only_a=float((va & ~vb).sum() / n), only_b=float((vb & ~va).sum() / n),
        coh_mean_a=float(np.nanmean(coh_a)) if np.isfinite(coh_a).any() else float("nan"),
        coh_mean_b=float(np.nanmean(coh_b)) if np.isfinite(coh_b).any() else float("nan"),
        n_joint=int(joint.sum()), pearson_r=r, slope=_tls_slope(xa, xb),
        bias_mm_yr=float(np.mean(xa - xb)) if xa.size else float("nan"),
        rmsd_mm_yr=float(np.sqrt(np.mean((xa - xb) ** 2))) if xa.size else float("nan"),
        rmsd_detrended_mm_yr=rmsd_d, ramp_mm_yr=ramp,
        range_a_mm_yr=rng(ra, va), range_b_mm_yr=rng(rb, vb),
        noise_a_mm_yr=a.noise_rate * 1000, noise_b_mm_yr=b.noise_rate * 1000,
        sign_suspect=bool(np.isfinite(r) and r < -0.3),
        hotspots=hot,
        difference=a.rate.copy(data=(diff / 1000).astype("float32")).rename("difference"),
    )
