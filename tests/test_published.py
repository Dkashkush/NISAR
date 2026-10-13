"""Published-results checks: unit handling, literature values, references in the report."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sarcompare import validate as V
from sarcompare.pipeline import Pipeline


@pytest.mark.parametrize("spec,expected", [
    ({"units": "mm/yr"}, 1.0), ({"units": "cm/yr"}, 10.0), ({"units": "m/y"}, 1000.0),
    ({"units": "ft/yr"}, 304.8), ({"units": "in/yr"}, 25.4),
    ({"units": "ft", "period_years": 2}, 152.4), ({"units": "mm", "period_years": 4}, 0.25),
])
def test_units(spec, expected):
    assert V._units_to_mm_per_yr(spec) == pytest.approx(expected)


def test_units_errors():
    with pytest.raises(ValueError):
        V._units_to_mm_per_yr({"units": "furlongs/yr"})
    with pytest.raises(ValueError):
        V._units_to_mm_per_yr({"units": "cm"})  # total displacement needs period_years


def test_parse_published_value_los_and_range():
    v = V.parse_published_value({"citation": "X", "lon": 370, "lat": 30, "rate_mm_yr": -30, "component": "LOS",
                                 "incidence_deg": 60, "rate_min_mm_yr": -20, "rate_max_mm_yr": -40})
    assert v["lon"] == pytest.approx(10) and v["rate"] == pytest.approx(-60)
    assert (v["lo"], v["hi"]) == (pytest.approx(-80), pytest.approx(-40))


def test_literature_checks_on_demo(demo_cfg):
    result = Pipeline(demo_cfg, on_event=lambda e: None).run(download=False)
    lit = result.literature
    assert len(lit) == 2
    city, slide = lit
    assert city.verdict == {"NISAR": "consistent", "Sentinel-1": "consistent"}
    assert slide.verdict["NISAR"] == "consistent" and slide.verdict["Sentinel-1"] == "no data"  # forest
    assert all(city.absolute.values())  # tied to GNSS
    report = Path(result.outputs["report"]).read_text()
    assert "Published studies" in report and "Synthetic Study A" in report
    assert "References and data credits" in report


def test_literature_detects_disagreement_and_outside(demo_cfg):
    base = Pipeline(replace(demo_cfg, name="lit2"), on_event=lambda e: None).run(download=False)
    city = base.literature[0]
    values = [{"citation": "Wrong", "lon": city.lon, "lat": city.lat, "rate_mm_yr": +40},
              {"citation": "Far away", "lon": 10.0, "lat": 45.0, "rate_mm_yr": -10}]
    checks = V.validate_literature(values, [base.nisar_rate, base.s1_rate], base.gnss)
    assert set(checks[0].verdict.values()) == {"differs"}
    assert set(checks[1].verdict.values()) == {"outside area"}
    assert np.isnan(checks[1].insar["NISAR"])


def test_published_peak_is_compared_with_the_fastest_pixels():
    import numpy as np
    from datetime import date
    from pyproj import Transformer
    from sarcompare.processing import RateMap
    from sarcompare.readers.base import make_da
    from sarcompare.validate import validate_literature

    x = 480000 + np.arange(80) * 90.0
    y = 2150000 + np.arange(80)[::-1] * 90.0
    X, Y = np.meshgrid(x, y)
    bowl = -0.30 * np.exp(-((X - x.mean()) ** 2 + (Y - y.mean()) ** 2) / (2 * 1500.0 ** 2))  # peak -300 mm/yr
    da = make_da(bowl, x, y, "EPSG:32614", "rate")
    rm = RateMap("NISAR", da, da * 0 + 0.7, da * 0 + 9, 9, 120, date(2025, 10, 1), date(2026, 2, 1), 38, 0.24,
                 noise_rate=0.018)
    lon, lat = Transformer.from_crs("EPSG:32614", "EPSG:4326", always_xy=True).transform(x.mean(), y.mean())
    common = {"citation": "Test", "lon": lon, "lat": lat, "radius_km": 3, "rate_mm_yr": -300}
    median, peak = validate_literature([common, {**common, "stat": "peak"}], [rm])
    assert peak.insar["NISAR"] < -280 and peak.verdict["NISAR"] == "consistent"
    assert median.insar["NISAR"] > -200 and median.verdict["NISAR"] == "differs"  # a median is not a peak


def test_egms_tiles_are_recognised_and_east_west_is_compared(demo_cfg, tmp_path):
    """EGMS Ortho tiles: settings come from the file name; an east-west tile is compared with east-west maps."""
    import shutil
    from dataclasses import replace

    import numpy as np
    import rioxarray

    from sarcompare.pipeline import Pipeline
    from sarcompare.validate import egms_defaults

    assert egms_defaults("EGMS_L3_E32N26_100km_U_2019_2023_1.tiff")["component"] == "vertical"
    e = egms_defaults("x/EGMS_L3_E32N26_100km_E_2019_2023_1.tiff")
    assert e["component"] == "east-west" and e["period"] == "2019-2023" and "EGMS" in e["citation"]
    assert egms_defaults("other_map.tif") == {}

    src = demo_cfg.published_maps[0]["path"]
    up = tmp_path / "EGMS_L3_E99N99_100km_U_2019_2023_1.tiff"
    shutil.copy(src, up)
    da = rioxarray.open_rasterio(src, masked=True)
    east = tmp_path / "EGMS_L3_E99N99_100km_E_2019_2023_1.tiff"
    da.copy(data=np.zeros_like(da.values)).rio.to_raster(east)  # the demo has no sideways motion
    cfg = replace(demo_cfg, name="egms", published_maps=[{"path": str(up)}, {"path": str(east)}],
                  published_values=[])
    result = Pipeline(cfg, on_event=lambda ev: None).run(download=False)
    vert, ew = result.published
    assert vert.label == "EGMS Ortho vertical 2019-2023" and vert.component == "vertical"
    assert vert.stats["NISAR"].r > 0.7
    assert ew.component == "east-west" and set(ew.stats) == {"Sentinel-1"}  # only Sentinel-1 has both directions
    assert abs(ew.stats["Sentinel-1"].bias_mm_yr) < 10
    report = (cfg.run_dir / "report.html").read_text()
    assert "European Ground Motion Service" in report
