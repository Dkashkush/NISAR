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
