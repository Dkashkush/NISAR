"""Self-contained HTML report (figures embedded, light/dark aware)."""

from __future__ import annotations

import base64
import html
import math
from datetime import datetime

from . import __author__
from .notes import GOOD, WARN

STAGE_TITLES = {
    "search": "Data search & pair selection",
    "download": "Download",
    "load": "Loading each pair",
    "stack": "Stacking pairs into rates",
    "reference": "Common reference point",
    "compare": "Comparison & interpretation",
    "validate": "Validation against published data",
    "report": "Outputs",
}

CSS = """
:root{--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;
--good:#006300;--warn:#8a5a00;--info:#256abf;--chip:#f0efec}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;
--ink2:#c3c2b7;--muted:#898781;--line:#2c2c2a;--good:#0ca30c;--warn:#fab219;--info:#86b6ef;--chip:#2c2c2a}}
:root[data-theme="dark"]{--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--line:#2c2c2a;
--good:#0ca30c;--warn:#fab219;--info:#86b6ef;--chip:#2c2c2a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px}
.sub{color:var(--ink2);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.tile .v{font-size:24px;font-weight:600}.tile .k{color:var(--ink2);font-size:13px}
ul.notes{list-style:none;padding:0;margin:0}
ul.notes li{padding:8px 0;border-top:1px solid var(--line);display:flex;gap:10px}
ul.notes li:first-child{border-top:0}
.tag{flex:none;font-size:12px;font-weight:600;border-radius:6px;padding:1px 7px;height:fit-content;background:var(--chip)}
.tag.good{color:var(--good)}.tag.warn{color:var(--warn)}.tag.info{color:var(--info)}
img{max-width:100%;height:auto;display:block;border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}th{color:var(--ink2);font-weight:600}
.scroll{overflow-x:auto}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.two{grid-template-columns:1fr}}
.banner{background:var(--chip);border-radius:8px;padding:10px 12px;color:var(--ink2)}
code{font-size:13px;overflow-wrap:anywhere}
li{overflow-wrap:anywhere}
.tile{min-width:0}
"""

LEVEL_LABEL = {GOOD: ("good", "✓ Finding"), WARN: ("warn", "! Check"), "info": ("info", "i Note")}


def _img(themes: dict[str, bytes], alt: str) -> str:
    light = base64.b64encode(themes["light"]).decode()
    dark = base64.b64encode(themes["dark"]).decode()
    return (f'<picture><source srcset="data:image/png;base64,{dark}" media="(prefers-color-scheme: dark)">'
            f'<img src="data:image/png;base64,{light}" alt="{html.escape(alt)}"></picture>')


def _num(v, nd=1, suffix=""):
    return "n/a" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.{nd}f}{suffix}"


VERDICT = {"consistent": "✓ consistent", "differs": "! differs", "no data": "no data", "outside area": "outside area"}


def _literature_table(checks, sensors=("NISAR", "Sentinel-1")) -> str:
    e = html.escape
    rows = []
    for c in checks:
        pub = (f"{c.published_min:+.0f} to {c.published_max:+.0f}" if c.published_min is not None
               and c.published_max is not None else f"{c.published_mm_yr:+.0f}")
        if getattr(c, "stat", "median") == "peak":
            pub += "<br><small>peak</small>"
        cells = "".join(f"<td>{_num(c.insar.get(s, float('nan')), 0)}<br><small>{e(VERDICT.get(c.verdict.get(s), ''))}"
                        f"</small></td>" for s in sensors)
        rows.append(f"<tr><td>{e(c.citation)}</td><td>{e(c.place or f'{c.lat:.4f}, {c.lon:.4f}')}</td>"
                    f"<td>{e(c.period)}</td><td>{pub}</td>{cells}</tr>")
    absolute = any(any(c.absolute.values()) for c in checks)
    note = ("This run's values are tied to GNSS (absolute)." if absolute else
            "This run's values are relative to the area median; published rates are often absolute.")
    return ('<h3 style="margin:18px 0 6px">Published studies</h3><div class="scroll"><table><thead><tr>'
            "<th>Study</th><th>Place</th><th>Period</th><th>Published (mm/yr)</th>"
            + "".join(f"<th>{e(s)} (mm/yr)</th>" for s in sensors)
            + f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
            f'<p class="sub" style="margin-top:6px">Vertical rates, negative = sinking. Where the study reports a '
            "peak, this run's value is the fastest-moving 2% of its pixels nearby; otherwise the median. "
            f"{e(note)}</p>")


