"""Reader for NISAR L2 GUNW (Geocoded UNWrapped interferogram) HDF5 products.

The reader locates layers by name rather than by hard-coded path, so it tolerates the
small layout changes between NISAR product-specification versions. Typical layout::

    /science/LSAR/identification/{referenceZeroDopplerStartTime, secondaryZeroDopplerStartTime, orbitPassDirection}
    /science/LSAR/GUNW/grids/frequencyA/centerFrequency
    /science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/{xCoordinates, yCoordinates, projection}
    /science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram/HH/{unwrappedPhase, coherenceMagnitude,
                                                                   connectedComponents, ionospherePhaseScreen}
    /science/LSAR/GUNW/metadata/radarGrid/{incidenceAngle, xCoordinates, yCoordinates, heightAboveEllipsoid}
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import h5py
import numpy as np

from ..aoi import AOI
from ..notes import INFO, WARN, Note
from ..products import NISAR, WAVELENGTH_NISAR_L, pair_dates_from_name
from .base import Interferogram, aoi_slices, apply_fill, decode, make_da

SPEED_OF_LIGHT = 299_792_458.0
# NISAR interferograms are reference x conj(secondary) with the earlier acquisition as reference,
# so positive unwrapped phase means the range grew (motion away from the satellite).
NISAR_PHASE_SIGN = -1.0
DEFAULT_INCIDENCE = 38.0


def _all_datasets(h5: h5py.File) -> list[str]:
    paths: list[str] = []
    h5.visititems(lambda name, obj: paths.append("/" + name) if isinstance(obj, h5py.Dataset) else None)
    return paths


def _named(paths: list[str], name: str) -> list[str]:
    return [p for p in paths if p.rsplit("/", 1)[-1] == name]


def _sibling_upwards(h5: h5py.File, start_group: str, name: str) -> str | None:
    """Find dataset ``name`` in ``start_group`` or any parent group."""
    g = start_group
    while g and g != "/":
        cand = f"{g}/{name}"
        if cand in h5:
            return cand
        g = g.rsplit("/", 1)[0]
    return None


def _epsg(h5: h5py.File, group: str) -> int:
    path = _sibling_upwards(h5, group, "projection")
    if path is None:
        raise ValueError("NISAR GUNW: no 'projection' dataset found")
    ds = h5[path]
    for key in ("epsg_code", "epsg"):
        if key in ds.attrs:
            return int(np.asarray(ds.attrs[key]).ravel()[0])
    return int(np.asarray(ds[()]).ravel()[0])


def _decode_gunw_mask(ds: h5py.Dataset, rows: slice, cols: slice) -> np.ndarray:
    """True where both acquisitions have valid data and the pixel is not water.

    GUNW ``mask`` layout (as decoded by MintPy's prep_nisar): low byte = W*100 + R*10 + S, where W=1 marks
    water and R/S are the reference/secondary subswath numbers (0 = no valid sample).
    """
    raw = ds[rows, cols]
    fill = ds.attrs.get("_FillValue")
    ok = np.ones(raw.shape, bool)
    if fill is not None:
        ok &= raw != np.asarray(fill).ravel()[0]
    bits = np.where(ok, raw.astype(np.int64) & 0xFF, 0)
    return ok & ((bits // 10) % 10 > 0) & (bits % 10 > 0) & (bits // 100 != 1)


def _pick_phase_path(paths: list[str], polarization: str | None) -> str:
    cands = [p for p in _named(paths, "unwrappedPhase") if "pixelOffsets" not in p]
    if not cands:
        raise ValueError("NISAR GUNW: no 'unwrappedPhase' layer found")
    if polarization:
        pol = [p for p in cands if f"/{polarization.upper()}/" in p]
        if not pol:
            raise ValueError(f"NISAR GUNW: polarization {polarization} not in file")
        cands = pol
    cands.sort(key=lambda p: ("frequencyA" not in p, "/HH/" not in p and "/VV/" not in p, p))
    return cands[0]


def _dates(h5: h5py.File, paths: list[str], filename: str):
    ref = _named(paths, "referenceZeroDopplerStartTime")
    sec = _named(paths, "secondaryZeroDopplerStartTime")
    if ref and sec:
        try:
            d1 = datetime.fromisoformat(decode(h5[ref[0]][()])[:19]).date()
            d2 = datetime.fromisoformat(decode(h5[sec[0]][()])[:19]).date()
            return min(d1, d2), max(d1, d2)
        except ValueError:
            pass
    dates = pair_dates_from_name(filename)
    if not dates:
        raise ValueError(f"NISAR GUNW: cannot determine acquisition dates for {filename}")
    return dates


def _incidence(h5: h5py.File, paths: list[str], aoi_xy: tuple[float, float]) -> float | None:
    cube_paths = [p for p in _named(paths, "incidenceAngle") if "radarGrid" in p] or _named(paths, "incidenceAngle")
    if not cube_paths:
        return None
    cube = h5[cube_paths[0]]
    group = cube_paths[0].rsplit("/", 1)[0]
    data = np.asarray(cube[()], dtype="float64")
    if data.ndim == 3:
        hpath = _sibling_upwards(h5, group, "heightAboveEllipsoid")
        k = int(np.argmin(np.abs(h5[hpath][()]))) if hpath else data.shape[0] // 2
        data = data[k]
    xp, yp = _sibling_upwards(h5, group, "xCoordinates"), _sibling_upwards(h5, group, "yCoordinates")
    if xp and yp and data.ndim == 2:
        xs, ys = h5[xp][()], h5[yp][()]
        i = int(np.argmin(np.abs(ys - aoi_xy[1])))
        j = int(np.argmin(np.abs(xs - aoi_xy[0])))
        if data.shape == (ys.size, xs.size) and np.isfinite(data[i, j]):
            return float(data[i, j])
    val = float(np.nanmean(data))
    return val if np.isfinite(val) else None


def read_nisar_gunw(path: str | Path, aoi: AOI, polarization: str | None = None,
                    apply_ionosphere: bool = False, sign: float | None = None) -> Interferogram:
    path = Path(path)
    notes: list[Note] = []
    with h5py.File(path, "r") as h5:
        paths = _all_datasets(h5)
        phase_path = _pick_phase_path(paths, polarization)
        pol_group = phase_path.rsplit("/", 1)[0]
        pol = pol_group.rsplit("/", 1)[-1]
        xpath = _sibling_upwards(h5, pol_group, "xCoordinates")
        ypath = _sibling_upwards(h5, pol_group, "yCoordinates")
        if not (xpath and ypath):
            raise ValueError("NISAR GUNW: coordinate arrays not found")
        epsg = _epsg(h5, pol_group)
        x, y = h5[xpath][()], h5[ypath][()]
        bounds = aoi.bounds_in(f"EPSG:{epsg}")
        rows, cols = aoi_slices(x, y, bounds)

        phase_ds = h5[phase_path]
        phase = apply_fill(phase_ds[rows, cols], phase_ds.attrs.get("_FillValue"))
        coh_path = f"{pol_group}/coherenceMagnitude"
        coh = apply_fill(h5[coh_path][rows, cols], h5[coh_path].attrs.get("_FillValue")) if coh_path in h5 \
            else np.full_like(phase, np.nan)
        mask_path = _sibling_upwards(h5, pol_group, "mask")
        if mask_path is not None:
            valid = _decode_gunw_mask(h5[mask_path], rows, cols)
            if valid.shape == phase.shape:
                phase = np.where(valid, phase, np.nan)
        cc_path = f"{pol_group}/connectedComponents"
        if cc_path in h5:
            cc = h5[cc_path][rows, cols]
            phase = np.where(cc == 0, np.nan, phase)
        iono_path = f"{pol_group}/ionospherePhaseScreen"
        iono_std_mm = None
        if iono_path in h5:
            iono = apply_fill(h5[iono_path][rows, cols], h5[iono_path].attrs.get("_FillValue"))
            if iono.shape == phase.shape:
                iono_std_mm = float(np.nanstd(iono) * WAVELENGTH_NISAR_L / (4 * np.pi) * 1000)
                if apply_ionosphere:
                    phase = phase - iono

        freq_paths = [p for p in _named(paths, "centerFrequency") if "frequencyA" in p] or _named(paths, "centerFrequency")
        wavelength = WAVELENGTH_NISAR_L
        if freq_paths:
            f = float(np.asarray(h5[freq_paths[0]][()]).ravel()[0])
            if f > 1e8:
                wavelength = SPEED_OF_LIGHT / f
        date1, date2 = _dates(h5, paths, path.name)
        cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
        inc = _incidence(h5, paths, (cx, cy))
        direction = None
        dir_paths = _named(paths, "orbitPassDirection")
        if dir_paths:
            direction = decode(h5[dir_paths[0]][()]).upper() or None

    if inc is None:
        inc = DEFAULT_INCIDENCE
        notes.append(Note("load", WARN, f"NISAR {date1}→{date2}: no incidence-angle layer; assuming {inc}°."))
    s = NISAR_PHASE_SIGN if sign is None else sign
    los = s * phase * wavelength / (4 * np.pi)
    if iono_std_mm is not None:
        verb = "removed" if apply_ionosphere else "provided (not applied)"
        lvl = WARN if iono_std_mm > 10 and not apply_ionosphere else INFO
        notes.append(Note("load", lvl, f"NISAR {date1}→{date2}: ionospheric phase screen {verb}; it varies by "
                                       f"{iono_std_mm:.1f} mm (1σ) over the AOI. L-band is ~20× more sensitive "
                                       "to the ionosphere than C-band."))
    return Interferogram(
        sensor=NISAR, product_type="GUNW", name=path.name, date1=date1, date2=date2,
        los=make_da(los, x[cols], y[rows], f"EPSG:{epsg}", "los"),
        coherence=make_da(coh, x[cols], y[rows], f"EPSG:{epsg}", "coherence"),
        wavelength=wavelength, incidence_deg=inc, flight_direction=direction, notes=notes,
        extras={"polarization": pol, "ionosphere_std_mm": iono_std_mm},
    )
