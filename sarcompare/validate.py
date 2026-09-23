"""Validation against independent, published data.

Two kinds of reference are supported:

* **GNSS velocities.** Either automatically from the Nevada Geodetic Laboratory (NGL, geodesy.unr.edu):
  station list ``DataHoldings.txt`` + daily ``.tenv3`` position series (IGS20), read with the same column
  layout as MintPy's ``objects/gnss.py``; velocities are fitted over the *same period* as the InSAR. Or from
  a CSV of published station velocities (e.g. a table from a paper): ``site,lon,lat,up_mm_yr[,up_sigma_mm_yr]``.
* **Published velocity maps** (GeoTIFF/NetCDF readable by rasterio): e.g. EGMS Ortho (vertical), LiCSAR/COMET
  LiCSBAS or OPERA DISP velocities (LOS), or supplementary data from a paper.

InSAR rates are relative to the reference point, while GNSS are absolute, so one constant offset per sensor
(median InSAR − GNSS) is removed before computing errors. That is standard practice and is reported.
"""

from __future__ import annotations

import csv
import math
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import rioxarray
import xarray as xr
from pyproj import Transformer

from .aoi import AOI
from .compare import _tls_slope
from .processing import RateMap, Reference, regrid

NGL_SITE_LIST_URL = "https://geodesy.unr.edu/NGLStationPages/DataHoldings.txt"
NGL_TENV3_URL = "https://geodesy.unr.edu/gps_timeseries/IGS20/tenv3/IGS20/{site}.tenv3"
MIN_SPAN_YEARS = 0.5
MIN_EPOCHS = 50


# ---------------------------------------------------------------------------
# GNSS
# ---------------------------------------------------------------------------


@dataclass
class GnssStation:
    site: str
    lon: float
    lat: float
    up_mm_yr: float
    up_sigma_mm_yr: float
    period: str = ""
    n_epochs: int = 0
    note: str = ""
    insar: dict = field(default_factory=dict)  # sensor -> referenced InSAR vertical rate (mm/yr) or nan
    residual: dict = field(default_factory=dict)  # sensor -> InSAR - GNSS - offset (mm/yr)


@dataclass
class SensorStats:
    n: int
    offset_mm_yr: float
    rmse_mm_yr: float
    mean_abs_mm_yr: float
    r: float


@dataclass
class GnssValidation:
    source: str
    stations: list[GnssStation]
    stats: dict[str, SensorStats]
    skipped: list[str] = field(default_factory=list)


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:  # nosec - fixed https URLs
        f.write(r.read())
    tmp.replace(dest)
    return dest


def _std_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0


def read_ngl_site_list(path: str | Path) -> list[dict]:
    """NGL DataHoldings.txt: Sta Lat Long Hgt X Y Z Dtbeg Dtend Dtmod NumSol ... (header line skipped)."""
    sites = []
    for line in Path(path).read_text().splitlines()[1:]:
        p = line.split()
        if len(p) < 11:
            continue
        try:
            sites.append({"site": p[0], "lat": float(p[1]), "lon": _std_lon(float(p[2])),
                          "start": datetime.strptime(p[7], "%Y-%m-%d").date(),
                          "end": datetime.strptime(p[8], "%Y-%m-%d").date(), "n": int(p[10])})
        except ValueError:
            continue
    return sites


def read_tenv3(path: str | Path) -> dict:
    """NGL .tenv3 daily positions. Columns (0-based, as in MintPy): 1 date (YYMMMDD), 7+8 east, 9+10 north,
    11+12 up (integer + fractional metres), 14-16 sigmas, 20 lat, 21 lon."""
    dates, e, n, u, su = [], [], [], [], []
    lat = lon = None
    for line in Path(path).read_text().splitlines()[1:]:
        p = line.split()
        if len(p) < 22:
            continue
        try:
            dates.append(datetime.strptime(p[1], "%y%b%d").date())
            e.append(float(p[7]) + float(p[8]))
            n.append(float(p[9]) + float(p[10]))
            u.append(float(p[11]) + float(p[12]))
            su.append(float(p[16]))
            lat, lon = float(p[20]), _std_lon(float(p[21]))
        except ValueError:
            continue
    return {"dates": np.array(dates), "e": np.array(e), "n": np.array(n), "u": np.array(u),
            "sig_u": np.array(su), "lat": lat, "lon": lon}


def _decimal_year(d: date) -> float:
    start = date(d.year, 1, 1)
    return d.year + (d - start).days / ((date(d.year + 1, 1, 1) - start).days)


