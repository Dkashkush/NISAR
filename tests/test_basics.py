from datetime import date

import pytest

from sarcompare.aoi import AOI
from sarcompare.config import Config
from sarcompare.products import NISAR, Product, pair_dates_from_name
from sarcompare.search import classify_file, select_pairs


@pytest.mark.parametrize("name,expected", [
    ("NISAR_L2_PR_GUNW_004_151_A_011_005_4000_SH_20251108T155041_20251108T155058_20251120T155041_"
     "20251120T155058_X05009_N_F_J_001.h5", (date(2025, 11, 8), date(2025, 11, 20))),
    # acquisition crossing midnight: the reference *end* time is the next day
    ("NISAR_L2_PR_GUNW_004_151_A_011_005_4000_SH_20251108T235950_20251109T000008_20251120T235950_"
     "20251121T000008_X05009_N_F_J_001.h5", (date(2025, 11, 8), date(2025, 11, 20))),
    ("S1-GUNW-A-R-064-tops-20210723_20210711-015000-00119W_00033N-PP-6267-v2_0_4.nc",
     (date(2021, 7, 11), date(2021, 7, 23))),
    ("S1AA_20200101T010203_20200113T010204_VVP012_INT80_G_ueF_1A2B", (date(2020, 1, 1), date(2020, 1, 13))),
    ("S1_136231_IW2_20200604_20200616_VV_INT80_10C8", (date(2020, 6, 4), date(2020, 6, 16))),
    ("OPERA_L3_DISP-S1_IW_F11115_VV_20160705T140755Z_20160729T140756Z_v1.0_20250101T000000Z",
     (date(2016, 7, 5), date(2016, 7, 29))),
])
def test_pair_dates(name, expected):
    assert pair_dates_from_name(name) == expected


def test_aoi_parsing():
    a = AOI.parse([10, 45, 10.5, 45.5])
    assert a.utm_crs().to_epsg() == 32632
    assert 1500 < a.area_km2() < 2500
    b = AOI.parse("POLYGON((10 45,10.5 45,10.5 45.5,10 45.5,10 45))")
    assert b.bounds == a.bounds
    c = AOI.parse('{"type":"Polygon","coordinates":[[[10,45],[10.5,45],[10.5,45.5],[10,45.5],[10,45]]]}')
    assert c.bounds == a.bounds
    with pytest.raises(ValueError):
        AOI.from_bbox(11, 45, 10, 46)


def test_config_validation():
    with pytest.raises(ValueError):
        Config(aoi=[0, 0, 1, 1], s1_source="NOPE")
    with pytest.raises(ValueError):
        Config(aoi=[0, 0, 1, 1], nisar_source="LOCAL")
    cfg = Config(aoi=[0, 0, 1, 1], start="2025-10-01")
    assert cfg.start == date(2025, 10, 1)


def test_classify(tmp_path):
    assert classify_file(tmp_path / "NISAR_L2_PR_GUNW_x.h5") == (NISAR, "GUNW")
    assert classify_file(tmp_path / "S1-GUNW-A-R-1.nc")[1] == "ARIA_GUNW"
    assert classify_file(tmp_path / "OPERA_L3_DISP-S1_x.nc")[1] == "DISP-S1"
    assert classify_file(tmp_path / "readme.txt") is None


def _p(d1, d2, track=1, direction="ASCENDING"):
    return Product(NISAR, "GUNW", f"{d1}_{d2}", date1=d1, date2=d2, track=track, flight_direction=direction)


def test_select_pairs_prefers_chain_and_single_geometry():
    d = date(2025, 10, 1)
    from datetime import timedelta as td
    prods = [_p(d + td(12 * k), d + td(12 * (k + 1))) for k in range(8)]
    prods += [_p(d, d + td(24)), _p(d, d + td(300))]  # overlapping + too long
    prods += [_p(d, d + td(12), track=2, direction="DESCENDING")]  # other geometry
    sel, notes = select_pairs(prods, max_pairs=4, min_span=6, max_span=96)
    assert len(sel) == 4
    assert all(p.track == 1 for p in sel)
    assert all(sel[i].date2 <= sel[i + 1].date1 for i in range(len(sel) - 1))
    assert any("viewing geometries" in n.text for n in notes)


class _FakeResult:
    def __init__(self, props):
        self.properties = props


