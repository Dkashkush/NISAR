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
    window: dict = field(default_factory=dict)  # sensor -> GNSS rate over that sensor's own dates (see window_rate)
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
    corrections: dict = field(default_factory=dict)  # sensor -> GnssCorrection used to tie the map (if any)


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


def window_rate(dates: np.ndarray, values: np.ndarray, start: date, end: date,
                half_days: int = 6) -> tuple[float, float] | None:
    """Rate (m/yr) and 1-sigma from the position at the end minus the position at the start of a window.

    That is exactly what a gapless chain of InSAR pairs measures (the sum of the pairs is end minus start), so
    it stays comparable when motion is seasonal, unlike a straight-line fit. Positions are medians of the days
    within ``half_days`` of each end; None when either end has fewer than 3 days of data.
    """
    ends = []
    for d0 in (start, end):
        sel = np.array([abs((d - d0).days) <= half_days for d in dates])
        if sel.sum() < 3:
            return None
        v = values[sel]
        t = np.mean([(d - start).days for d in dates[sel]])
        ends.append((float(np.median(v)), 1.25 * float(np.std(v)) / math.sqrt(sel.sum()), t))
    years = (ends[1][2] - ends[0][2]) / 365.25
    if years <= 0:
        return None
    return (ends[1][0] - ends[0][0]) / years, math.hypot(ends[0][1], ends[1][1]) / years


def station_rate(st: GnssStation, sensor: str) -> float:
    """GNSS vertical rate (mm/yr) to compare with ``sensor``: over that sensor's own dates when available."""
    return station_rate_full(st, sensor)[0]


def station_rate_full(st: GnssStation, sensor: str) -> tuple[float, float, str]:
    """(rate, 1-sigma, period) of the GNSS vertical rate used for ``sensor``, in mm/yr."""
    w = st.window.get(sensor)
    if w:
        return w["up_mm_yr"], w["up_sigma_mm_yr"], w["period"]
    return st.up_mm_yr, st.up_sigma_mm_yr, st.period


def ngl_stations(aoi: AOI, start: date, end: date, cache: str | Path, max_stations: int = 30,
                 progress=None, windows: dict | None = None) -> tuple[list[GnssStation], list[str]]:
    """Stations inside the AOI with data in the InSAR period; velocities fitted over that period.

    ``windows`` ({sensor: (start, end)}): also measure each station over each satellite's own dates
    (``GnssStation.window``), which is what the comparison then uses.
    """
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
        st = GnssStation(c["site"], ts["lon"] if ts["lon"] is not None else c["lon"],
                         ts["lat"] if ts["lat"] is not None else c["lat"], v * 1000, sv * 1000,
                         f"{d.min()}→{d.max()}", int(sel.sum()), note)
        for sensor, (w0, w1) in (windows or {}).items():
            wr = window_rate(ts["dates"], ts["u"], w0, w1)
            if wr is not None:
                st.window[sensor] = {"up_mm_yr": wr[0] * 1000, "up_sigma_mm_yr": wr[1] * 1000,
                                     "period": f"{w0}→{w1}"}
        if windows and len(st.window) == len(windows):
            st.note = ""  # every comparison uses the rate over that satellite's own dates
        stations.append(st)
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


MIN_PLANE_STATIONS = 5


@dataclass
class GnssCorrection:
    """How one rate map was tied to GNSS: a constant shift, or a plane (shift + tilt) when stations allow."""

    kind: str  # "offset" or "plane"
    n: int
    shift_mm_yr: float  # value removed at the centre of the area
    tilt_ew_mm_yr: float = 0.0  # change of the removed plane across the area, west to east
    tilt_ns_mm_yr: float = 0.0  # south to north
    loo: dict = field(default_factory=dict)  # site -> leave-one-out residual (mm/yr), plane only
    why_offset: str = ""  # why no plane: "few" stations, poor "spread", likely "sign" error, or "mode" = offset


