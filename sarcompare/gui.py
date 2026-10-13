"""OpenSAR Explorer graphical interface: a guided, map-based web app (runs in your browser).

Start it with ``sarcompare gui`` (or double-click Start-GUI.bat / start-gui.command / start-gui.sh).
"""

from __future__ import annotations

import math
import os
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

from sarcompare import __author__
from sarcompare.config import Config
from sarcompare.notes import GOOD, WARN
from sarcompare.pipeline import Event, Pipeline

ICON = {GOOD: "✅", WARN: "⚠️"}
STAGES = {"search": "Finding satellite data", "download": "Downloading", "load": "Reading the data",
          "stack": "Measuring ground motion", "reference": "Lining up the two satellites",
          "compare": "Comparing NISAR and Sentinel-1", "validate": "Checking against independent data",
          "figures": "Drawing maps", "report": "Writing the report"}
UPLOADS = Path("data/uploads")
NISAR_LAUNCH = date(2025, 7, 30)


def _setting(name: str, default: str) -> str:
    """Server setting: an environment variable, else a Streamlit secret (e.g. the Secrets box of Streamlit
    Community Cloud), else the default."""
    if name in os.environ:
        return os.environ[name]
    try:
        if st.secrets.load_if_toml_exists():  # quiet when there is no secrets file (local use)
            return str(st.secrets.get(name, default))
    except Exception:  # a malformed secrets file must not stop the app
        pass
    return default


# Hosted mode (set SARCOMPARE_ONLINE=1 on the server): no server folders, per-visitor files, size limits.
ONLINE = _setting("SARCOMPARE_ONLINE", "") == "1"
MAX_AOI_KM2 = float(_setting("SARCOMPARE_MAX_AOI_KM2", "2500"))
MAX_PAIRS_ONLINE = int(_setting("SARCOMPARE_MAX_PAIRS", "8"))

MODE_DEMO, MODE_REAL, MODE_LOCAL = "Try the demo", "Analyse a real place", "Use files I already have"

CSS = """
<style>
.app-banner{background:linear-gradient(90deg,#0b2e4f 0%,#1f5f8b 100%);color:#fff;padding:18px 24px;
  border-radius:10px;margin:0 0 6px}
.app-banner h1{color:#fff;font-size:1.55rem;line-height:1.25;margin:0;padding:0}
.app-banner p{margin:6px 0 0;color:#d6e4f0;font-size:.98rem}
.app-banner .byline{font-size:.95rem;font-weight:500;color:#d6e4f0;margin-left:8px;white-space:nowrap}
.app-banner .module{display:inline-block;background:rgba(255,255,255,.16);color:#fff;border:1px solid
  rgba(255,255,255,.35);border-radius:999px;padding:1px 10px;margin-right:8px;font-size:.85rem;font-weight:600}
.step{display:flex;align-items:center;gap:10px;font-size:1.18rem;font-weight:650;margin:26px 0 6px;color:#1f2933}
.step .n{background:#1f5f8b;color:#fff;border-radius:50%;min-width:30px;height:30px;display:inline-flex;
  align-items:center;justify-content:center;font-size:.95rem}
.hint{color:#5b6670;font-size:.92rem;margin:-2px 0 8px}
.badge{display:inline-block;background:#fff4d6;color:#7a5200;border:1px solid #f0d58a;border-radius:6px;
  padding:2px 8px;font-size:.85rem;font-weight:600;vertical-align:middle}
.app-footer{border-top:1px solid #d9dee3;margin-top:42px;padding-top:12px;color:#5b6670;font-size:.84rem}
.app-footer a{color:#1f5f8b}
</style>
"""


# ----------------------------------------------------------------------------------------------- helpers


def step(n: int | str, title: str, hint: str = ""):
    st.markdown(f'<div class="step"><span class="n">{n}</span>{title}</div>', unsafe_allow_html=True)
    if hint:
        st.markdown(f'<div class="hint">{hint}</div>', unsafe_allow_html=True)


def show_notes(notes, container=st):
    for n in notes:
        container.markdown(f"{ICON.get(n.level, 'ℹ️')} {n.text}")


def session_id() -> str:
    """Short random id per browser session, so visitors never share uploads or outputs."""
    if "sid" not in st.session_state:
        import uuid
        st.session_state["sid"] = uuid.uuid4().hex[:10]
    return st.session_state["sid"]


def save_upload(f) -> str:
    folder = UPLOADS / session_id()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / Path(f.name).name
    path.write_bytes(f.getbuffer())
    return str(path)


def header():
    st.markdown(
        f'<div class="app-banner"><h1>🛰️ OpenSAR Explorer <span class="byline">created by {__author__}</span></h1>'
        '<p><span class="module">Ground Motion module</span> See where the ground is sinking or rising, '
        "measured by two radar satellites (NISAR and Sentinel-1), side by side, explained in plain "
        "language.</p></div>", unsafe_allow_html=True)


def footer():
    st.markdown(
        f'<div class="app-footer">OpenSAR Explorer, created by {__author__}, is an independent open-source tool. Not affiliated with NASA, ISRO, ESA, ASF or USGS. '
        "Data: NISAR (NASA/ISRO) and Copernicus Sentinel-1 (ESA) via the "
        '<a href="https://asf.alaska.edu" target="_blank">Alaska Satellite Facility</a>; GNSS from the '
        '<a href="https://geodesy.unr.edu" target="_blank">Nevada Geodetic Laboratory</a>; base maps © '
        "OpenStreetMap contributors and Esri. Results are research estimates: verify before making decisions.</div>",
        unsafe_allow_html=True)


# ----------------------------------------------------------------------------------------------- Explore page