def test_asf_result_conversion():
    from sarcompare.search import _to_product

    r = _FakeResult({
        "sceneName": "NISAR_L2_PR_GUNW_005_120_A_045_4020_SH_20251010T123000_20251010T123020_20251022T123000_"
                     "20251022T123020_X05010_N_P_J_001",
        "url": "https://nisar.asf.earthdatacloud.nasa.gov/x/NISAR_L2_PR_GUNW_005_120_A_045_4020_SH_20251010T123000_"
               "20251010T123020_20251022T123000_20251022T123020_X05010_N_P_J_001.h5",
        "flightDirection": "Ascending", "pathNumber": 120, "frameNumber": 45,
        "bytes": {"a.h5": {"bytes": 250_000_000, "format": "HDF5"}},
    })
    p = _to_product(r, NISAR, "GUNW")
    assert (p.date1, p.date2) == (date(2025, 10, 10), date(2025, 10, 22))
    assert p.flight_direction == "ASCENDING" and p.track == 120 and p.size_mb == 250


def test_parse_name_geometry():
    from sarcompare.products import parse_name

    n = parse_name("NISAR_L2_PR_GUNW_004_151_D_011_005_4000_SH_20251108T155041_20251108T155058_"
                   "20251120T155041_20251120T155058_X05009_N_F_J_001.h5")
    assert (n["direction"], n["track"], n["frame"]) == ("DESCENDING", 151, 11)
    a = parse_name("S1-GUNW-A-R-064-tops-20210723_20210711-015000-00119W_00033N-PP-6267-v2_0_4.nc")
    assert (a["direction"], a["track"]) == ("ASCENDING", 64)


def test_scan_local_keeps_geometries_apart_and_dedupes(tmp_path):
    from sarcompare.search import scan_local
    from sarcompare.products import SENTINEL1

    for name in ("NISAR_L2_PR_GUNW_004_151_A_011_005_4000_SH_20251108T155041_20251108T155058_20251120T155041_"
                 "20251120T155058_X05009_N_F_J_001.h5",
                 "NISAR_L2_PR_GUNW_004_020_D_050_005_4000_SH_20251110T010000_20251110T010018_20251122T010000_"
                 "20251122T010018_X05009_N_F_J_001.h5"):
        (tmp_path / name).write_bytes(b"")
    prods = scan_local(tmp_path, NISAR)
    assert {p.geometry_key for p in prods} == {("ASCENDING", 151), ("DESCENDING", 20)}
    hyp3 = tmp_path / "hyp3"
    hyp3.mkdir()
    stem = "S1AA_20200101T010203_20200113T010204_VVP012_INT80_G_ueF_1A2B"
    (hyp3 / f"{stem}.zip").write_bytes(b"")
    (hyp3 / stem).mkdir()
    (hyp3 / stem / f"{stem}_unw_phase.tif").write_bytes(b"")
    s1 = scan_local(hyp3, SENTINEL1)
    assert len(s1) == 1 and s1[0].local_path.is_dir()


def test_gunw_mask_decoding():
    import h5py
    import numpy as np
    from sarcompare.readers.nisar import _decode_gunw_mask

    vals = np.array([[11, 111, 10], [1, 0, 255]], dtype="uint8")
    with h5py.File("mem.h5", "w", driver="core", backing_store=False) as h5:
        ds = h5.create_dataset("mask", data=vals)
        ds.attrs["_FillValue"] = np.uint8(255)
        out = _decode_gunw_mask(ds, slice(None), slice(None))
    # valid land / water / no secondary / no reference / nothing / fill
    assert out.tolist() == [[True, False, False], [False, False, False]]


def test_select_pairs_prefers_consecutive_window():
    from datetime import timedelta as td
    from sarcompare.search import _best_window

    d = date(2025, 11, 1)
    chain = [_p(d + td(12 * k), d + td(12 * (k + 1))) for k in range(3)]          # consecutive
    chain += [_p(d + td(60 + 24 * k), d + td(72 + 24 * k)) for k in range(4)]      # gaps between pairs
    w = _best_window(chain, 3)
    assert all(a.date2 == b.date1 for a, b in zip(w, w[1:]))
    sel, notes = select_pairs(chain, max_pairs=3, min_span=6, max_span=96)
    assert any("consecutive pairs" in n.text for n in notes)


def test_fix_components_aligns_whole_cycles_and_drops_tiny_pieces():
    import numpy as np
    from sarcompare.readers.base import fix_components

    rng = np.random.default_rng(0)
    truth = rng.normal(0, 0.3, (60, 60))  # radians: real signal, much less than half a cycle
    labels = np.ones((60, 60), int)
    labels[:, 40:] = 2  # second piece, unwrapped 2 cycles too high
    labels[0:3, 0:3] = 3  # tiny piece
    phase = truth + np.where(labels == 2, 2 * 2 * np.pi, 0.0)
    fixed, info = fix_components(phase, labels, 2 * np.pi)
    assert info["pieces"] == 3 and info["shifted"] == 1
    np.testing.assert_allclose(fixed[labels == 2], truth[labels == 2], atol=1e-5)
    assert np.isnan(fixed[labels == 3]).all()
    kept, info = fix_components(phase, labels, None)  # C-band: keep only the largest piece
    assert np.isnan(kept[labels != 1]).all() and np.isfinite(kept[labels == 1]).all()
    single, info = fix_components(truth, np.ones_like(labels), 2 * np.pi)
    assert info["pieces"] == 1 and np.array_equal(single, truth.astype("float32"))


