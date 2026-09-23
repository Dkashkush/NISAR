"""Self-contained HTML report (figures embedded, light/dark aware)."""

from __future__ import annotations

import base64
import html
import math
from datetime import datetime

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


def build_report(r) -> str:
    cfg, c, a, b = r.config, r.comparison, r.nisar_rate, r.s1_rate
    e = html.escape
    w, s, ea, n = r.config.get_aoi().bounds
    synthetic = bool(cfg.extra.get("synthetic"))

    gstats = r.gnss.stats if r.gnss else {}
    tiles = [
        (f"{c.coverage_a:.0%} / {c.coverage_b:.0%}", "AOI coverage NISAR / Sentinel-1"),
        (_num(c.pearson_r, 2), "Correlation of rates (r)"),
        (_num(c.rmsd_mm_yr, 1, " mm/yr"), "RMS difference"),
        (f"{c.only_a:.0%}", "Area only NISAR can measure"),
        (str(len(c.hotspots)), "Moving areas detected"),
    ]
    if gstats:
        tiles.append((" / ".join(_num(gstats[k].rmse_mm_yr, 1) for k in ("NISAR", "Sentinel-1") if k in gstats),
                      "RMSE vs GNSS, NISAR / S1 (mm/yr)"))
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
            extra = (_img(fig["rate_maps"], "Rate maps side by side and difference")
                     + '<div style="max-width:520px;margin-top:12px">'
                     + _img(fig["scatter"], "Scatter of NISAR vs Sentinel-1 rates") + "</div>")
        if stage == "validate":
            if "gnss" in fig:
                extra += '<div style="max-width:560px">' + _img(fig["gnss"], "InSAR vs GNSS scatter") + "</div>"
            for k in range(len(r.published)):
                extra += _img(fig[f"published_{k}"], "Published velocity map comparison")
            if r.gnss and r.gnss.stations:
                rows = "".join(
                    f"<tr><td>{e(st.site)}</td><td>{st.lat:.4f}, {st.lon:.4f}</td><td>{st.up_mm_yr:+.1f}"
                    f" ± {_num(st.up_sigma_mm_yr)}</td>"
                    + "".join(f"<td>{_num(st.insar.get(sn, float('nan')), 1)}</td>" for sn in ("NISAR", "Sentinel-1"))
                    + f"<td>{e(st.period)}</td></tr>" for st in r.gnss.stations)
                extra += ('<div class="scroll" style="margin-top:12px"><table><thead><tr><th>Station</th>'
                          "<th>Lat, lon</th><th>GNSS up (mm/yr)</th><th>NISAR (mm/yr)</th>"
                          "<th>Sentinel-1 (mm/yr)</th><th>GNSS period</th></tr></thead>"
                          f"<tbody>{rows}</tbody></table></div>"
                          '<p class="sub" style="margin-top:6px">InSAR values are relative to the reference '
                          "point (before the offset is removed).</p>")
        if body or extra:
            sections.append(f'<h2>{len(sections) + 1} · {e(title)}</h2><div class="card">{extra}{body}</div>')

    metrics_rows = [
        ("Pairs used", a.n_pairs, b.n_pairs),
        ("Period", f"{a.date_start} → {a.date_end}", f"{b.date_start} → {b.date_end}"),
        ("Total time in pairs (days)", a.total_days, b.total_days),
        ("Wavelength (cm)", _num(a.wavelength * 100), _num(b.wavelength * 100)),
        ("Mean incidence (°)", _num(a.incidence_deg), _num(b.incidence_deg)),
        ("Mean coherence (AOI)", _num(c.coh_mean_a, 2), _num(c.coh_mean_b, 2)),
        ("AOI coverage", f"{c.coverage_a:.0%}", f"{c.coverage_b:.0%}"),
        (f"Rate range, 2–98% ({a.component}, mm/yr)", f"{_num(c.range_a_mm_yr[0])} … {_num(c.range_a_mm_yr[1])}",
         f"{_num(c.range_b_mm_yr[0])} … {_num(c.range_b_mm_yr[1])}"),
        ("Rate noise, 1σ (mm/yr)", _num(c.noise_a_mm_yr), _num(c.noise_b_mm_yr)),
    ]
    mt = "".join(f"<tr><td>{e(str(k))}</td><td>{e(str(x))}</td><td>{e(str(y))}</td></tr>" for k, x, y in metrics_rows)
    hs = "".join(
        f"<tr><td>{e(h.sensor)}</td><td>{h.lat:.4f}, {h.lon:.4f}</td><td>{h.area_km2:.1f}</td>"
        f"<td>{h.peak_mm_yr:+.0f}</td><td>{e(h.seen_by_other)}</td></tr>" for h in c.hotspots) \
        or '<tr><td colspan="5">None above the detection threshold</td></tr>'
    files = "".join(f"<li><code>{e(k)}</code>: {e(v)}</li>" for k, v in r.outputs.items())
    banner = ('<p class="banner"><strong>Synthetic demo data.</strong> These results come from simulated products '
              "built to show how the tool works. They are not real measurements of this location.</p>"
              if synthetic else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NISAR vs Sentinel-1</title><style>{CSS}</style></head>
<body><main>
<h1>NISAR vs Sentinel-1: {e(cfg.name)}</h1>
<p class="sub">AOI {w:.3f}–{ea:.3f}°E, {s:.3f}–{n:.3f}°N · grid {cfg.resolution_m:.0f} m ·
coherence threshold {cfg.coherence_threshold} · {e(a.component)} rates · generated {datetime.now():%Y-%m-%d %H:%M}</p>
{banner}
<div class="tiles">{tiles_html}</div>
{''.join(sections)}
<h2>Side-by-side summary</h2>
<div class="card scroll"><table><thead><tr><th></th><th>NISAR (L-band)</th><th>Sentinel-1 (C-band)</th></tr></thead>
<tbody>{mt}</tbody></table></div>
<h2>Moving areas</h2>
<div class="card scroll"><table><thead><tr><th>Detected by</th><th>Lat, lon</th><th>Area (km²)</th>
<th>Peak (mm/yr)</th><th>Seen by the other sensor</th></tr></thead><tbody>{hs}</tbody></table></div>
<h2>Files</h2><div class="card"><ul>{files}</ul></div>
</main></body></html>"""