def choose_area():
    """Step 2: map + example places + typed coordinates. Returns (W, S, E, N) or None."""
    from streamlit_folium import st_folium

    from sarcompare.aoi import AOI
    from sarcompare.maps import EXAMPLE_PLACES, bbox_from_drawing, box_around, find_place, picker_map

    step(2, "Where?", "Search for any place, then draw a box around the area you want on the map.")
    side, mapcol = st.columns([2, 3], gap="large")
    with side:
        with st.form("place_search", border=False):
            q1, q2 = st.columns([3, 1], vertical_alignment="bottom")
            query = q1.text_input("Search a place", placeholder="e.g. Jakarta, Tehran, Venice, Joshimath")
            searched = q2.form_submit_button("Search", width="stretch")
        if searched:
            try:
                st.session_state["places"] = find_place(query)
                if not st.session_state["places"]:
                    st.warning(f"No place called '{query}' was found. Try adding the country, e.g. 'Delhi, India'.")
            except OSError:
                st.session_state["places"] = []
                st.error("Place search is not reachable right now. Zoom the map by hand, or type coordinates below.")
        places = st.session_state.get("places") or []
        if places:
            names = [p["name"] for p in places]
            pick = st.selectbox("Found", names, key="place_pick") if len(places) > 1 else names[0]
            found = places[names.index(pick)]
            if st.session_state.get("_view_for") != found["name"]:  # new place: move the map there
                st.session_state["_view_for"] = found["name"]
                st.session_state["view"] = (found["lat"], found["lon"], 11)
                st.session_state["bbox"] = None
            st.caption(f"Map moved to **{found['name']}**. Now draw your box on the map (square ▢ button), or:")
            if st.button("Use a box around this place", width="stretch"):
                st.session_state["bbox"] = box_around(found["lat"], found["lon"], found["bbox"],
                                                      max_km2=min(MAX_AOI_KM2, 900) if ONLINE else 900)
                st.rerun()
        options = ["(or choose an example place)"] + list(EXAMPLE_PLACES)
        place = st.selectbox("Example places", options, key="place")
        if place != options[0] and st.session_state.get("_place_applied") != place:
            st.session_state["bbox"] = EXAMPLE_PLACES[place]
            st.session_state["_place_applied"] = place
            st.session_state["view"] = None
        with st.expander("Type coordinates instead"):
            cur = st.session_state.get("bbox") or (-99.25, 19.25, -98.95, 19.55)
            c1, c2 = st.columns(2)
            w = c1.number_input("West (longitude)", -180.0, 180.0, float(cur[0]), format="%.4f")
            e = c2.number_input("East (longitude)", -180.0, 180.0, float(cur[2]), format="%.4f")
            s = c1.number_input("South (latitude)", -90.0, 90.0, float(cur[1]), format="%.4f")
            n = c2.number_input("North (latitude)", -90.0, 90.0, float(cur[3]), format="%.4f")
            if st.button("Use these coordinates"):
                if w < e and s < n:
                    st.session_state["bbox"] = (w, s, e, n)
                    st.session_state["view"] = None
                else:
                    st.error("West must be less than East, and South less than North.")
        bbox = st.session_state.get("bbox")
        if bbox:
            area = AOI.from_bbox(*bbox).area_km2()
            st.metric("Selected area", f"{area:,.0f} km²")
            st.caption(f"{bbox[1]:.3f}° to {bbox[3]:.3f}° N · {bbox[0]:.3f}° to {bbox[2]:.3f}° E")
            if ONLINE and area > MAX_AOI_KM2:
                st.warning(f"The online version handles areas up to {MAX_AOI_KM2:,.0f} km². Draw a smaller box.")
            elif area > 5000:
                st.info("Large area: downloads will be big and processing slow. 300–2,500 km² works best.")
        else:
            st.info("No area selected yet.")
    with mapcol:
        st.markdown("**1.** Find your place with the search on the left, or zoom the map with the mouse wheel.  \n"
                    "**2.** Click the **square ▢ button** on the left of the map.  \n"
                    "**3.** Click and drag to draw a box around your area. Draw again to replace it.")
        out = st_folium(picker_map(bbox, view=st.session_state.get("view")), height=430, use_container_width=True,
                        returned_objects=["last_active_drawing"], key="picker")
        drawn = bbox_from_drawing((out or {}).get("last_active_drawing"))
        if drawn and drawn != st.session_state.get("_last_drawn"):
            st.session_state["_last_drawn"] = drawn
            st.session_state["bbox"] = drawn
            st.rerun()
    return st.session_state.get("bbox")


def choose_period():
    step(3, "When?", "Longer periods give more reliable results. NISAR data start in late 2025.")
    today = date.today()
    choice = st.radio("Time period", ["Last 6 months", "Last 12 months", "Since NISAR launch (July 2025)",
                                      "Custom dates"], index=2, horizontal=True, label_visibility="collapsed")
    if choice == "Last 6 months":
        return today - timedelta(days=183), today
    if choice == "Last 12 months":
        return today - timedelta(days=365), today
    if choice.startswith("Since"):
        return NISAR_LAUNCH, today
    c1, c2, _ = st.columns([1, 1, 2])
    return c1.date_input("From", NISAR_LAUNCH), c2.date_input("To", today)