def fit_gnss_correction(rm: RateMap, stations: list[GnssStation], mode: str = "auto"
                        ) -> tuple[np.ndarray, GnssCorrection] | None:
    """Correction (m/yr, on the map's grid) that makes ``rm`` match GNSS, or None without usable stations.

    InSAR errors that do not cancel over a short period are mostly smooth across the area (troposphere and
    ionosphere of the first and last dates, orbits). With at least MIN_PLANE_STATIONS stations spread over the area,
    ``mode="auto"`` removes the best-fitting plane through InSAR - GNSS, as is common practice; otherwise one
    constant shift. Each station's leave-one-out residual (predicted from a plane through the others) is kept, so
    the validation stays honest.
    """
    tr = Transformer.from_crs("EPSG:4326", rm.rate.rio.crs, always_xy=True)
    xs, ys = rm.rate.x.values, rm.rate.y.values
    cx, cy = xs.mean(), ys.mean()
    pts = []
    for st in stations:
        v = sample_rate(rm, st.lon, st.lat)
        if math.isfinite(v):
            x, y = tr.transform(st.lon, st.lat)
            pts.append((st.site, (x - cx) / 1000, (y - cy) / 1000, v - station_rate(st, rm.sensor)))
    if not pts:
        return None
    X = np.array([p[1] for p in pts])
    Y = np.array([p[2] for p in pts])
    d = np.array([p[3] for p in pts])
    width, height = np.ptp(xs) / 1000, np.ptp(ys) / 1000
    spread = len(pts) >= 2 and np.ptp(X) >= 0.3 * width and np.ptp(Y) >= 0.3 * height
    g = np.array([station_rate(st, rm.sensor) for st in stations if st.site in {p[0] for p in pts}])
    A = np.column_stack([np.ones_like(X), X, Y])
    # Sign check: InSAR - GNSS should be the smoother (planar) field. If InSAR + GNSS fits a plane much better, the
    # InSAR sign is probably flipped, and a plane through the differences would partly hide that.
    flipped = False
    if len(pts) >= MIN_PLANE_STATIONS and g.std() > 0:
        rss = [float(np.sum((v - A @ np.linalg.lstsq(A, v, rcond=None)[0]) ** 2)) for v in (d, d + 2 * g)]
        flipped = rss[1] < 0.5 * rss[0]
    if mode != "offset" and len(pts) >= MIN_PLANE_STATIONS and spread and not flipped:
        coef, *_ = np.linalg.lstsq(A, d, rcond=None)
        loo = {}
        for k in range(len(pts)):
            keep = np.arange(len(pts)) != k
            ck, *_ = np.linalg.lstsq(A[keep], d[keep], rcond=None)
            loo[pts[k][0]] = float(d[k] - A[k] @ ck)
        GX, GY = np.meshgrid((xs - cx) / 1000, (ys - cy) / 1000)
        grid = (coef[0] + coef[1] * GX + coef[2] * GY) / 1000
        return grid, GnssCorrection("plane", len(pts), float(coef[0]), float(coef[1] * width),
                                    float(coef[2] * height), loo)
    shift = float(np.median(d))
    why = ("sign" if flipped else "spread" if len(pts) >= MIN_PLANE_STATIONS and not spread
           else "mode" if mode == "offset" else "few")
    return np.full(rm.rate.shape, shift / 1000), GnssCorrection("offset", len(pts), shift, why_offset=why)