def _references(r) -> str:
    """Data credits, method references, and every study the run was compared against."""
    e = html.escape
    cfg = r.config
    items = []
    if cfg.extra.get("synthetic"):
        items.append("Synthetic demo data generated by OpenSAR Explorer (not real measurements).")
    else:
        if r.nisar_rate is not None:
            items.append("NISAR data: NASA/ISRO, distributed by the Alaska Satellite Facility (ASF) DAAC.")
        if r.s1_rate is not None:
            types = {p.product_type for p in r.s1_products}
            if types - {"LICSAR"}:
                items.append("Contains modified Copernicus Sentinel data; Sentinel-1 interferometric products (ARIA, "
                             "OPERA (NASA JPL) or HyP3 (ASF)) distributed by the ASF DAAC.")
            if "LICSAR" in types:
                from .licsar import CITATION

                items.append("Contains modified Copernicus Sentinel data processed by COMET's LiCSAR system: "
                             + e(CITATION))
    if r.gnss is not None and "Nevada" in r.gnss.source:
        items.append("Blewitt, G., Hammond, W. C., &amp; Kreemer, C. (2018). Harnessing the GPS data explosion for "
                     "interdisciplinary science. <i>Eos</i>, 99. GNSS time series from the Nevada Geodetic "
                     "Laboratory.")
    items.append("Product reading follows the conventions of MintPy: Yunjun, Z., Fattahi, H., &amp; Amelung, F. "
                 "(2019). Small baseline InSAR time series analysis: unwrapping error correction and noise "
                 "reduction. <i>Computers &amp; Geosciences</i>, 133, 104331.")
    seen = set()
    from .validate import egms_defaults

    for raw in cfg.published_maps:
        spec = {**egms_defaults(raw.get("path", "")), **raw}
        cite = str(spec.get("citation") or "").strip()
        if cite and cite not in seen:
            seen.add(cite)
            items.append(f"Published map: {e(cite)}")
    for c in r.literature:
        if c.citation not in seen:
            seen.add(c.citation)
            items.append(f"Published study: {e(c.citation)}")
    return "".join(f"<li>{it}</li>" for it in items)