def choose_checks(n: int):
    step(n, "Extra checks (optional)", "Compare the satellite results with independent measurements.")
    c1, c2 = st.columns(2, gap="large")
    with c1:
        gnss = st.toggle("Compare with GNSS ground stations (recommended)", value=True,
                         help="Free GPS station data from the Nevada Geodetic Laboratory, downloaded automatically "
                              "for stations inside your area.")
        gnss_csv = None
        if gnss and st.checkbox("Use my own table of station velocities instead (CSV)"):
            gnss_csv = st.file_uploader("CSV with columns site, lon, lat, up_mm_yr, up_sigma_mm_yr", type=["csv"])
    with c2:
        from sarcompare.validate import egms_defaults

        files = st.file_uploader("Compare with published velocity maps (optional)", type=["tif", "tiff", "nc"],
                                 accept_multiple_files=True,
                                 help="For example EGMS Ortho tiles (Europe: the vertical 'U' and east-west 'E' "
                                      "files are recognised automatically), LiCSAR/COMET or OPERA velocity maps, "
                                      "or data from a paper.")
        with st.expander("Where do I get EGMS maps? (Europe)"):
            st.markdown("1. Open [egms.land.copernicus.eu](https://egms.land.copernicus.eu) and sign in (free EU "
                        "Login).\n2. Choose the **Ortho** product, the most recent period, and the 100 km tile "
                        "that covers your area.\n3. Download the **vertical (U)** and, if you like, the "
                        "**east-west (E)** files, unzip them, and upload the `.tiff` files here.\n\nEGMS is made "
                        "from Sentinel-1 by the EU Copernicus programme and calibrated with GNSS, so it is an "
                        "independent check. Its periods end before NISAR's start, which is fine for steady motion.")
        pub = []
        manual = [f for f in files or [] if not egms_defaults(f.name)]
        for f in files or []:
            spec = egms_defaults(f.name)
            if spec:
                st.success(f"{f.name}: recognised as {spec['label']}; settings filled in automatically.")
                pub.append((f, spec))
        if manual:
            st.caption("Settings for " + ", ".join(f.name for f in manual) + ":")
            a1, a2 = st.columns(2)
            units = a1.selectbox("Units", ["mm/yr", "cm/yr", "m/yr", "ft/yr", "in/yr",
                                           "mm (total over a period)", "cm (total over a period)",
                                           "ft (total over a period)"])
            comp = a2.selectbox("Direction", ["vertical", "LOS", "east-west"],
                                help="LOS = along the satellite's line of sight (most InSAR velocity maps). "
                                     "east-west maps are compared with this run's east-west maps.")
            years = (st.number_input("Years the map covers", 0.1, 50.0, 1.0, 0.1)
                     if "total" in units else None)
            sign = st.radio("Positive values mean", ["up / toward satellite / east", "down / away / west"],
                            horizontal=True)
            inc = st.number_input("Incidence angle (°)", 20.0, 50.0, 39.0) if comp == "LOS" else None
            cite = st.text_input("Source / citation", placeholder="e.g. California DWR, TRE ALTAMIRA InSAR, 2025")
            for f in manual:
                pub.append((f, {"label": Path(f.name).stem, "units": units.split(" ")[0], "component": comp,
                                "sign": 1 if sign.startswith("up") else -1, "incidence_deg": inc,
                                "period_years": years, "citation": cite}))
        pub_spec = None
    lit = published_values_editor()
    return gnss, gnss_csv, pub, pub_spec, lit


def published_values_editor() -> list[dict]:
    """Editable table for rates reported in papers or official reports."""
    with st.expander("📚 Compare with rates from published studies (optional)"):
        st.caption("Type in rates you read in papers or official reports, with the citation. The app compares its "
                   "own result at the same place. Use the study's period and vertical rates where possible.")
        import pandas as pd

        empty = pd.DataFrame({"Citation": pd.Series(dtype="str"), "Place": pd.Series(dtype="str"),
                              "Longitude": pd.Series(dtype="float"), "Latitude": pd.Series(dtype="float"),
                              "Rate (mm/yr)": pd.Series(dtype="float"), "Direction": pd.Series(dtype="str"),
                              "Reported as": pd.Series(dtype="str"), "Period": pd.Series(dtype="str")})
        table = st.data_editor(
            empty, num_rows="dynamic", width="stretch", key="lit_table",
            column_config={
                "Citation": st.column_config.TextColumn(help="e.g. Smith et al. (2021), Journal, volume"),
                "Rate (mm/yr)": st.column_config.NumberColumn(min_value=0.0, help="Size of the motion"),
                "Direction": st.column_config.SelectboxColumn(options=["sinking", "rising"], default="sinking"),
                "Reported as": st.column_config.SelectboxColumn(
                    options=["typical rate", "peak"], default="typical rate",
                    help="'peak' if the paper gives the fastest motion there (e.g. 'up to', 'peaks of')"),
                "Period": st.column_config.TextColumn(help="Years the study covers, e.g. 2015-2020"),
            })
    values = []
    for _, row in table.iterrows():
        try:
            lon, lat, rate = float(row["Longitude"]), float(row["Latitude"]), float(row["Rate (mm/yr)"])
        except (TypeError, ValueError):
            continue
        if any(v != v for v in (lon, lat, rate)):  # NaN: incomplete row
            continue
        sign = 1.0 if str(row.get("Direction")) == "rising" else -1.0
        values.append({"citation": str(row.get("Citation") or "").strip(), "place": str(row.get("Place") or ""),
                       "lon": lon, "lat": lat, "rate_mm_yr": sign * abs(rate), "period": str(row.get("Period") or ""),
                       "stat": "peak" if str(row.get("Reported as")) == "peak" else "median",
                       "radius_km": 2.0 if str(row.get("Reported as")) == "peak" else 1.0})
    return values