def fit_velocity(dates: np.ndarray, values: np.ndarray) -> tuple[float, float]:
    """Linear rate (m/yr) and a realistic 1-sigma, fitting an annual cycle too when the span allows.

    The white-noise formal error is inflated by sqrt(n / n_months) because daily GNSS positions are
    strongly correlated in time (formal errors are otherwise far too small).
    """
    t = np.array([_decimal_year(d) for d in dates])
    t0 = t.mean()
    cols = [np.ones_like(t), t - t0]
    if t.max() - t.min() >= 1.5:
        cols += [np.sin(2 * np.pi * t), np.cos(2 * np.pi * t)]
    A = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(A, values, rcond=None)
    resid = values - A @ coef
    dof = max(len(t) - A.shape[1], 1)
    cov = np.linalg.pinv(A.T @ A) * (resid @ resid / dof)
    sigma = math.sqrt(max(cov[1, 1], 0.0))
    n_months = max((t.max() - t.min()) * 12, 1.0)
    sigma *= math.sqrt(max(len(t) / n_months, 1.0))
    return float(coef[1]), float(sigma)


def ngl_stations(aoi: AOI, start: date, end: date, cache: str | Path, max_stations: int = 30,
                 progress=None) -> tuple[list[GnssStation], list[str]]:
    """Stations inside the AOI with data in the InSAR period; velocities fitted over that period."""
    cache = Path(cache)
    site_file = cache / "DataHoldings.txt"
    if not site_file.exists():
        _download(NGL_SITE_LIST_URL, site_file)
    w, s, e, n = aoi.bounds
    cands = [x for x in read_ngl_site_list(site_file)
             if w <= x["lon"] <= e and s <= x["lat"] <= n and x["end"] >= start and x["start"] <= end]
    cands.sort(key=lambda x: -x["n"])
    stations, skipped = [], []
    for k, c in enumerate(cands[:max_stations]):
        if progress:
            progress(k, min(len(cands), max_stations), c["site"])
        f = cache / f"{c['site']}.tenv3"
        try:
            if not f.exists():
                _download(NGL_TENV3_URL.format(site=c["site"]), f)
            ts = read_tenv3(f)
        except OSError as err:
            skipped.append(f"{c['site']}: download failed ({err})")
            continue
        sel = (ts["dates"] >= start) & (ts["dates"] <= end)
        note = ""
        span = (ts["dates"][sel].max() - ts["dates"][sel].min()).days / 365.25 if sel.sum() > 1 else 0
        if sel.sum() < MIN_EPOCHS or span < MIN_SPAN_YEARS:
            sel = np.ones(ts["dates"].size, bool)  # fall back to the whole record
            note = "too little data in the InSAR period; used the full record (long-term rate)"
            if sel.sum() < MIN_EPOCHS:
                skipped.append(f"{c['site']}: only {sel.sum()} daily positions")
                continue
        v, sv = fit_velocity(ts["dates"][sel], ts["u"][sel])
        d = ts["dates"][sel]
        stations.append(GnssStation(c["site"], ts["lon"] if ts["lon"] is not None else c["lon"],
                                    ts["lat"] if ts["lat"] is not None else c["lat"], v * 1000, sv * 1000,
                                    f"{d.min()}→{d.max()}", int(sel.sum()), note))
    if len(cands) > max_stations:
        skipped.append(f"{len(cands) - max_stations} more stations not used (max_stations = {max_stations})")
    return stations, skipped


def csv_stations(path: str | Path) -> list[GnssStation]:
    """Published station velocities: columns site, lon, lat, up_mm_yr (or up_m_yr), optional up_sigma_mm_yr."""
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            scale = 1000.0 if "up_m_yr" in row else 1.0
            up = float(row.get("up_mm_yr") or row["up_m_yr"]) * scale
            if row.get("up_sigma_mm_yr"):
                sig = float(row["up_sigma_mm_yr"])
            elif row.get("up_sigma_m_yr"):
                sig = float(row["up_sigma_m_yr"]) * 1000.0
            else:
                sig = float("nan")
            out.append(GnssStation(row.get("site", "?"), _std_lon(float(row["lon"])), float(row["lat"]), up,
                                   sig, row.get("period", ""), note="published table"))
    return out


def sample_rate(rm: RateMap, lon: float, lat: float, half: int = 1) -> float:
    """Median InSAR rate (mm/yr) in a (2*half+1)^2 window around a point; nan outside/no data."""
    x, y = Transformer.from_crs("EPSG:4326", rm.rate.rio.crs, always_xy=True).transform(lon, lat)
    xs, ys = rm.rate.x.values, rm.rate.y.values
    res = abs(xs[1] - xs[0]) if xs.size > 1 else 1.0
    if not (xs.min() - res <= x <= xs.max() + res and ys.min() - res <= y <= ys.max() + res):
        return float("nan")
    c, r = int(np.argmin(np.abs(xs - x))), int(np.argmin(np.abs(ys - y)))
    patch = rm.rate.values[max(0, r - half):r + half + 1, max(0, c - half):c + half + 1] * 1000
    return float(np.nanmedian(patch)) if np.isfinite(patch).any() else float("nan")


