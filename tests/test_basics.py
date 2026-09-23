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