def validate_gnss(stations: list[GnssStation], rates: list[RateMap], source: str,
                  skipped: list[str] | None = None, corrections: dict | None = None) -> GnssValidation:
    """``corrections`` ({sensor: GnssCorrection}): when a map was tied to GNSS with a plane, its errors are the
    leave-one-out residuals (each station predicted without itself), not the in-sample fit."""
    stats = {}
    for rm in rates:
        for st in stations:
            st.insar[rm.sensor] = sample_rate(rm, st.lon, st.lat)
        both = [st for st in stations if math.isfinite(st.insar[rm.sensor])]
        if not both:
            stats[rm.sensor] = SensorStats(0, float("nan"), float("nan"), float("nan"), float("nan"))
            continue
        d = np.array([st.insar[rm.sensor] - station_rate(st, rm.sensor) for st in both])
        offset = float(np.median(d))
        res = d - offset
        corr = (corrections or {}).get(rm.sensor)
        if corr is not None and corr.kind == "plane":
            res = np.array([corr.loo.get(st.site, float("nan")) for st in both])
            offset = 0.0
        for st, rv in zip(both, res):
            st.residual[rm.sensor] = float(rv)
        g = np.array([station_rate(st, rm.sensor) for st in both])
        i = np.array([st.insar[rm.sensor] for st in both])
        r = float(np.corrcoef(g, i)[0, 1]) if len(both) >= 3 and g.std() > 0 and i.std() > 0 else float("nan")
        rmse = float(np.sqrt(np.mean(res ** 2))) if len(both) >= 2 else float("nan")
        stats[rm.sensor] = SensorStats(len(both), offset, rmse, float(np.mean(np.abs(res))), r)
    return GnssValidation(source, stations, stats, skipped or [], dict(corrections or {}))


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


_LENGTH_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "ft": 304.8, "in": 25.4}


def _units_to_mm_per_yr(spec: dict) -> float:
    """Scale factor to mm/yr. Accepts rates (mm/yr, cm/yr, m/yr, ft/yr, in/yr) or total displacement over a
    period (mm, cm, m, ft, in) together with ``period_years`` (e.g. a cumulative-subsidence map for 2015-2024)."""
    units = str(spec.get("units", "mm/yr")).lower().replace(" ", "").replace("/y", "/yr").replace("/yrr", "/yr")
    length, _, per = units.partition("/")
    if length not in _LENGTH_MM or per not in ("", "yr"):
        raise ValueError(f"Unknown units {spec.get('units')!r}; use mm/yr, cm/yr, m/yr, ft/yr, in/yr, or a length "
                         "(mm, cm, m, ft, in) with period_years for total displacement maps")
    if per == "yr":
        return _LENGTH_MM[length]
    years = spec.get("period_years")
    if not years or float(years) <= 0:
        raise ValueError(f"Units {spec.get('units')!r} are a total displacement; also give period_years "
                         "(the number of years the map covers) to turn it into a rate")
    return _LENGTH_MM[length] / float(years)


def load_published_map(spec: dict, template: xr.DataArray) -> xr.DataArray:
    """Read a published velocity raster and convert it to vertical mm/yr, positive = up."""
    da = rioxarray.open_rasterio(spec["path"], masked=True)
    if "band" in da.dims:
        da = da.isel(band=int(spec.get("band", 1)) - 1, drop=True)
    if da.rio.crs is None:
        da = da.rio.write_crs("EPSG:4326")
    da = da.astype("float32").rio.write_nodata(np.nan, encoded=False)
    scale = _units_to_mm_per_yr(spec)
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
        if ref.mode == "gnss":  # the InSAR maps were then shifted to match GNSS: shift the published map alike
            tie = [np.nanmedian(rm.rate.values[ref.mask]) * 1000 for rm in rates]
            pv = pv + float(np.mean(tie))
            mode = "referenced like the InSAR maps (area median, then tied to GNSS)"
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


# ---------------------------------------------------------------------------
# Published values from papers / reports
# ---------------------------------------------------------------------------


@dataclass
class LiteratureCheck:
    """One rate reported in a published study, compared with this run's rates at the same place."""

    citation: str
    place: str
    lon: float
    lat: float
    radius_km: float
    published_mm_yr: float  # vertical, negative = sinking
    published_min: float | None
    published_max: float | None
    period: str
    insar: dict = field(default_factory=dict)  # sensor -> median rate (mm/yr) within radius
    absolute: dict = field(default_factory=dict)  # sensor -> True if tied to GNSS (absolute), else relative
    verdict: dict = field(default_factory=dict)  # sensor -> consistent | differs | no data | outside area


