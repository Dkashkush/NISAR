import json
from pathlib import Path

import numpy as np

from sarcompare.demo import DEMO_BBOX, Scene
from sarcompare.aoi import AOI
from sarcompare.pipeline import Pipeline
from sarcompare.products import NISAR, SENTINEL1
from sarcompare.readers import read_product


def test_readers_recover_truth(tmp_path):
    """Noise-free synthetic pairs: each reader must return toward-positive LOS metres at the right scale."""
    from datetime import date

    from sarcompare.demo import write_aria_gunw, write_nisar_gunw
    from sarcompare.products import Product

    aoi = AOI.from_bbox(*DEMO_BBOX)
    scene = Scene(aoi)
    d1, d2 = date(2025, 10, 10), date(2026, 1, 8)
    files = {
        NISAR: (tmp_path / f"NISAR_L2_PR_GUNW_{d1:%Y%m%d}T000000_{d2:%Y%m%d}T000000.h5", write_nisar_gunw, "GUNW"),
        SENTINEL1: (tmp_path / f"S1-GUNW-A-R-114-tops-{d2:%Y%m%d}_{d1:%Y%m%d}-x.nc", write_aria_gunw, "ARIA_GUNW"),
    }
    for sensor, (path, writer, ptype) in files.items():
        writer(path, scene, d1, d2, noise=0.0)
        ifg = read_product(Product(sensor, ptype, path.name, local_path=path), aoi, nisar_apply_ionosphere=True)
        assert (ifg.date1, ifg.date2) == (d1, d2)
        da = ifg.los if sensor == NISAR else ifg.los.rio.reproject(scene.crs)
        X, Y = np.meshgrid(da.x.values, da.y.values)
        truth = scene.vertical_rate(X, Y) * ifg.span_years * np.cos(np.radians(ifg.incidence_deg))
        ok = np.isfinite(da.values) & np.isfinite(truth)
        strong = ok & (truth < 0.6 * np.nanmin(truth))  # centre of the subsidence bowl
        quiet = ok & (np.abs(truth) < 0.05 * np.abs(np.nanmin(truth)))
        measured = np.median(da.values[strong]) - np.median(da.values[quiet])
        expected = np.median(truth[strong]) - np.median(truth[quiet])
        assert measured < 0, (sensor, measured)  # subsidence comes out negative => sign handled
        assert 0.9 < measured / expected < 1.1, (sensor, measured, expected)


def test_demo_end_to_end(demo_cfg):
    events = []
    result = Pipeline(demo_cfg, on_event=events.append).run(download=False)
    c = result.comparison
    assert c.pearson_r > 0.5
    assert 0.7 < c.slope < 1.4
    assert c.coverage_a > 0.9 and c.coverage_b < 0.8  # L-band keeps the forest, C-band loses it
    assert c.only_a > 0.2
    # the subsidence bowl is found by both sensors
    assert any(h.peak_mm_yr < -50 and h.seen_by_other == "yes" for h in c.hotspots)
    # the landslide under forest is found by NISAR only
    assert any(h.sensor == NISAR and h.seen_by_other == "no data" for h in c.hotspots)
    stages = {e.stage for e in events}
    assert {"search", "load", "stack", "reference", "compare", "report"} <= stages
    report = Path(result.outputs["report"]).read_text()
    assert "Synthetic demo data" in report and "data:image/png;base64" in report
    metrics = json.loads(Path(result.outputs["metrics"]).read_text())
    assert metrics["comparison"]["n_joint"] > 0
    for key in ("nisar_vertical_rate_m_per_yr", "s1_vertical_rate_m_per_yr"):
        assert Path(result.outputs[key]).exists()


def test_sign_override_is_flagged(demo_cfg):
    from dataclasses import replace
    cfg = replace(demo_cfg, s1_sign=-1.0, name="flipped")
    result = Pipeline(cfg, on_event=lambda e: None).run(download=False)
    assert result.comparison.sign_suspect
    assert any("sign-convention" in n.text for n in result.notes)
