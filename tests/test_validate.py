from datetime import date, timedelta
from pathlib import Path

import numpy as np
import rioxarray  # noqa: F401
import xarray as xr

from sarcompare import validate as V
from sarcompare.demo import DEMO_BBOX, DEMO_STATIONS, Scene
from sarcompare.aoi import AOI
from sarcompare.pipeline import Pipeline


def test_fit_velocity_recovers_trend():
    rng = np.random.default_rng(0)
    d = [date(2023, 1, 1) + timedelta(days=k) for k in range(900)]
    t = np.arange(900) / 365.25
    y = -0.020 * t + 0.003 * np.sin(2 * np.pi * t) + rng.normal(0, 0.003, t.size)
    v, s = V.fit_velocity(np.array(d), y)
    assert abs(v + 0.020) < 0.001 and 0 < s < 0.002


def test_ngl_readers(demo_cfg):
    folder = Path(demo_cfg.gnss_dir)
    sites = V.read_ngl_site_list(folder / "DataHoldings.txt")
    assert {s["site"] for s in sites} == {s[0] for s in DEMO_STATIONS}
    ts = V.read_tenv3(folder / "DM01.tenv3")
    assert ts["dates"][0] == date(2024, 1, 1) and ts["u"].size > 900
    assert DEMO_BBOX[0] < ts["lon"] < DEMO_BBOX[2]
    # fitted GNSS velocity matches the synthetic truth (+2 mm/yr frame offset)
    scene = Scene(AOI.from_bbox(*DEMO_BBOX))
    stations, skipped = V.ngl_stations(AOI.from_bbox(*DEMO_BBOX), date(2025, 10, 1), date(2026, 8, 1), folder)
    assert len(stations) == len(DEMO_STATIONS) and not skipped
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", scene.crs, always_xy=True)
    for st in stations:
        x, y = tr.transform(st.lon, st.lat)
        truth = scene.vertical_rate(np.array([x]), np.array([y]))[0] * 1000 + 2
        assert abs(st.up_mm_yr - truth) < 4 * max(st.up_sigma_mm_yr, 1.5), (st.site, st.up_mm_yr, truth)


def test_csv_stations(tmp_path):
    f = tmp_path / "pub.csv"
    f.write_text("site,lon,lat,up_m_yr,up_sigma_m_yr\nAAAA,78.1,30.3,-0.05,0.002\nBBBB,378.2,30.4,0.001,\n")
    st = V.csv_stations(f)
    assert st[0].up_mm_yr == -50 and st[0].up_sigma_mm_yr == 2
    assert abs(st[1].lon - 18.2) < 1e-9 and np.isnan(st[1].up_sigma_mm_yr)


def test_demo_validation_and_published_conversions(demo_cfg, tmp_path):
    result = Pipeline(demo_cfg, on_event=lambda e: None).run(download=False)
    g = result.gnss
    assert g is not None and g.stats["NISAR"].n >= 6
    assert g.stats["NISAR"].rmse_mm_yr < 15 and g.stats["NISAR"].r > 0.9
    pv = result.published[0]
    assert pv.stats["NISAR"].r > 0.7 and pv.stats["Sentinel-1"].r > 0.7

    # same published map, but stored as LOS in m/yr with the opposite sign: conversions must undo that
    src = rioxarray.open_rasterio(demo_cfg.published_maps[0]["path"], masked=True).squeeze("band", drop=True)
    inc = 40.0
    los = -(src / 1000) * np.cos(np.radians(inc))
    path = tmp_path / "los.tif"
    los.rio.write_nodata(np.nan).rio.to_raster(path)
    spec = {"path": str(path), "units": "m/yr", "component": "LOS", "sign": -1, "incidence_deg": inc}
    pv2 = V.validate_published(spec, [result.nisar_rate, result.s1_rate], result.reference, result.inside)
    assert abs(pv2.stats["NISAR"].r - pv.stats["NISAR"].r) < 0.01


def test_point_reference(demo_cfg):
    from dataclasses import replace

    cfg = replace(demo_cfg, reference_lonlat=(78.0, 30.22), gnss=None, published_maps=[], name="pointref")
    result = Pipeline(cfg, on_event=lambda e: None).run(download=False)
    ref = result.reference
    assert ref.mode == "point" and abs(ref.lon - 78.0) < 0.01
    v = result.nisar_rate.rate.values[ref.mask]
    assert abs(np.nanmedian(v)) < 1e-6