def _rate_in_radius(rm: RateMap, lon: float, lat: float, radius_km: float) -> tuple[float, str]:
    x, y = Transformer.from_crs("EPSG:4326", rm.rate.rio.crs, always_xy=True).transform(lon, lat)
    xs, ys = rm.rate.x.values, rm.rate.y.values
    if not (xs.min() <= x <= xs.max() and ys.min() <= y <= ys.max()):
        return float("nan"), "outside area"
    res = abs(xs[1] - xs[0]) if xs.size > 1 else 1.0
    r = max(radius_km * 1000.0, res)
    cols = np.where(np.abs(xs - x) <= r)[0]
    rows = np.where(np.abs(ys - y) <= r)[0]
    X, Y = np.meshgrid(xs[cols], ys[rows])
    vals = rm.rate.values[np.ix_(rows, cols)] * 1000
    vals = vals[(np.hypot(X - x, Y - y) <= r) & np.isfinite(vals)]
    if vals.size == 0:
        return float("nan"), "no data"
    return float(np.median(vals)), ""


def parse_published_value(v: dict) -> dict:
    """Normalise one published value: rate as vertical mm/yr, negative = sinking."""
    rate = float(v["rate_mm_yr"])
    lo = v.get("rate_min_mm_yr")
    hi = v.get("rate_max_mm_yr")
    if str(v.get("component", "vertical")).upper() == "LOS":
        inc = v.get("incidence_deg")
        if inc is None:
            raise ValueError(f"Published value from {v.get('citation')!r} is LOS: give incidence_deg")
        f = 1.0 / np.cos(np.radians(float(inc)))
        rate, lo, hi = rate * f, (lo * f if lo is not None else None), (hi * f if hi is not None else None)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return {"citation": str(v.get("citation", "")).strip() or "(no citation given)",
            "place": str(v.get("place", "")).strip(), "lon": _std_lon(float(v["lon"])), "lat": float(v["lat"]),
            "radius_km": float(v.get("radius_km") or 1.0), "rate": rate,
            "lo": float(lo) if lo is not None else None, "hi": float(hi) if hi is not None else None,
            "period": str(v.get("period", "")).strip()}


def validate_literature(values: list[dict], rates: list[RateMap], gnss: GnssValidation | None = None,
                        tied: bool = False) -> list[LiteratureCheck]:
    """Compare rates reported in published studies with this run, place by place.

    InSAR rates here are relative (zero = area median). When the GNSS check produced an offset for a sensor,
    it is removed so the comparison is absolute, like most published rates. A value counts as consistent when
    it lies within the published range, or within max(2 x rate noise, 25 % of the published rate, 5 mm/yr).
    """
    out = []
    for raw in values:
        v = parse_published_value(raw)
        chk = LiteratureCheck(v["citation"], v["place"], v["lon"], v["lat"], v["radius_km"], v["rate"],
                              v["lo"], v["hi"], v["period"])
        for rm in rates:
            val, problem = _rate_in_radius(rm, v["lon"], v["lat"], v["radius_km"])
            stats = gnss.stats.get(rm.sensor) if gnss is not None else None
            absolute = tied or bool(stats is not None and stats.n >= 2 and math.isfinite(stats.offset_mm_yr))
            if absolute and not tied and math.isfinite(val):
                val -= stats.offset_mm_yr
            chk.insar[rm.sensor] = val
            chk.absolute[rm.sensor] = absolute
            if problem:
                chk.verdict[rm.sensor] = problem
                continue
            noise = rm.noise_rate * 1000 if math.isfinite(rm.noise_rate) else 10.0
            if v["lo"] is not None and v["hi"] is not None:
                ok = v["lo"] - 2 * noise <= val <= v["hi"] + 2 * noise
            else:
                ok = abs(val - v["rate"]) <= max(2 * noise, 0.25 * abs(v["rate"]), 5.0)
            chk.verdict[rm.sensor] = "consistent" if ok else "differs"
        out.append(chk)
    return out
