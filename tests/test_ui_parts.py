"""Non-browser tests for the parts behind the web interface."""

from sarcompare.maps import EXAMPLE_PLACES, _zoom_for, bbox_from_drawing, picker_map, results_map
from sarcompare.pipeline import Pipeline


def test_bbox_from_drawing():
    feat = {"geometry": {"type": "Polygon",
                         "coordinates": [[[10, 45], [10.5, 45], [10.5, 45.4], [10, 45.4], [10, 45]]]}}
    assert bbox_from_drawing(feat) == (10.0, 45.0, 10.5, 45.4)
    assert bbox_from_drawing(None) is None


def test_example_places_are_valid_and_small_enough_online():
    from sarcompare.aoi import AOI

    for name, bbox in EXAMPLE_PLACES.items():
        area = AOI.from_bbox(*bbox).area_km2()
        assert 50 < area < 2500, (name, area)


def test_zoom_for():
    assert 9 <= _zoom_for(77.95, 30.2, 78.25, 30.45, 1000) <= 12
    assert _zoom_for(-180, -80, 180, 80) == 2


def test_maps_and_summary_render(demo_cfg):
    from sarcompare.interpret import plain_summary

    result = Pipeline(demo_cfg, on_event=lambda e: None).run(download=False)
    html = results_map(result).get_root().render()
    assert "NISAR (L-band)" in html and "GNSS stations" in html and "invalidateSize" in html
    assert len(picker_map((10, 45, 10.5, 45.4)).get_root().render()) > 1000
    summary = plain_summary(result)
    assert summary and "sinking" in summary[0][1]
    assert sum("more moving area" in t for _, t in summary) <= 1


def test_auto_sentinel1_source_order():
    from sarcompare.config import Config

    na = Pipeline(Config(aoi=[-99.25, 19.25, -98.95, 19.55]), on_event=lambda e: None)
    assert na._s1_sources() == ["ARIA_S1_GUNW", "OPERA_DISP_S1"]
    india = Pipeline(Config(aoi=[77.0, 28.4, 77.2, 28.6]), on_event=lambda e: None)
    assert india._s1_sources() == ["ARIA_S1_GUNW"]