def test_select_pairs_chains_opera_files_through_reference_changes():
    """OPERA DISP-S1: files (ref -> later dates); the next reference is the previous group's last date."""
    from datetime import timedelta as td
    from sarcompare.products import SENTINEL1

    def f(d1, d2):
        return Product(SENTINEL1, "DISP-S1", f"{d1}_{d2}", date1=d1, date2=d2, track=137, flight_direction="ASCENDING")

    r0 = date(2025, 8, 12)
    r1, r2 = r0 + td(12 * 8), r0 + td(12 * 16)
    files = [f(r0, r0 + td(12 * k)) for k in range(1, 9)] + [f(r1, r1 + td(12 * k)) for k in range(1, 9)]
    files += [f(r2, r2 + td(12 * k)) for k in range(1, 4)]
    sel, notes = select_pairs(files, max_pairs=8, min_span=6, max_span=730)
    assert [(p.date1, p.date2) for p in sel] == [(r0, r1), (r1, r2), (r2, r2 + td(36))]
    # aim at another sensor's window: the chain that overlaps it best wins over the longest one
    sel, _ = select_pairs(files, max_pairs=8, min_span=6, max_span=730, target=(r0, r1))
    assert [(p.date1, p.date2) for p in sel] == [(r0, r1)]


def test_select_pairs_follows_target_window():
    from datetime import timedelta as td

    d = date(2025, 10, 1)
    prods = [_p(d + td(12 * k), d + td(12 * (k + 1))) for k in range(30)]
    sel, _ = select_pairs(prods, max_pairs=4, min_span=6, max_span=96)
    assert sel[-1].date2 == d + td(360)  # no target: most recent
    target = (d + td(60), d + td(108))
    sel, _ = select_pairs(prods, max_pairs=4, min_span=6, max_span=96, target=target)
    assert (sel[0].date1, sel[-1].date2) == target


def test_select_pairs_uses_each_frame_and_merge_lines_them_up():
    from datetime import timedelta as td
    import numpy as np
    from sarcompare.processing import RateMap, merge_frames
    from sarcompare.readers.base import make_da

    d = date(2025, 12, 6)
    prods = []
    for frame in (70, 71):
        prods += [Product(NISAR, "GUNW", f"f{frame}_{k}", date1=d + td(12 * k), date2=d + td(12 * (k + 1)), track=34,
                          flight_direction="ASCENDING", frame=frame) for k in range(5)]
    sel, notes = select_pairs(prods, max_pairs=3, min_span=6, max_span=96)
    assert {p.frame for p in sel} == {70, 71} and len(sel) == 6
    assert any("spans 2 frames" in n.text for n in notes)

    x, y = np.arange(40) * 90.0, np.arange(30)[::-1] * 90.0
    truth = np.tile(np.linspace(-0.05, 0.05, 40), (30, 1))
    a, b = truth.copy(), truth.copy() + 0.02  # frame b has its own zero, 20 mm/yr off
    a[:, 25:], b[:, :15] = np.nan, np.nan  # overlap in columns 15-24

    def rm(v):
        da = make_da(v, x, y, "EPSG:32611", "rate")
        return RateMap(NISAR, da, da * 0 + 0.5, da * 0 + 3, 3, 36, d, d + td(36), 40.0, 0.24, noise_rate=0.01)

    merged, mnotes = merge_frames([rm(a), rm(b)])
    np.testing.assert_allclose(merged.rate.values, truth, atol=1e-6)
    assert merged.n_pairs == 6 and "joined a neighbouring frame" in mnotes[0].text


def test_select_pairs_prefers_the_geometry_that_covers_the_area():
    from datetime import timedelta as td
    from shapely.geometry import box

    aoi = box(0, 0, 1, 1)
    d = date(2025, 12, 1)

    def p(track, frame, k, cover):
        return Product(NISAR, "GUNW", f"t{track}f{frame}_{k}", date1=d + td(12 * k), date2=d + td(12 * (k + 1)),
                       track=track, flight_direction="ASCENDING", frame=frame,
                       extra={"cover": cover.intersection(aoi), "aoi_area": aoi.area})

    full = [p(34, 20, k, box(-1, -1, 2, 2)) for k in range(6)]
    edge = [p(135, f, k, box(0.9, y, 1.5, y + 0.5)) for f, y in ((19, 0), (20, 0.5)) for k in range(6)]
    sel, notes = select_pairs(full + edge, max_pairs=4, min_span=6, max_span=96)
    assert {x.track for x in sel} == {34} and len(sel) == 4
    assert any("covering 100% of the area" in n.text for n in notes)