def build_report(r) -> str:
    cfg, c = r.config, r.comparison
    maps = [rm for rm in (r.nisar_rate, r.s1_rate) if rm is not None]  # one map when a satellite has no data here
    a, sensors = maps[0], [rm.sensor for rm in maps]
    single = len(maps) == 1
    e = html.escape
    w, s, ea, n = r.config.get_aoi().bounds
    synthetic = bool(cfg.extra.get("synthetic"))

    gstats = r.gnss.stats if r.gnss else {}
    if single:
        tiles = [
            (f"{c.coverage_a:.0%}", f"AOI coverage {a.sensor}"),
            (_num(c.noise_a_mm_yr, 0, " mm/yr"), "Rate noise (1σ)"),
            (str(len(c.hotspots)), "Moving areas detected"),
            ("not available", "Sentinel-1" if a.sensor == "NISAR" else "NISAR"),
        ]
    else:
        tiles = [
            (f"{c.coverage_a:.0%} / {c.coverage_b:.0%}", "AOI coverage NISAR / Sentinel-1"),
            (_num(c.pearson_r, 2), "Correlation of rates (r)"),
            (_num(c.rmsd_mm_yr, 1, " mm/yr"), "RMS difference"),
            (f"{c.only_a:.0%}", "Area only NISAR can measure"),
            (str(len(c.hotspots)), "Moving areas detected"),
        ]
    if gstats:
        tiles.append((" / ".join(_num(gstats[k].rmse_mm_yr, 1) for k in sensors if k in gstats),
                      f"RMSE vs GNSS, {' / '.join(k.replace('Sentinel-1', 'S1') for k in sensors if k in gstats)} "
                      "(mm/yr)"))
    tiles_html = "".join(f'<div class="tile"><div class="v">{e(v)}</div><div class="k">{e(k)}</div></div>'
                         for v, k in tiles)

    by_stage: dict[str, list] = {}
    for note in r.notes:
        by_stage.setdefault(note.stage, []).append(note)

    def notes_html(stage):
        items = []
        for note in by_stage.get(stage, []):
            cls, label = LEVEL_LABEL.get(note.level, LEVEL_LABEL["info"])
            items.append(f'<li><span class="tag {cls}">{label}</span><span>{e(note.text)}</span></li>')
        return f'<ul class="notes">{"".join(items)}</ul>' if items else ""

    fig = r.figures
    sections = []
    for stage, title in STAGE_TITLES.items():
        body = notes_html(stage)
        extra = ""
        if stage == "search" and "timeline" in fig:
            extra = _img(fig["timeline"], "Timeline of interferometric pairs")
        if stage == "stack" and "coherence_maps" in fig:
            extra = (_img(fig["coherence_maps"], "Coherence maps side by side")
                     + '<div style="max-width:520px;margin-top:12px">'
                     + _img(fig["coherence_hist"], "Coherence histograms") + "</div>")
        if stage == "compare" and "rate_maps" in fig:
            extra = _img(fig["rate_maps"], "Rate maps side by side and difference")
            if "scatter" in fig:
                extra += ('<div style="max-width:520px;margin-top:12px">'
                          + _img(fig["scatter"], "Scatter of NISAR vs Sentinel-1 rates") + "</div>")
            if "east_west" in fig:
                extra += ('<div style="max-width:760px;margin-top:12px">'
                          + _img(fig["east_west"], "East-west motion from ascending and descending passes") + "</div>")
        if stage == "validate":
            if "gnss" in fig:
                extra += '<div style="max-width:560px">' + _img(fig["gnss"], "InSAR vs GNSS scatter") + "</div>"
            for k in range(len(r.published)):
                extra += _img(fig[f"published_{k}"], "Published velocity map comparison")
            if r.gnss and r.gnss.stations:
                from .validate import station_rate_full

                def cells(st, sn):
                    g, gs, _ = station_rate_full(st, sn)
                    return f"<td>{_num(st.insar.get(sn, float('nan')), 1)}</td><td>{_num(g, 1)} ± {_num(gs)}</td>"

                rows = "".join(
                    f"<tr><td>{e(st.site)}</td><td>{st.lat:.4f}, {st.lon:.4f}</td>"
                    + "".join(cells(st, sn) for sn in sensors)
                    + f"<td>{st.up_mm_yr:+.1f} ± {_num(st.up_sigma_mm_yr)} ({e(st.period)})</td></tr>"
                    for st in r.gnss.stations)
                tied = r.reference is not None and r.reference.mode == "gnss"
                extra += ('<div class="scroll" style="margin-top:12px"><table><thead><tr><th>Station</th>'
                          "<th>Lat, lon</th>"
                          + "".join(f"<th>{e(sn)}</th><th>GNSS, {e(sn)} dates</th>" for sn in sensors)
                          + "<th>GNSS long-term fit</th></tr></thead>"
                          f"<tbody>{rows}</tbody></table></div>"
                          '<p class="sub" style="margin-top:6px">Vertical rates in mm/yr. GNSS is measured over '
                          "each satellite's own dates (position at the end minus position at the start). "
                          + ("InSAR values are tied to GNSS, so they are absolute." if tied else
                             "InSAR values are relative to the reference (before the offset is removed).")
                          + "</p>")
            if r.literature:
                extra += _literature_table(r.literature, sensors)
        if body or extra:
            sections.append(f'<h2>{len(sections) + 1} · {e(title)}</h2><div class="card">{extra}{body}</div>')

    # columns follow maps: c's "a" fields describe maps[0] and its "b" fields maps[1]
    side = [(c.coh_mean_a, c.coverage_a, c.range_a_mm_yr, c.noise_a_mm_yr),
            (c.coh_mean_b, c.coverage_b, c.range_b_mm_yr, c.noise_b_mm_yr)][:len(maps)]
    metrics_rows = [
        ("Pairs used", *(rm.n_pairs for rm in maps)),
        ("Period", *(f"{rm.date_start} → {rm.date_end}" for rm in maps)),
        ("Total time in pairs (days)", *(rm.total_days for rm in maps)),
        ("Wavelength (cm)", *(_num(rm.wavelength * 100) for rm in maps)),
        ("Mean incidence (°)", *(_num(rm.incidence_deg) for rm in maps)),
        ("Mean coherence (AOI)", *(_num(x[0], 2) for x in side)),
        ("AOI coverage", *(f"{x[1]:.0%}" for x in side)),
        (f"Rate range, 2–98% ({a.component}, mm/yr)", *(f"{_num(x[2][0])} … {_num(x[2][1])}" for x in side)),
        ("Rate noise, 1σ (mm/yr)", *(_num(x[3]) for x in side)),
    ]
    mt = "".join("<tr>" + "".join(f"<td>{e(str(v))}</td>" for v in row) + "</tr>" for row in metrics_rows)
    band = {"NISAR": "NISAR (L-band)", "Sentinel-1": "Sentinel-1 (C-band)"}
    head = "".join(f"<th>{e(band.get(sn, sn))}</th>" for sn in sensors)
    seen_words = {"single": "only one satellite has data"}
    hs = "".join(
        f"<tr><td>{e(h.sensor)}</td><td>{h.lat:.4f}, {h.lon:.4f}</td><td>{h.area_km2:.1f}</td>"
        f"<td>{h.peak_mm_yr:+.0f}</td><td>{e(seen_words.get(h.seen_by_other, h.seen_by_other))}</td></tr>"
        for h in c.hotspots) \
        or '<tr><td colspan="5">None above the detection threshold</td></tr>'
    files = "".join(f"<li><code>{e(k)}</code>: {e(v)}</li>" for k, v in r.outputs.items())
    banner = ('<p class="banner"><strong>Synthetic demo data.</strong> These results come from simulated products '
              "built to show how the tool works. They are not real measurements of this location.</p>"
              if synthetic else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenSAR Explorer: Ground Motion report</title><style>{CSS}</style></head>
<body><main>
<p class="sub" style="margin:0 0 2px">OpenSAR Explorer · Ground Motion module · created by {e(__author__)}</p>
<h1>{e(" vs ".join(sensors) if not single else f"{a.sensor} ground motion")}: {e(cfg.name)}</h1>
<p class="sub">AOI {w:.3f}–{ea:.3f}°E, {s:.3f}–{n:.3f}°N · grid {cfg.resolution_m:.0f} m ·
coherence threshold {cfg.coherence_threshold} · {e(a.component)} rates · generated {datetime.now():%Y-%m-%d %H:%M}</p>
{banner}
<div class="tiles">{tiles_html}</div>
{''.join(sections)}
<h2>Side-by-side summary</h2>
<div class="card scroll"><table><thead><tr><th></th>{head}</tr></thead>
<tbody>{mt}</tbody></table></div>
<h2>Moving areas</h2>
<div class="card scroll"><table><thead><tr><th>Detected by</th><th>Lat, lon</th><th>Area (km²)</th>
<th>Peak (mm/yr)</th><th>Seen by the other sensor</th></tr></thead><tbody>{hs}</tbody></table></div>
<h2>References and data credits</h2><div class="card"><ol>{_references(r)}</ol></div>
<h2>Files</h2><div class="card"><ul>{files}</ul></div>
</main></body></html>"""