def token_step(n: int):
    step(n, "Your free NASA Earthdata token", "Needed to download satellite data. It is used only for your own "
                                              "downloads in this session and is never saved.")
    c1, c2 = st.columns([1, 1], gap="large")
    token = c1.text_input("Earthdata token", type="password", label_visibility="collapsed",
                          placeholder="Paste your token here")
    with c2.expander("How do I get a token? (about 2 minutes)"):
        st.markdown("1. Create a free account at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov/users/new).\n"
                    "2. Log in and open **Generate Token** (in your profile menu).\n"
                    "3. Click **Generate Token**, copy it, and paste it here.\n\n"
                    "Tokens expire after about two months; just make a new one.")
    return token


def advanced_settings():
    with st.expander("⚙️ Advanced settings (most people can skip this)"):
        c1, c2, c3 = st.columns(3)
        s1 = c1.selectbox("Sentinel-1 product", ["Automatic (recommended)", "ARIA interferograms",
                                                 "OPERA DISP-S1 (North America)", "Ordered from ASF HyP3",
                                                 "LiCSAR (COMET)"],
                          help="Automatic tries the ready-made products (ARIA, then OPERA in North America), "
                               "then interferograms you ordered from ASF HyP3 for this area, then LiCSAR "
                               "(free, ready-made for many tectonic and volcanic regions, e.g. from the "
                               "Mediterranean to the Himalaya).")
        direction = c1.selectbox("Satellite pass", ["Any", "ASCENDING", "DESCENDING"])
        coh = c2.slider("Data quality threshold (coherence)", 0.1, 0.8, 0.35, 0.05,
                        help="Pixels whose radar signal is less stable than this are left out.")
        max_pairs = c2.slider("Image pairs per satellite", 2, MAX_PAIRS_ONLINE if ONLINE else 20, 6)
        res = c3.select_slider("Map pixel size (m)", [30, 60, 90, 120, 200], value=90)
        vertical = c3.checkbox("Show vertical motion", True, help="Converts line-of-sight motion to up/down.")
        both_dirs = c3.checkbox("Use both orbit directions", True,
                                help="Combine passes from north-going (ascending) and south-going (descending) "
                                     "orbits when both cover your area. This separates up-down from east-west "
                                     "motion instead of assuming the ground only moves up and down. Reads about "
                                     "twice as many files.")
        iono = c3.checkbox("Correct NISAR for the ionosphere", True)
        ref = st.text_input("Stable reference point 'longitude, latitude' (blank = automatic)", "")
    s1_code = {"Automatic (recommended)": "AUTO", "ARIA interferograms": "ARIA_S1_GUNW",
               "Ordered from ASF HyP3": "HYP3", "LiCSAR (COMET)": "LICSAR"}.get(s1, "OPERA_DISP_S1")
    return dict(s1=s1_code, direction=None if direction == "Any" else direction, coh=coh, max_pairs=max_pairs,
                res=res, vertical=vertical, iono=iono, ref=ref, both_dirs=both_dirs)


def build_config(mode, bbox, period, checks, token, adv, local_dirs) -> Config:
    from sarcompare.demo import demo_config

    gnss, gnss_csv, pub, pub_spec, lit = checks
    if mode == MODE_DEMO:
        cfg = demo_config()
        if not gnss:
            cfg.gnss = None
    else:
        kw = dict(name="my_area", aoi=list(bbox), start=period[0], end=period[1], s1_source=adv["s1"],
                  nisar_flight_direction=adv["direction"], s1_flight_direction=adv["direction"])
        if mode == MODE_LOCAL:
            kw.update(nisar_source="LOCAL", nisar_dir=local_dirs[0], s1_source="LOCAL", s1_dir=local_dirs[1])
        cfg = Config(**kw)
        cfg.gnss = "NGL" if gnss else None
        cfg.earthdata_token = token or None  # per session; never os.environ (shared by all users of a server)
    if gnss and gnss_csv is not None:
        cfg.gnss = save_upload(gnss_csv)
    if pub:
        cfg.published_maps = list(cfg.published_maps) + [{"path": save_upload(f), **spec} for f, spec in pub]
    if lit:
        cfg.published_values = list(cfg.published_values) + lit
    if ONLINE:
        cfg.output_dir = f"outputs/{session_id()}"
    cfg.coherence_threshold, cfg.resolution_m, cfg.max_pairs = adv["coh"], float(adv["res"]), adv["max_pairs"]
    cfg.project_to_vertical, cfg.nisar_apply_ionosphere = adv["vertical"], adv["iono"]
    cfg.combine_directions = adv.get("both_dirs", True)
    if adv["ref"].strip():
        cfg.reference_lonlat = tuple(float(v) for v in adv["ref"].split(","))
    return cfg


def friendly_error(e: Exception) -> str:
    msg, name = str(e), type(e).__name__
    low = msg.lower()
    if name == "AccessError":  # already a plain-language reason with the fix
        return f"Could not get the data files: {msg}"
    if name == "HyP3Error":  # plain-language reason from the HyP3 order/status code
        return msg
    if "credential" in low or ("token" in low and ("invalid" in low or "unauthor" in low)):
        return "Your Earthdata token was not accepted. Check that you pasted all of it, or generate a new one."
    if name in ("ConnectionError", "ProxyError", "Timeout", "ConnectTimeout") or "cmr.earthdata" in low:
        return "Could not reach NASA's data servers. Check your internet connection and try again."
    if "no nisar products" in low or "no sentinel-1 products" in low:
        return msg + " See **Help & FAQ → Why was no data found?** for what to try next."
    return f"Something went wrong: {msg}"


