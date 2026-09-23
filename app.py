"""Streamlit web app: pick an area, search, download and compare NISAR with Sentinel-1.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import streamlit as st

from sarcompare.config import Config
from sarcompare.notes import GOOD, WARN
from sarcompare.pipeline import Event, Pipeline

st.set_page_config(page_title="NISAR vs Sentinel-1", page_icon="🛰️", layout="wide")

ICON = {GOOD: "✅", WARN: "⚠️"}
STAGES = {"search": "Search & pair selection", "download": "Download", "load": "Load pairs",
          "stack": "Stack into rates", "reference": "Reference point", "compare": "Compare & interpret",
          "figures": "Figures", "report": "Report"}


def theme() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def show_notes(notes, container):
    for n in notes:
        container.markdown(f"{ICON.get(n.level, 'ℹ️')} {n.text}")


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Setup")
    mode = st.radio("Data", ["Demo (synthetic)", "Search ASF", "Local files"],
                    help="Demo needs no account. Search ASF needs a free NASA Earthdata login.")
    name = st.text_input("Run name", "demo_synthetic" if mode.startswith("Demo") else "my_area")

    if not mode.startswith("Demo"):
        st.subheader("Area of interest")
        aoi_kind = st.radio("Define by", ["Bounding box", "WKT / GeoJSON"], horizontal=True)
        if aoi_kind == "Bounding box":
            c1, c2 = st.columns(2)
            west = c1.number_input("West (°E)", value=-99.25, format="%.4f")
            east = c2.number_input("East (°E)", value=-98.95, format="%.4f")
            south = c1.number_input("South (°N)", value=19.25, format="%.4f")
            north = c2.number_input("North (°N)", value=19.55, format="%.4f")
            aoi = [west, south, east, north]
        else:
            aoi = st.text_area("Paste WKT or GeoJSON", "POLYGON((-99.25 19.25,-98.95 19.25,-98.95 19.55,"
                                                          "-99.25 19.55,-99.25 19.25))")
        c1, c2 = st.columns(2)
        start = c1.date_input("Start", date(2025, 10, 1))
        end = c2.date_input("End", date.today())

    if mode == "Search ASF":
        s1_source = st.selectbox("Sentinel-1 product", ["ARIA_S1_GUNW", "OPERA_DISP_S1"],
                                 help="ARIA GUNW: selected regions worldwide. OPERA DISP-S1: North America. "
                                      "Elsewhere, order HyP3 InSAR jobs in ASF Vertex and use 'Local files'.")
        direction = st.selectbox("Flight direction", ["Any", "ASCENDING", "DESCENDING"])
        token = st.text_input("Earthdata token (optional)", type="password",
                              help="Or set EARTHDATA_TOKEN / ~/.netrc on the machine running this app.")
        if token:
            os.environ["EARTHDATA_TOKEN"] = token
    if mode == "Local files":
        nisar_dir = st.text_input("Folder with NISAR GUNW .h5 files", "data/NISAR")
        s1_dir = st.text_input("Folder with Sentinel-1 products (ARIA .nc, OPERA .nc, HyP3 .zip)", "data/Sentinel-1")

    with st.expander("Advanced"):
        coh = st.slider("Coherence threshold", 0.1, 0.8, 0.35, 0.05,
                        help="Pixels below this in a pair are ignored for that pair.")
        res = st.select_slider("Comparison grid (m)", [30, 60, 90, 120, 200], value=90)
        max_pairs = st.slider("Max pairs per sensor", 2, 20, 6)
        vertical = st.checkbox("Project to vertical", True, help="Assumes no horizontal motion.")
        iono = st.checkbox("Apply NISAR ionosphere correction", True)
        ref_txt = st.text_input("Reference point 'lon, lat' (blank = automatic)", "")

    def build_config() -> Config:
        if mode.startswith("Demo"):
            from sarcompare.demo import demo_config
            cfg = demo_config()
        else:
            kw = dict(name=name, aoi=aoi, start=start, end=end)
            if mode == "Search ASF":
                kw.update(s1_source=s1_source,
                          nisar_flight_direction=None if direction == "Any" else direction,
                          s1_flight_direction=None if direction == "Any" else direction)
            else:
                kw.update(nisar_source="LOCAL", nisar_dir=nisar_dir, s1_source="LOCAL", s1_dir=s1_dir)
            cfg = Config(**kw)
        cfg.coherence_threshold, cfg.resolution_m, cfg.max_pairs = coh, float(res), max_pairs
        cfg.project_to_vertical, cfg.nisar_apply_ionosphere = vertical, iono
        if ref_txt.strip():
            cfg.reference_lonlat = tuple(float(v) for v in ref_txt.split(","))
        return cfg

    search_clicked = st.button("1 · Search", use_container_width=True)
    run_clicked = st.button("2 · Download & compare", type="primary", use_container_width=True)

# ---------------------------------------------------------------- main
st.title("NISAR vs Sentinel-1")
st.caption("L-band (24 cm) and C-band (5.6 cm) radar interferometry over the same ground, side by side, "
           "with interpretation at every step.")
if mode.startswith("Demo"):
    st.info("Demo mode uses **synthetic** products (a subsiding city plus a landslide under forest) written in the "
            "real NISAR GUNW and ARIA GUNW file layouts. The results are not measurements of a real place.")

log = st.container()


def make_pipeline():
    boxes = {}

    def on_event(ev: Event):
        if ev.stage not in boxes:
            boxes[ev.stage] = log.status(STAGES.get(ev.stage, ev.stage), expanded=ev.stage in ("search", "compare"))
        box = boxes[ev.stage]
        box.write(f"**{ev.message}**")
        show_notes(ev.notes, box)

    return Pipeline(build_config(), on_event), boxes


def finish(boxes, state="complete"):
    for stage, box in boxes.items():
        box.update(state=state, expanded=stage == "compare" or state == "error")


try:
    if search_clicked:
        p, boxes = make_pipeline()
        nisar, s1 = p.search()
        finish(boxes)
        st.session_state["pipeline"] = p
        st.session_state.pop("result", None)
        rows = [x.to_row() for x in nisar + s1]
        if rows:
            st.subheader("Pairs that will be used")
            st.dataframe(rows, use_container_width=True, hide_index=True)
            total = sum(r["size_mb"] or 0 for r in rows)
            st.caption(f"Estimated download: {total / 1000:.1f} GB. Files already downloaded are reused.")

    if run_clicked:
        p, boxes = make_pipeline()
        with st.spinner("Working…"):
            try:
                st.session_state["result"] = p.run(download=True)
            except Exception:
                finish(boxes, "error")
                raise
        finish(boxes)
except Exception as e:  # show a friendly message instead of a traceback
    st.error(f"{type(e).__name__}: {e}")

res = st.session_state.get("result")
if res:
    th = theme()
    c = res.comparison
    st.divider()
    st.subheader("Results")
    m = st.columns(6)
    m[0].metric("NISAR coverage", f"{c.coverage_a:.0%}")
    m[1].metric("Sentinel-1 coverage", f"{c.coverage_b:.0%}")
    m[2].metric("Rate correlation r", f"{c.pearson_r:.2f}")
    m[3].metric("RMS difference", f"{c.rmsd_mm_yr:.1f} mm/yr")
    m[4].metric("Only NISAR can see", f"{c.only_a:.0%}")
    m[5].metric("Moving areas", len(c.hotspots))

    tabs = st.tabs(["Displacement", "Coherence", "Agreement", "Pairs", "Interpretation", "Downloads"])
    with tabs[0]:
        st.image(res.figures["rate_maps"][th], use_container_width=True)
        st.caption("Left and middle: each sensor's mean rate. Right: NISAR minus Sentinel-1 where both have data. "
                   "Grey = no reliable data.")
        if c.hotspots:
            st.dataframe([{"detected by": h.sensor, "lat": round(h.lat, 4), "lon": round(h.lon, 4),
                           "area km²": round(h.area_km2, 1), "peak mm/yr": round(h.peak_mm_yr),
                           "seen by other": h.seen_by_other} for h in c.hotspots], hide_index=True)
    with tabs[1]:
        st.image(res.figures["coherence_maps"][th], use_container_width=True)
        col, _ = st.columns([1, 1])
        col.image(res.figures["coherence_hist"][th], use_container_width=True)
    with tabs[2]:
        col, _ = st.columns([1, 1])
        col.image(res.figures["scatter"][th], use_container_width=True)
    with tabs[3]:
        st.image(res.figures["timeline"][th], use_container_width=True)
        st.dataframe([p.to_row() for p in res.nisar_products + res.s1_products], hide_index=True)
    with tabs[4]:
        for stage, title in STAGES.items():
            notes = [n for n in res.notes if n.stage == stage]
            if notes:
                st.markdown(f"#### {title}")
                show_notes(notes, st)
    with tabs[5]:
        for key, path in res.outputs.items():
            path = Path(path)
            if path.exists():
                st.download_button(f"Download {path.name}", path.read_bytes(), file_name=path.name, key=key)
