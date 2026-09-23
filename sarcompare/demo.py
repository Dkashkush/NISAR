"""Synthetic demo: writes fake-but-realistic NISAR GUNW and ARIA S1 GUNW files, then runs the real pipeline.

The scene has a subsiding city (groundwater pumping), a landslide on a forested hillslope, tropospheric
noise that differs on every date, an L-band ionospheric ramp, and land cover that decorrelates C-band
in the forest. It exists so you can try the whole workflow without credentials or downloads, and so the
readers are exercised on files with the same internal layout as the real products.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import h5py
import numpy as np
from pyproj import Transformer
from scipy import ndimage

from .aoi import AOI
from .config import Config
from .products import WAVELENGTH_NISAR_L, WAVELENGTH_S1_C

DEMO_BBOX = (77.95, 30.20, 78.25, 30.45)
NISAR_INC = (36.0, 40.0)  # incidence varies across the scene
S1_INC = 34.0


def _smooth_noise(rng, shape, scale_px, sigma):
    f = ndimage.gaussian_filter(rng.standard_normal(shape), scale_px, mode="wrap")
    return f / (f.std() or 1) * sigma


class Scene:
    """Truth fields defined on continuous UTM coordinates."""

    def __init__(self, aoi: AOI, seed: int = 7):
        self.aoi = aoi
        self.crs = aoi.utm_crs()
        self.rng = np.random.default_rng(seed)
        w, s, e, n = aoi.bounds_in(self.crs)
        self.w, self.s, self.e, self.n = w, s, e, n
        cx, cy = (w + e) / 2, (s + n) / 2
        self.city = (cx + 0.22 * (e - w), cy - 0.2 * (n - s))
        self.slide = (cx - 0.2 * (e - w), cy + 0.28 * (n - s))
        self.lake = (cx - 0.3 * (e - w), cy - 0.3 * (n - s))
        # land cover on a coarse helper grid: forest in the north, city around the bowl
        self.lc_res = 100.0
        self.lx = np.arange(w - 6000, e + 6000, self.lc_res)
        self.ly = np.arange(n + 6000, s - 6000, -self.lc_res)
        X, Y = np.meshgrid(self.lx, self.ly)
        edge = cy + 0.05 * (n - s) + _smooth_noise(self.rng, X.shape, 25, 2500)
        self.forest = Y > edge
        self.urban = np.hypot(X - self.city[0], Y - self.city[1]) < 5000

    def vertical_rate(self, X, Y):
        """m/yr, negative = subsidence."""
        bowl = -0.10 * np.exp(-((X - self.city[0]) ** 2 + (Y - self.city[1]) ** 2) / (2 * 3000.0 ** 2))
        slide = -0.06 * np.exp(-((X - self.slide[0]) ** 2 / (2 * 900.0 ** 2) + (Y - self.slide[1]) ** 2 /
                                 (2 * 1500.0 ** 2)))
        tect = 0.004 * (X - X.mean()) / 20000
        return bowl + slide + tect

    def cover(self, X, Y):
        """Land-cover class at arbitrary coordinates: 0 fields, 1 forest, 2 urban."""
        i = np.clip(((self.ly[0] - Y) / self.lc_res).astype(int), 0, self.ly.size - 1)
        j = np.clip(((X - self.lx[0]) / self.lc_res).astype(int), 0, self.lx.size - 1)
        return np.where(self.urban[i, j], 2, np.where(self.forest[i, j], 1, 0))

    def coherence(self, X, Y, sensor: str, span_days: int):
        c = self.cover(X, Y)
        if sensor == "NISAR":
            base = np.choose(c, [0.62, 0.55, 0.88])
            decay = np.exp(-span_days / 400)
        else:
            base = np.choose(c, [0.42, 0.13, 0.82])
            decay = np.exp(-span_days / 160)
        coh = base * (0.75 + 0.25 * decay) + self.rng.normal(0, 0.05, X.shape)
        return np.clip(coh, 0.02, 0.98)

    def atmosphere(self, shape, sigma_m):
        return _smooth_noise(self.rng, shape, max(shape) / 6, sigma_m) + _smooth_noise(self.rng, shape, 3, sigma_m / 3)


def _pairs(first: date, n: int, span: int) -> list[tuple[date, date]]:
    return [(first + timedelta(days=k * span), first + timedelta(days=(k + 1) * span)) for k in range(n)]


def write_nisar_gunw(path: Path, scene: Scene, d1: date, d2: date, res=80.0, noise: float = 1.0):
    x = np.arange(scene.w - 5000, scene.e + 5000, res)
    y = np.arange(scene.n + 5000, scene.s - 5000, -res)
    X, Y = np.meshgrid(x, y)
    span = (d2 - d1).days
    inc = NISAR_INC[0] + (NISAR_INC[1] - NISAR_INC[0]) * (X - X.min()) / (X.max() - X.min())
    los = scene.vertical_rate(X, Y) * span / 365.25 * np.cos(np.radians(inc))
    los += noise * scene.atmosphere(X.shape, 0.003)
    coh = scene.coherence(X, Y, "NISAR", span)
    los += noise * scene.rng.normal(0, 1, X.shape) * 0.004 * np.sqrt(np.maximum(1 - coh, 0))
    k = 4 * np.pi / WAVELENGTH_NISAR_L
    iono = (_smooth_noise(scene.rng, X.shape, max(X.shape) / 2, 1.0) + 0.8 * (Y - Y.mean()) / (Y.max() - Y.min()))
    iono = iono * 0.012 * k  # ~cm-level ionospheric phase ramp, in radians
    phase = -k * los + iono  # NISAR convention: positive phase = motion away from the satellite
    cc = np.where(coh < 0.2, 0, 1).astype("uint16")
    phase = np.where(cc == 0, np.nan, phase)

    g = "/science/LSAR/GUNW/grids/frequencyA"
    with h5py.File(path, "w") as h5:
        ident = h5.create_group("/science/LSAR/identification")
        ident["referenceZeroDopplerStartTime"] = np.bytes_(f"{d1}T12:30:00.000000")
        ident["secondaryZeroDopplerStartTime"] = np.bytes_(f"{d2}T12:30:00.000000")
        ident["orbitPassDirection"] = np.bytes_("Ascending")
        h5[f"{g}/centerFrequency"] = 1.2575e9
        u = h5.create_group(f"{g}/unwrappedInterferogram")
        u["xCoordinates"], u["yCoordinates"] = x, y
        proj = u.create_dataset("projection", data=np.uint32(scene.crs.to_epsg()))
        proj.attrs["epsg_code"] = scene.crs.to_epsg()
        # GUNW validity mask: W*100 + R*10 + S (water flag, reference/secondary subswath); a small lake is water
        lake = np.hypot(X - scene.lake[0], Y - scene.lake[1]) < 1200
        u.create_dataset("mask", data=np.where(lake, 111, 11).astype("uint8"), compression="gzip")
        hh = u.create_group("HH")
        hh.create_dataset("unwrappedPhase", data=phase.astype("float32"), compression="gzip")
        hh.create_dataset("coherenceMagnitude", data=coh.astype("float32"), compression="gzip")
        hh.create_dataset("connectedComponents", data=cc, compression="gzip")
        hh.create_dataset("ionospherePhaseScreen", data=iono.astype("float32"), compression="gzip")
        rg = h5.create_group("/science/LSAR/GUNW/metadata/radarGrid")
        cx, cy = x[::25], y[::25]
        CX, _ = np.meshgrid(cx, cy)
        cube_inc = NISAR_INC[0] + (NISAR_INC[1] - NISAR_INC[0]) * (CX - x.min()) / (x.max() - x.min())
        rg["xCoordinates"], rg["yCoordinates"] = cx, cy
        rg["heightAboveEllipsoid"] = np.array([-500.0, 0.0, 1000.0, 3000.0])
        rg["incidenceAngle"] = np.stack([cube_inc + d for d in (0.05, 0.0, -0.1, -0.3)]).astype("float32")


def write_aria_gunw(path: Path, scene: Scene, d1: date, d2: date, res_deg=1 / 1200, noise: float = 1.0):
    w, s, e, n = scene.aoi.bounds
    lon = np.arange(w - 0.05, e + 0.05, res_deg)
    lat = np.arange(n + 0.05, s - 0.05, -res_deg)
    LON, LAT = np.meshgrid(lon, lat)
    X, Y = Transformer.from_crs("EPSG:4326", scene.crs, always_xy=True).transform(LON, LAT)
    span = (d2 - d1).days
    los = scene.vertical_rate(X, Y) * span / 365.25 * np.cos(np.radians(S1_INC))
    los += noise * scene.atmosphere(X.shape, 0.003)
    coh = scene.coherence(X, Y, "S1", span)
    los += noise * scene.rng.normal(0, 1, X.shape) * 0.004 * np.sqrt(np.maximum(1 - coh, 0))
    phase = 4 * np.pi / WAVELENGTH_S1_C * los  # ARIA convention: positive phase = toward the satellite
    cc = np.where(coh < 0.25, 0, 1).astype("int16")
    phase = np.where(cc == 0, 0.0, phase)
    g = "/science/grids/data"
    with h5py.File(path, "w") as h5:
        h5[f"{g}/longitude"], h5[f"{g}/latitude"] = lon, lat
        up = h5.create_dataset(f"{g}/unwrappedPhase", data=phase.astype("float32"), compression="gzip")
        up.attrs["_FillValue"] = np.float32(0.0)
        h5.create_dataset(f"{g}/coherence", data=coh.astype("float32"), compression="gzip")
        h5.create_dataset(f"{g}/connectedComponents", data=cc, compression="gzip")
        h5["/science/radarMetaData/wavelength"] = WAVELENGTH_S1_C
        h5["/science/grids/imagingGeometry/incidenceAngle"] = np.full((3, 4, 4), S1_INC, dtype="float32")


def make_demo_data(root: str | Path = "data/demo", seed: int = 7) -> tuple[Path, Path]:
    """Write the synthetic product folders; returns (nisar_dir, s1_dir)."""
    root = Path(root)
    nisar_dir, s1_dir = root / "nisar", root / "sentinel1"
    nisar_dir.mkdir(parents=True, exist_ok=True)
    s1_dir.mkdir(parents=True, exist_ok=True)
    scene = Scene(AOI.from_bbox(*DEMO_BBOX, name="demo"), seed)
    for i, (d1, d2) in enumerate(_pairs(date(2025, 10, 10), 6, 48)):
        # official pattern: NISAR_L2_PR_GUNW_<refcycle>_<track>_<dir>_<frame>_<seccycle>_<bw>_<pol>_<4 times>_<crid>_...
        name = (f"NISAR_L2_PR_GUNW_{4 * i + 5:03d}_120_A_045_{4 * i + 9:03d}_4000_SH_{d1:%Y%m%d}T123000_"
                f"{d1:%Y%m%d}T123020_{d2:%Y%m%d}T123000_{d2:%Y%m%d}T123020_X05010_N_P_J_001.h5")
        write_nisar_gunw(nisar_dir / name, scene, d1, d2)
    for d1, d2 in _pairs(date(2025, 10, 4), 6, 48):
        name = f"S1-GUNW-A-R-114-tops-{d2:%Y%m%d}_{d1:%Y%m%d}-003012-00078E_00030N-PP-a1b2-v3_0_1.nc"
        write_aria_gunw(s1_dir / name, scene, d1, d2)
    return nisar_dir, s1_dir


def demo_config(root: str | Path = "data/demo", output_dir: str = "outputs") -> Config:
    nisar_dir, s1_dir = make_demo_data(root)
    return Config(name="demo_synthetic", aoi=list(DEMO_BBOX), nisar_source="LOCAL", nisar_dir=str(nisar_dir),
                  s1_source="LOCAL", s1_dir=str(s1_dir), max_pairs=6, output_dir=output_dir,
                  extra={"synthetic": True})