def run_pipeline(cfg: Config, search_only: bool):
    boxes = {}

    def on_event(ev: Event):
        if ev.stage not in boxes:
            boxes[ev.stage] = st.status(STAGES.get(ev.stage, ev.stage), expanded=False)
        boxes[ev.stage].write(f"**{ev.message}**")
        show_notes(ev.notes, boxes[ev.stage])

    p = Pipeline(cfg, on_event)
    try:
        if search_only:
            p.search()
            result = None
        else:
            with st.spinner("Working… this can take a few minutes for real data."):
                result = p.run(download=True)
    except Exception as e:  # show a friendly message instead of a traceback
        for b in boxes.values():
            b.update(state="error")
        st.error(friendly_error(e))
        return None
    for b in boxes.values():
        b.update(state="complete", expanded=False)
    if search_only:
        rows = [x.to_row() for x in p.result.nisar_products + p.result.s1_products]
        if rows:
            st.success(f"Found {len(p.result.nisar_products)} NISAR and {len(p.result.s1_products)} Sentinel-1 image "
                       f"pairs (about {sum(r['size_mb'] or 0 for r in rows) / 1000:.1f} GB to download).")
            for sensor, found in (("Sentinel-1", p.result.s1_products), ("NISAR", p.result.nisar_products)):
                if not found:
                    other = "NISAR" if sensor == "Sentinel-1" else "Sentinel-1"
                    st.info(f"No ready-made {sensor} pairs for this area and period: the analysis will use "
                            f"{other} alone. The search notes above say how to add {sensor}.")
            st.dataframe(rows, width="stretch", hide_index=True)
        else:
            st.warning("No usable image pairs were found for this area and period. See Help & FAQ.")
    return result


def offer_hyp3(res):
    """Plan and order Sentinel-1 interferograms from ASF HyP3 for an area without ready-made ones."""
    from sarcompare.pipeline import Pipeline as _Pipeline

    cfg = res.config
    with st.container(border=True):
        st.markdown("#### Add Sentinel-1: order it from ASF HyP3")
        st.markdown("No ready-made Sentinel-1 interferograms cover this area and period. ASF's free **HyP3** "
                    "service can make them from the raw Sentinel-1 images, on the same dates as the NISAR pairs, "
                    "with your Earthdata login. Processing usually takes under an hour. Then run this area again: "
                    "the finished interferograms are found and used automatically. HyP3 keeps them for 14 days.")
        st.caption("This uses free HyP3 processing credits from your Earthdata account (a monthly allowance). "
                   "HyP3 jobs are publicly visible: the job name (this area's coordinates), the images and your "
                   "Earthdata user name.")
        if not (cfg.earthdata_token or os.environ.get("EARTHDATA_TOKEN")):
            st.info("An Earthdata token is needed to order. Paste yours in the token step and run again.")
            return
        if st.button("Find Sentinel-1 pairs to order", key="hyp3_plan_btn"):
            try:
                p = _Pipeline(cfg, on_event=lambda e: None)
                p.result.nisar_products = list(res.nisar_products)
                with st.spinner("Searching Sentinel-1 images and checking your HyP3 credits…"):
                    pairs, notes = p.hyp3_plan()
                    client = p._hyp3()
                    info = client.user()
                    from sarcompare.hyp3 import access_problem

                    access_problem(info)  # new HyP3 users apply once (free); explained before anything else
                    credits = info.get("remaining_credits")
                    cost = client.cost()
                st.session_state["hyp3_plan"] = (pairs, [n.text for n in notes], credits, cost)
            except Exception as e:
                st.error(friendly_error(e))
        plan = st.session_state.get("hyp3_plan")
        if not plan:
            return
        pairs, texts, credits, cost = plan
        for t in texts:
            st.markdown(f"ℹ️ {t}")
        if not pairs:
            st.warning("No suitable Sentinel-1 image pairs were found for this area and period.")
            return
        st.dataframe([{"From": str(q.date1), "To": str(q.date2), "Days": q.span_days,
                       "Direction": q.direction.lower(), "Track": q.path, "Satellite": q.platform,
                       "Area covered": f"{q.cover:.0%}"} for q in pairs], hide_index=True, width="stretch")
        left = "no limit" if credits is None else f"{credits:,.0f} left this month"
        st.markdown(f"**{len(pairs)} pairs, about {cost * len(pairs):,.0f} credits** ({left}).")
        done = st.session_state.get("hyp3_ordered")
        if done:
            st.success(done)
            return
        if st.button(f"Order {len(pairs)} Sentinel-1 interferograms", type="primary", key="hyp3_order_btn"):
            try:
                p = _Pipeline(cfg, on_event=lambda e: None)
                with st.spinner("Sending the order to ASF HyP3…"):
                    out = p.hyp3_order(pairs)
                msg = " ".join(n.text for n in out.notes)
                if out.skipped:
                    msg += f" {out.skipped} pair(s) were already ordered and were not sent again."
                st.session_state["hyp3_ordered"] = msg
                st.success(msg)
            except Exception as e:
                st.error(friendly_error(e))


