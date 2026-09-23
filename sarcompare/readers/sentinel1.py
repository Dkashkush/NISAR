"""Readers for Sentinel-1 interferometric products.

Supported:
* ARIA S1 GUNW (netCDF4/HDF5, lon/lat grid) - ASF dataset ARIA_S1_GUNW
* OPERA L3 DISP-S1 (netCDF4, UTM grid, North America) - ASF dataset OPERA_S1
* HyP3 on-demand INSAR_GAMMA / INSAR_ISCE_BURST (zip or folder of GeoTIFFs) - global, ordered in ASF Vertex
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import h5py
import numpy as np
import rioxarray
from pyproj import CRS

from ..aoi import AOI
from ..notes import WARN, Note
from ..products import SENTINEL1, WAVELENGTH_S1_C, pair_dates_from_name
from .base import Interferogram, aoi_slices, apply_fill, decode, make_da

DEFAULT_INCIDENCE = 38.0  # mid-swath for Sentinel-1 IW (range ~30-46°)
# ARIA GUNW uses the later acquisition as reference, so positive phase = motion toward the satellite.
ARIA_PHASE_SIGN = 1.0
# HyP3 unwrapped phase: positive = motion away from the satellite (its los_disp layer is already signed toward-positive).
HYP3_PHASE_SIGN = -1.0
OPERA_SIGN = 1.0  # DISP-S1 'displacement' is already metres, positive toward the satellite


def _dates(name: str):
    dates = pair_dates_from_name(name)
    if not dates:
        raise ValueError(f"Cannot determine acquisition dates from name {name}")
    return dates


def _first(h5: h5py.File, *candidates: str) -> str | None:
    for c in candidates:
        if c in h5:
            return c
    return None


# ---------------------------------------------------------------------------
# ARIA S1 GUNW
# ---------------------------------------------------------------------------


def read_aria_gunw(path: str | Path, aoi: AOI, sign: float | None = None) -> Interferogram:
    path = Path(path)
    notes: list[Note] = []
    date1, date2 = _dates(path.name)
    with h5py.File(path, "r") as h5:
        g = "/science/grids/data"
        lon, lat = h5[f"{g}/longitude"][()], h5[f"{g}/latitude"][()]
        rows, cols = aoi_slices(lon, lat, aoi.bounds)
        uds = h5[f"{g}/unwrappedPhase"]
        phase = apply_fill(uds[rows, cols], uds.attrs.get("_FillValue"))
        coh = apply_fill(h5[f"{g}/coherence"][rows, cols], h5[f"{g}/coherence"].attrs.get("_FillValue"))
        if f"{g}/connectedComponents" in h5:
            cc = h5[f"{g}/connectedComponents"][rows, cols]
            phase = np.where(cc <= 0, np.nan, phase)
        wl_path = _first(h5, "/science/radarMetaData/wavelength")
        wavelength = float(np.asarray(h5[wl_path][()]).ravel()[0]) if wl_path else WAVELENGTH_S1_C
        inc_path = _first(h5, "/science/grids/imagingGeometry/incidenceAngle")
        inc = float(np.nanmean(h5[inc_path][()])) if inc_path else None
    if inc is None or not np.isfinite(inc):
        inc = DEFAULT_INCIDENCE
        notes.append(Note("load", WARN, f"Sentinel-1 {date1}→{date2}: no incidence layer; assuming {inc}°."))
    direction = None
    name_upper = path.name.upper()
    if "-A-" in name_upper:
        direction = "ASCENDING"
    elif "-D-" in name_upper:
        direction = "DESCENDING"
    s = ARIA_PHASE_SIGN if sign is None else sign
    los = s * phase * wavelength / (4 * np.pi)
    return Interferogram(
        sensor=SENTINEL1, product_type="ARIA_GUNW", name=path.name, date1=date1, date2=date2,
        los=make_da(los, lon[cols], lat[rows], "EPSG:4326", "los"),
        coherence=make_da(coh, lon[cols], lat[rows], "EPSG:4326", "coherence"),
        wavelength=wavelength, incidence_deg=inc, flight_direction=direction, notes=notes,
    )


# ---------------------------------------------------------------------------
# OPERA DISP-S1
# ---------------------------------------------------------------------------


def _opera_crs(h5: h5py.File):
    sr = h5.get("spatial_ref")
    if sr is not None:
        for key in ("crs_wkt", "spatial_ref"):
            if key in sr.attrs:
                return CRS.from_wkt(decode(sr.attrs[key]))
    raise ValueError("OPERA DISP-S1: missing spatial_ref CRS")


def read_opera_disp(path: str | Path, aoi: AOI, sign: float | None = None) -> Interferogram:
    path = Path(path)
    notes = [Note("load", WARN, "OPERA DISP-S1 files do not carry incidence angles (they are in the separate "
                                f"DISP-S1-STATIC product); assuming {DEFAULT_INCIDENCE}° for vertical projection.")]
    date1, date2 = _dates(path.name)
    with h5py.File(path, "r") as h5:
        crs = _opera_crs(h5)
        x, y = h5["x"][()], h5["y"][()]
        rows, cols = aoi_slices(x, y, aoi.bounds_in(crs))
        dds = h5["displacement"]
        disp = apply_fill(dds[rows, cols], dds.attrs.get("_FillValue"))
        coh_name = _first(h5, "temporal_coherence", "estimated_phase_quality", "phase_similarity")
        coh = apply_fill(h5[coh_name][rows, cols], h5[coh_name].attrs.get("_FillValue")) if coh_name \
            else np.full_like(disp, np.nan)
        if "recommended_mask" in h5:
            disp = np.where(h5["recommended_mask"][rows, cols] == 0, np.nan, disp)
    s = OPERA_SIGN if sign is None else sign
    return Interferogram(
        sensor=SENTINEL1, product_type="DISP-S1", name=path.name, date1=date1, date2=date2,
        los=make_da(s * disp, x[cols], y[rows], crs, "los"),
        coherence=make_da(coh, x[cols], y[rows], crs, "coherence"),
        wavelength=WAVELENGTH_S1_C, incidence_deg=DEFAULT_INCIDENCE, notes=notes,
    )


# ---------------------------------------------------------------------------
# HyP3 on-demand InSAR (GeoTIFFs)
# ---------------------------------------------------------------------------


def _hyp3_folder(path: Path) -> Path:
    if path.is_dir():
        return path
    if path.suffix.lower() == ".zip":
        out = path.with_suffix("")
        if not out.exists():
            with zipfile.ZipFile(path) as z:
                z.extractall(out.parent)
        # zips usually contain a single top-level folder with the product name
        if not any(out.glob("*_unw_phase.tif")):
            matches = list(out.parent.glob(f"{path.stem}*/"))
            for m in matches:
                if any(m.glob("*_unw_phase.tif")):
                    return m
        return out
    raise ValueError(f"Not a HyP3 product: {path}")


def _open_tif(folder: Path, suffix: str):
    files = list(folder.glob(f"*_{suffix}.tif"))
    if not files:
        return None
    return rioxarray.open_rasterio(files[0], masked=True).squeeze("band", drop=True)


def read_hyp3_insar(path: str | Path, aoi: AOI, sign: float | None = None) -> Interferogram:
    folder = _hyp3_folder(Path(path))
    notes: list[Note] = []
    date1, date2 = _dates(folder.name)
    unw = _open_tif(folder, "unw_phase")
    if unw is None:
        raise ValueError(f"HyP3 product {folder} has no *_unw_phase.tif")
    w, s_, e, n = aoi.bounds_in(unw.rio.crs)
    pad_x, pad_y = (e - w) * 0.05, (n - s_) * 0.05
    clip = dict(minx=w - pad_x, miny=s_ - pad_y, maxx=e + pad_x, maxy=n + pad_y)
    unw = unw.rio.clip_box(**clip)
    corr = _open_tif(folder, "corr")
    coh = corr.rio.clip_box(**clip).values if corr is not None else np.full(unw.shape, np.nan)
    nodata = coh == 0  # HyP3 writes 0 outside the valid footprint
    los_da = _open_tif(folder, "los_disp")
    if los_da is not None and sign is None:
        los = los_da.rio.clip_box(**clip).values  # already metres, positive toward the sensor
    else:
        sgn = HYP3_PHASE_SIGN if sign is None else sign
        los = sgn * unw.values * WAVELENGTH_S1_C / (4 * np.pi)
    los = np.where(nodata, np.nan, los)
    coh = np.where(nodata, np.nan, coh)
    theta = _open_tif(folder, "lv_theta")
    th = theta.rio.clip_box(**clip).values.astype("float64") if theta is not None else None
    if th is not None:
        th[th == 0] = np.nan  # 0 = no data (same handling as MintPy's HyP3 loader)
    if th is not None and np.isfinite(th).any():
        # lv_theta is the look-vector elevation from horizontal, in radians
        inc = float(90.0 - np.degrees(np.nanmean(th)))
    else:
        inc = DEFAULT_INCIDENCE
        notes.append(Note("load", WARN, f"HyP3 {date1}→{date2}: no lv_theta layer; assuming {inc}° incidence. "
                                        "Request 'include_look_vectors' when ordering."))
    x, y = unw.x.values, unw.y.values
    return Interferogram(
        sensor=SENTINEL1, product_type="HYP3_INSAR", name=folder.name, date1=date1, date2=date2,
        los=make_da(los, x, y, unw.rio.crs, "los"), coherence=make_da(coh, x, y, unw.rio.crs, "coherence"),
        wavelength=WAVELENGTH_S1_C, incidence_deg=inc, notes=notes,
    )