def validate_gnss(stations: list[GnssStation], rates: list[RateMap], source: str,
                  skipped: list[str] | None = None) -> GnssValidation:
    stats = {}
    for rm in rates:
        for st in stations:
            st.insar[rm.sensor] = sample_rate(rm, st.lon, st.lat)
        both = [st for st in stations if math.isfinite(st.insar[rm.sensor])]
        if not both:
            stats[rm.sensor] = SensorStats(0, float("nan"), float("nan"), float("nan"), float("nan"))
            continue
        d = np.array([st.insar[rm.sensor] - st.up_mm_yr for st in both])
        offset = float(np.median(d))
        res = d - offset
        for st, rv in zip(both, res):
            st.residual[rm.sensor] = float(rv)
        g = np.array([st.up_mm_yr for st in both])
        i = np.array([st.insar[rm.sensor] for st in both])
        r = float(np.corrcoef(g, i)[0, 1]) if len(both) >= 3 and g.std() > 0 and i.std() > 0 else float("nan")
        rmse = float(np.sqrt(np.mean(res ** 2))) if len(both) >= 2 else float("nan")
        stats[rm.sensor] = SensorStats(len(both), offset, rmse, float(np.mean(np.abs(res))), r)
    return GnssValidation(source, stations, stats, skipped or [])


# ---------------------------------------------------------------------------
# Published velocity maps
# ---------------------------------------------------------------------------


@dataclass
class MapStats:
    n: int
    r: float
    slope: float
    rmsd_mm_yr: float
    bias_mm_yr: float
    coverage: float  # fraction of this sensor's valid pixels also covered by the published map


@dataclass
class PublishedValidation:
    label: str
    path: str
    reference_mode: str  # how the published map was tied to our reference point
    published: xr.DataArray  # vertical mm/yr on the common grid, referenced like the InSAR maps
    stats: dict[str, MapStats]
    difference: dict[str, xr.DataArray]  # sensor - published (mm/yr)
    period: str = ""


def load_published_map(spec: dict, template: xr.DataArray) -> xr.DataArray:
    """Read a published velocity raster and convert it to vertical mm/yr, positive = up."""
    da = rioxarray.open_rasterio(spec["path"], masked=True)
    if "band" in da.dims:
        da = da.isel(band=int(spec.get("band", 1)) - 1, drop=True)
    if da.rio.crs is None:
        da = da.rio.write_crs("EPSG:4326")
    da = da.astype("float32").rio.write_nodata(np.nan, encoded=False)
    units = str(spec.get("units", "mm/yr")).lower().replace(" ", "")
    scale = {"mm/yr": 1.0, "mm/y": 1.0, "m/yr": 1000.0, "m/y": 1000.0, "cm/yr": 10.0, "cm/y": 10.0}.get(units)
    if scale is None:
        raise ValueError(f"Unknown units {spec.get('units')!r}; use mm/yr, cm/yr or m/yr")
    out = regrid(da, template) * scale * float(spec.get("sign", 1.0))
    if str(spec.get("component", "vertical")).upper() == "LOS":
        inc = spec.get("incidence_deg")
        if inc is None:
            raise ValueError("A LOS published map needs incidence_deg to convert it to vertical")
        out = out / np.cos(np.radians(float(inc)))
    return out


def validate_published(spec: dict, rates: list[RateMap], ref: Reference, inside: np.ndarray) -> PublishedValidation:
    template = rates[0].rate
    pub = load_published_map(spec, template)
    pv = pub.values
    sel = ref.mask & np.isfinite(pv)
    if sel.sum() >= max(5, 0.2 * ref.mask.sum()):
        pv = pv - np.nanmedian(pv[sel])
        mode = ("referenced like the InSAR maps (median over the shared reference pixels)" if ref.mode == "area"
                else "referenced to the same point as the InSAR maps")
    else:
        # published map lacks data at our reference: align by the median offset to the first sensor
        a = rates[0].rate.values * 1000
        ok = inside & np.isfinite(a) & np.isfinite(pv)
        if not ok.any():
            raise ValueError(f"Published map {spec['path']} does not overlap the InSAR maps")
        pv = pv + np.median(a[ok] - pv[ok])
        mode = f"too little data at the reference; aligned by the median offset to {rates[0].sensor}"
    pub = pub.copy(data=pv.astype("float32"))
    stats, diffs = {}, {}
    for rm in rates:
        r_ = rm.rate.values * 1000
        own = inside & np.isfinite(r_)
        ok = own & np.isfinite(pv)
        x, y = pv[ok], r_[ok]
        diffs[rm.sensor] = rm.rate.copy(data=np.where(ok, r_ - pv, np.nan).astype("float32"))
        if ok.sum() >= 3 and x.std() > 0 and y.std() > 0:
            stats[rm.sensor] = MapStats(int(ok.sum()), float(np.corrcoef(x, y)[0, 1]), _tls_slope(x, y),
                                        float(np.sqrt(np.mean((y - x) ** 2))), float(np.mean(y - x)),
                                        float(ok.sum() / max(own.sum(), 1)))
        else:
            stats[rm.sensor] = MapStats(int(ok.sum()), *(float("nan"),) * 4, float(ok.sum() / max(own.sum(), 1)))
    return PublishedValidation(spec.get("label") or Path(spec["path"]).stem, str(spec["path"]), mode, pub,
                               stats, diffs, str(spec.get("period", "")))