def show_results(res):
    from sarcompare.interpret import plain_summary
    from sarcompare.maps import results_map

    c = res.comparison
    maps = res.rates  # one map when a satellite has no data for the area
    sensors = [rm.sensor for rm in maps]
    st.markdown("---")
    title = "## Results"
    if res.config.extra.get("synthetic"):
        title += ' <span class="badge">Synthetic demo data, not a real place</span>'
    st.markdown(title, unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("#### In short")
        for level, text in plain_summary(res):
            st.markdown(f"{ICON.get(level, 'ℹ️')} {text}")

    m = st.columns(5)
    if res.single:
        other = "Sentinel-1" if sensors[0] == "NISAR" else "NISAR"
        m[0].metric(f"Area {sensors[0]} can measure", f"{c.coverage_a:.0%}")
        m[1].metric(other, "No data", help=f"{other} has no usable data for this area and period. "
                                            "See All notes for how to add it.")
        m[2].metric("Noise (1σ)", f"{c.noise_a_mm_yr:.0f} mm/yr" if math.isfinite(c.noise_a_mm_yr) else "n/a",
                    help="Movements smaller than about twice this cannot be told apart from noise.")
        m[3].metric("Moving areas found", len(c.hotspots))
        if res.s1_rate is None and not res.config.extra.get("synthetic"):
            offer_hyp3(res)
    else:
        m[0].metric("Area NISAR can measure", f"{c.coverage_a:.0%}")
        m[1].metric("Area Sentinel-1 can measure", f"{c.coverage_b:.0%}")
        m[2].metric("Agreement (0 to 1)", f"{c.pearson_r:.2f}", help="Correlation between the two speed maps.")
        m[3].metric("Typical difference", f"{c.rmsd_mm_yr:.0f} mm/yr")
        m[4].metric("Moving areas found", len(c.hotspots))

    st.markdown("#### Map")
    st.caption("Switch layers with the box at the top right. Click markers for details. Red = sinking, "
               "blue = rising, no colour = no reliable data.")
    # Display-only map: plain embedded HTML is more robust than the two-way map component here.
    html = results_map(res).get_root().render()  # built by the app; user text in it is escaped
    if hasattr(st, "iframe"):  # st.components.v1.html is being removed from Streamlit
        st.iframe(html, height=580)
    else:
        import streamlit.components.v1 as components
        components.html(html, height=580)
    if c.hotspots:
        with st.expander(f"Moving areas ({len(c.hotspots)})", expanded=True):
            st.dataframe([{"Detected by": h.sensor, "Latitude": round(h.lat, 4), "Longitude": round(h.lon, 4),
                           "Area (km²)": round(h.area_km2, 1), "Peak (mm/yr)": round(h.peak_mm_yr),
                           "Seen by the other satellite": {"yes": "Yes", "no": "No", "no data": "No data there",
                                                           "single": "Only one satellite here"}[h.seen_by_other]}
                          for h in c.hotspots], hide_index=True, width="stretch")

    st.markdown("#### More detail")
    tabs = st.tabs(["⬇️ Download", "Side by side", "Agreement", "Checks", "Data used", "All notes"])
    with tabs[1]:
        st.image(res.figures["rate_maps"]["light"], width="stretch")
        st.caption("Left: NISAR. Middle: Sentinel-1. Right: their difference. Grey = no reliable data."
                   if not res.single else f"{sensors[0]} only (the other satellite has no data here). "
                                          "Grey = no reliable data.")
        if "east_west" in res.figures:
            st.image(res.figures["east_west"]["light"], width="stretch")
            st.caption("Sideways motion, measured by combining passes from the north-going (ascending) and "
                       "south-going (descending) orbits. Around a sinking area the ground often also moves toward "
                       "its centre. Grey = seen from one direction only.")
        st.image(res.figures["coherence_maps"]["light"], width="stretch")
        st.caption("Coherence = how stable the radar signal is between visits. Dark = good. Vegetation usually "
                   "lowers it, much more for Sentinel-1 than for NISAR.")
    with tabs[2]:
        if "scatter" in res.figures:
            col, txt = st.columns([1, 1], gap="large")
            col.image(res.figures["scatter"]["light"], width="stretch")
            txt.markdown("Each dot cloud compares the two satellites pixel by pixel. If they agreed perfectly, "
                         "everything would sit on the dashed 1:1 line.")
            show_notes([n for n in res.notes if n.stage == "compare"], txt)
        else:
            st.info("Only one satellite has data for this area, so there is nothing to compare it with. Its "
                    "results are checked against GNSS and published studies instead (Checks tab).")
            show_notes([n for n in res.notes if n.stage == "compare"])
    with tabs[3]:
        vnotes = [n for n in res.notes if n.stage == "validate"]
        if not vnotes:
            st.info("No extra checks were selected. Turn on 'Compare with GNSS ground stations'.")
        if "gnss" in res.figures:
            col, txt = st.columns([1, 1], gap="large")
            col.image(res.figures["gnss"]["light"], width="stretch")
            show_notes(vnotes, txt)
            from .validate import station_rate

            st.dataframe([{"Station": s.site, "Latitude": round(s.lat, 4), "Longitude": round(s.lon, 4),
                           **{k: v for sn in sensors for k, v in (
                               (f"{sn} (mm/yr)", round(s.insar.get(sn, float("nan")), 1)),
                               (f"GNSS, {sn} dates", round(station_rate(s, sn), 1)))},
                           "GNSS long-term (mm/yr)": round(s.up_mm_yr, 1)}
                          for s in res.gnss.stations], hide_index=True, width="stretch")
        else:
            show_notes(vnotes)
        for k in range(len(res.published)):
            st.image(res.figures[f"published_{k}"]["light"], width="stretch")
        if res.literature:
            st.markdown("**Published studies**")
            names = {"consistent": "✓ consistent", "differs": "! differs", "no data": "no data",
                     "outside area": "outside area"}
            st.dataframe([{"Study": c.citation, "Place": c.place, "Period": c.period,
                           "Published (mm/yr)": (f"{c.published_min:+.0f} to {c.published_max:+.0f}"
                                                 if c.published_min is not None and c.published_max is not None
                                                 else f"{c.published_mm_yr:+.0f}"),
                           **{k: v for sn in sensors for k, v in (
                               (f"{sn} (mm/yr)", round(c.insar.get(sn, float("nan")), 0)),
                               (f"{sn} check", names.get(c.verdict.get(sn), "")))}}
                          for c in res.literature], hide_index=True, width="stretch")
            st.caption("Vertical rates, negative = sinking. Differences often come from different periods: "
                       "subsidence speeds up in droughts and slows after wet years.")
    with tabs[4]:
        st.image(res.figures["timeline"]["light"], width="stretch")
        st.dataframe([p.to_row() for p in res.nisar_products + res.s1_products], hide_index=True,
                     width="stretch")
    with tabs[5]:
        for stage, title in STAGES.items():
            notes = [n for n in res.notes if n.stage == stage]
            if notes:
                st.markdown(f"#### {title}")
                show_notes(notes)
    with tabs[0]:
        rep = Path(res.outputs.get("report", ""))
        if rep.is_file():
            st.download_button("⬇️ Download the full report (HTML, opens in any browser)", rep.read_bytes(),
                               file_name=rep.name, type="primary", key="dl_report")
        st.markdown("**Map files for GIS software (QGIS, ArcGIS) and the numbers (JSON)**")
        for key, path in res.outputs.items():
            path = Path(path)
            if key != "report" and path.is_file():
                st.download_button(path.name, path.read_bytes(), file_name=path.name, key=f"dl_{key}")


def page_explore():
    step(1, "What would you like to do?")
    modes = [MODE_DEMO, MODE_REAL] + ([] if ONLINE else [MODE_LOCAL])
    captions = ["No account needed. Uses made-up data to show how everything works.",
                "Downloads real NISAR and Sentinel-1 data for any place (free NASA account).",
                "Analyse NISAR and Sentinel-1 files you already downloaded."][:len(modes)]
    mode = st.radio("Mode", modes, captions=captions, horizontal=True, label_visibility="collapsed")

    bbox, period, token, local_dirs = None, (None, None), None, (None, None)
    if mode == MODE_DEMO:
        st.info("**Demo:** a made-up 30 × 28 km scene with a sinking city and a landslide under forest, written in "
                "the real NISAR and Sentinel-1 file formats. Just press **Run analysis** below.")
        checks = choose_checks(2)
        n_next = 3
    else:
        bbox = choose_area()
        period = choose_period()
        checks = choose_checks(4)
        if mode == MODE_REAL:
            token = token_step(5)
        else:
            step(5, "Where are your files?")
            c1, c2 = st.columns(2)
            local_dirs = (c1.text_input("Folder with NISAR GUNW .h5 files", "data/NISAR"),
                          c2.text_input("Folder with Sentinel-1 files (ARIA .nc, OPERA .nc, HyP3 .zip)",
                                        "data/Sentinel-1"))
        n_next = 6
    adv = advanced_settings()

    step(n_next, "Run")
    b1, b2, _ = st.columns([1, 1, 2])
    run = b1.button("▶ Run analysis", type="primary", width="stretch")
    check = b2.button("Check available data", width="stretch", disabled=mode != MODE_REAL,
                      help="Search only, without downloading, to see what exists for your area and dates.")
    if run or check:
        problem = None
        if mode != MODE_DEMO and not bbox:
            problem = "Please choose an area first (step 2)."
        elif mode == MODE_REAL and ONLINE and not token:
            problem = "Please paste your free NASA Earthdata token (step 5)."
        elif mode != MODE_DEMO and period[0] and period[1] and period[0] >= period[1]:
            problem = "The start date must be before the end date."
        if problem:
            st.warning(problem)
        else:
            try:
                cfg = build_config(mode, bbox, period, checks, token, adv, local_dirs)
                if ONLINE and mode != MODE_DEMO and cfg.get_aoi().area_km2() > MAX_AOI_KM2:
                    raise ValueError(f"The online version handles areas up to {MAX_AOI_KM2:,.0f} km². "
                                     "Draw a smaller box, or run the app on your own computer.")
            except ValueError as e:
                st.warning(str(e))
            else:
                result = run_pipeline(cfg, search_only=check)
                if result is not None:
                    st.session_state["result"] = result
                    st.session_state.pop("hyp3_plan", None)  # an order offer belongs to one result
                    st.session_state.pop("hyp3_ordered", None)
    if st.session_state.get("result") is not None:
        show_results(st.session_state["result"])


# ----------------------------------------------------------------------------------------------- Learn / Help


def page_learn():
    st.markdown("## How it works")
    st.markdown(
        "**Radar satellites can measure ground motion of a few millimetres.** A radar satellite sends microwaves "
        "to the ground and records the echo. When it passes over the same place again days later, tiny changes "
        "in the distance between the satellite and the ground show up as a shift in the echo's *phase*. Comparing "
        "two such images is called **InSAR** (Interferometric Synthetic Aperture Radar). Comparing many images "
        "over months gives the ground's **speed**, in millimetres per year.")
    st.markdown("### Two satellites, two wavelengths")
    st.markdown(
        "| | NISAR | Sentinel-1 |\n|---|---|---|\n"
        "| Built by | NASA and ISRO (launched July 2025) | ESA, Copernicus (since 2014) |\n"
        "| Radar wavelength | 24 cm (L-band) | 5.6 cm (C-band) |\n"
        "| Sees through vegetation? | Largely yes: works in forests and fields | Mostly no: leaves scramble the "
        "signal |\n"
        "| Sensitivity to small motion | Good | Very good |\n"
        "| Affected by the ionosphere | More (the app corrects it) | Less |\n"
        "| Archive | New | More than 10 years |")
    st.markdown("Where **both agree**, the motion is very likely real. Where **only NISAR has data** (often "
                "under vegetation), you may be seeing something no one could measure before.")
    st.markdown("### How to read the maps")
    c1, c2 = st.columns(2, gap="large")
    c1.markdown("- 🟥 **Red**: the ground is moving **down** (sinking, *subsidence*).\n"
                "- 🟦 **Blue**: the ground is moving **up** (rising, *uplift*).\n"
                "- ⬜ **Pale**: little or no motion.\n"
                "- **No colour**: no reliable measurement (water, dense vegetation for Sentinel-1, steep slopes, "
                "or too few good images).")
    c2.markdown("- **Motion is relative** unless GNSS is on. Without GNSS, zero means the typical motion of your "
                "area; with GNSS stations in the area, the maps are tied to them and zero means no motion.\n"
                "- **Speeds are averages** over your time period; seasonal ups and downs are averaged out.\n"
                "- **'Vertical'** assumes the ground moves only up and down. Landslides also move sideways.")
    st.markdown("### What usually causes ground motion?")
    st.markdown("- **Groundwater pumping:** broad, bowl-shaped sinking in cities and farmland, centimetres per "
                "year.\n"
                "- **Landslides and creep:** small, fast patches on slopes.\n"
                "- **Mining and tunnelling:** localised sinking.\n"
                "- **Volcanoes and earthquakes:** uplift or sudden offsets.\n"
                "- **Building settlement and reclaimed land:** slow sinking of individual structures.")
    st.markdown("### Glossary")
    terms = {
        "InSAR": "Comparing radar images taken at different times to measure ground motion.",
        "Interferogram (pair)": "The comparison of two radar images; shows motion between their two dates.",
        "Coherence": "From 0 to 1: how similar the radar echo is between the two dates. Low = unreliable.",
        "Line of sight (LOS)": "The direction from the ground to the satellite. InSAR measures motion along it.",
        "Ionosphere": "The upper atmosphere. It delays radar signals, especially longer wavelengths like NISAR's.",
        "GNSS": "Satellite positioning (GPS and similar). Permanent stations measure ground motion very precisely at "
                "one point, so they are ideal for checking InSAR.",
        "Subsidence": "The ground sinking.",
        "Reference point": "The place assumed to be stable; all motion is measured relative to it.",
        "GUNW": "NISAR's ready-made 'geocoded unwrapped interferogram' product.",
    }
    for k, v in terms.items():
        st.markdown(f"**{k}**: {v}")


def page_help():
    st.markdown("## Help & FAQ")
    st.markdown("### Getting started in 1 minute")
    st.markdown("1. On **Explore**, choose **Try the demo** and press **Run analysis**. No account needed.\n"
                "2. To study a real place, choose **Analyse a real place**, pick an example place or draw a box, "
                "and paste your free NASA Earthdata token.\n"
                "3. Read **In short** at the top of the results, then explore the **Map** tab.")
    faq = {
        "Why was no data found?": (
            "NISAR started producing data in late 2025, so choose dates after that. Ready-made Sentinel-1 "
            "interferograms exist for parts of the world (ARIA, mainly the USA) and for North America (OPERA)."),
        "There is no Sentinel-1 data for my area. What happens?": (
            "The analysis continues with NISAR alone: the ground-motion map, the moving areas and the checks "
            "against GNSS and published studies all still work; only the second-satellite confirmation is "
            "missing. To add Sentinel-1, press **Find Sentinel-1 pairs to order** below the results: the app "
            "picks Sentinel-1 images on the same dates as the NISAR pairs and orders the interferograms from ASF's "
            "free HyP3 service with your Earthdata login (it uses free monthly processing credits; HyP3 jobs are "
            "publicly visible). Processing usually takes under an hour. Then run the same area again: the "
            "finished interferograms are found and used automatically. HyP3 keeps them for 14 days."),
        "Is my Earthdata token safe?": (
            "It is used only for your downloads in your own session and is never written to disk or shown to "
            "other visitors. You can delete a token any time in your Earthdata profile."),
        "How long does it take?": (
            "The demo takes under a minute. Real data depend on your area and internet speed: each NISAR file is "
            "hundreds of MB, so a first run can take 10–30 minutes. Re-runs reuse downloaded files."),
        "The two satellites disagree. Which is right?": (
            "Often both are partly right. They measure along different viewing directions and on slightly "
            "different dates, and each has its own noise (water vapour in the air, the ionosphere). Use the "
            "**Checks** tab: GNSS stations are an independent referee."),
        "How should I cite or credit the data?": (
            "Credit NASA/ISRO for NISAR and ESA's Copernicus programme for Sentinel-1, accessed via the Alaska "
            "Satellite Facility; GNSS from the Nevada Geodetic Laboratory. Check each provider's citation "
            "guidance."),
        "Can it predict disasters?": (
            "No. It shows **where** the ground is already moving and roughly **how fast**, which helps find areas "
            "that need closer study (a sinking neighbourhood, a creeping slope, ground around a mine). It cannot "
            "tell **whether or when** a landslide will fail, a building will be damaged, or an earthquake will "
            "happen. That needs continuous monitoring, ground instruments, field investigation and the responsible "
            "authorities. Think of it as hazard screening, not a forecast."),
        "Can I use this for decisions about safety?": (
            "No. This is a research and learning tool. Results should be verified by qualified experts using field "
            "observations before any decision."),
    }
    for q, a in faq.items():
        with st.expander(q):
            st.markdown(a)


# ----------------------------------------------------------------------------------------------- main


def main():
    st.set_page_config(page_title="OpenSAR Explorer: Ground Motion", page_icon="🛰️", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    nav = st.navigation([st.Page(page_explore, title="Explore", icon=":material/map:", default=True),
                         st.Page(page_learn, title="Learn", icon=":material/school:"),
                         st.Page(page_help, title="Help & FAQ", icon=":material/help:")], position="top")
    header()
    nav.run()
    footer()


if __name__ == "__main__":
    main()
