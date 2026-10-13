from pathlib import Path
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
        assert 50 < area < 1000, (name, area)  # fits the suggested Streamlit Cloud limit (1,000 km²)


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


def test_packages_txt_has_only_package_names():
    """Streamlit Community Cloud passes every word of packages.txt to apt-get, comments included."""
    from pathlib import Path

    lines = [l.strip() for l in (Path(__file__).parents[1] / "packages.txt").read_text().splitlines() if l.strip()]
    assert lines and all(" " not in l and not l.startswith("#") for l in lines)


def test_find_place_and_box_around():
    from sarcompare.aoi import AOI
    from sarcompare.maps import box_around, find_place

    def fake(url, params, headers):
        assert "nominatim" in url and params["q"] == "Venice" and "OpenSAR" in headers["User-Agent"]
        return [{"display_name": "Venice, Italy", "lat": "45.4371", "lon": "12.3326",
                 "boundingbox": ["45.38", "45.48", "12.28", "12.40"]}, {"bad": "row"}]

    places = find_place("Venice", fetch=fake)
    assert len(places) == 1 and places[0]["bbox"] == (12.28, 45.38, 12.40, 45.48)
    assert find_place("  ", fetch=fake) == []
    # the place's own outline when it is a sensible size, else a 20 km box (or smaller, to fit the limit)
    assert box_around(45.4371, 12.3326, places[0]["bbox"]) == (12.28, 45.38, 12.4, 45.48)
    big = box_around(28.6, 77.2, (60.0, 5.0, 100.0, 40.0), max_km2=900)
    assert 350 < AOI.from_bbox(*big).area_km2() < 450
    small = box_around(28.6, 77.2, None, max_km2=100)
    assert AOI.from_bbox(*small).area_km2() <= 101

    def broken(url, params, headers):
        raise ConnectionError("offline")

    try:
        find_place("Venice", fetch=broken)
        raise AssertionError("expected OSError")
    except OSError:
        pass


def test_place_search_in_the_app(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import sarcompare.maps as maps

    monkeypatch.setattr(maps, "find_place", lambda q, **kw: [
        {"name": "Venice, Italy", "lat": 45.4371, "lon": 12.3326, "bbox": (12.28, 45.38, 12.40, 45.48)}])
    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=60)
    at.run()
    next(r for r in at.radio if "Analyse a real place" in r.options).set_value("Analyse a real place").run()
    next(t for t in at.text_input if t.label == "Search a place").input("Venice").run()
    next(b for b in at.button if b.label == "Search").click().run()
    assert not at.exception
    assert at.session_state["view"] == (45.4371, 12.3326, 11) and at.session_state["bbox"] is None
    next(b for b in at.button if b.label == "Use a box around this place").click().run()
    assert at.session_state["bbox"] == (12.28, 45.38, 12.4, 45.48)


def _single_results_page(data_dir, out_dir, empty_dir):
    from dataclasses import replace

    from sarcompare.demo import demo_config
    from sarcompare.gui import show_results
    from sarcompare.pipeline import Pipeline

    cfg = replace(demo_config(data_dir, out_dir), s1_dir=empty_dir, name="nisar_only")
    show_results(Pipeline(cfg, on_event=lambda e: None).run(download=False))


def test_results_page_with_one_satellite(demo_cfg, tmp_path):
    """The results page works when Sentinel-1 has no data for the area (NISAR alone)."""
    from streamlit.testing.v1 import AppTest

    (tmp_path / "empty").mkdir()
    data_dir = Path(demo_cfg.nisar_dir).parent
    at = AppTest.from_function(_single_results_page, default_timeout=300,
                               args=(str(data_dir), str(tmp_path / "out"), str(tmp_path / "empty")))
    at.run()
    assert not at.exception, at.exception
    labels = {m.label: m.value for m in at.metric}
    assert labels.get("Sentinel-1") == "No data" and "Area NISAR can measure" in labels
    text = " ".join(md.value for md in at.markdown) + " ".join(i.value for i in at.info)
    assert "only NISAR was used" in text and "nothing to compare it with" in text
