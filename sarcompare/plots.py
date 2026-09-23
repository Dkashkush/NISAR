"""Figures for the comparison. Each figure can be rendered in a light or dark theme."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

from .compare import Comparison  # noqa: E402
from .processing import RateMap, Reference  # noqa: E402

# Categorical identity: NISAR = slot 1 (blue), Sentinel-1 = slot 2 (orange); fixed everywhere.
THEMES = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9",
                  axis="#c3c2b7", nodata="#e1e0d9", mid="#f0efec",
                  series={"NISAR": "#2a78d6", "Sentinel-1": "#eb6834"}),
    "dark": dict(surface="#1a1a19", ink="#ffffff", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a",
                 axis="#383835", nodata="#2c2c2a", mid="#383835",
                 series={"NISAR": "#3987e5", "Sentinel-1": "#d95926"}),
}


def _diverging(theme: dict) -> LinearSegmentedColormap:
    # red = moving away / subsiding (negative), blue = toward / uplift (positive), neutral grey midpoint
    return LinearSegmentedColormap.from_list(
        "updown", ["#8f1d1d", "#e34948", "#f2a3a2", theme["mid"], "#86b6ef", "#2a78d6", "#0d366b"])


def _sequential(theme: dict) -> LinearSegmentedColormap:
    lo = "#cde2fb" if theme is THEMES["light"] else "#184f95"
    hi = "#0d366b" if theme is THEMES["light"] else "#cde2fb"
    return LinearSegmentedColormap.from_list("coh", [lo, hi])


def _style(fig, axes, t):
    fig.patch.set_facecolor(t["surface"])
    for ax in np.atleast_1d(axes).ravel():
        ax.set_facecolor(t["nodata"])
        for s in ax.spines.values():
            s.set_color(t["axis"])
        ax.tick_params(colors=t["muted"], labelsize=8)
        ax.xaxis.label.set_color(t["ink2"])
        ax.yaxis.label.set_color(t["ink2"])
        ax.title.set_color(t["ink"])


def _extent(da):
    x, y = da.x.values / 1000, da.y.values / 1000
    dx = abs(x[1] - x[0]) / 2 if x.size > 1 else 0.5
    dy = abs(y[1] - y[0]) / 2 if y.size > 1 else 0.5
    return [x.min() - dx, x.max() + dx, y.min() - dy, y.max() + dy]


def _map_row(panels, cmap, vmin, vmax, cbar_label, ref: Reference | None, t, title):
    fig, axes = plt.subplots(1, len(panels), figsize=(4.2 * len(panels), 4.3), constrained_layout=True)
    _style(fig, axes, t)
    im = None
    for ax, (label, da) in zip(axes, panels):
        im = ax.imshow(da.values, extent=_extent(da), cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
        ax.set_title(label, fontsize=10, loc="left", color=t["ink"])
        ax.set_xlabel("Easting (km)", fontsize=8)
        if ax is axes[0]:
            ax.set_ylabel("Northing (km)", fontsize=8)
        if ref is not None:
            ax.plot(ref.x / 1000, ref.y / 1000, marker="^", ms=9, mfc=t["ink"], mec=t["surface"], mew=1.5)
    cb = fig.colorbar(im, ax=axes, shrink=0.85, pad=0.01)
    cb.set_label(cbar_label, color=t["ink2"], fontsize=8)
    cb.ax.tick_params(colors=t["muted"], labelsize=8)
    cb.outline.set_edgecolor(t["axis"])
    fig.suptitle(title, color=t["ink"], fontsize=11, x=0.01, ha="left")
    return fig


def rate_maps(a: RateMap, b: RateMap, c: Comparison, ref: Reference, theme="light"):
    t = THEMES[theme]
    ra, rb = a.rate * 1000, b.rate * 1000
    vals = np.concatenate([ra.values[np.isfinite(ra.values)], rb.values[np.isfinite(rb.values)]])
    lim = float(np.nanpercentile(np.abs(vals), 98)) if vals.size else 10.0
    lim = max(lim, 5.0)
    comp = a.component
    panels = [(f"{a.sensor}", ra), (f"{b.sensor}", rb), (f"{a.sensor} − {b.sensor}", c.difference * 1000)]
    return _map_row(panels, _diverging(t), -lim, lim, f"{comp} rate (mm/yr) · blue = up/toward, red = down/away",
                    ref, t, f"Mean {comp} displacement rate (▲ = reference point; grey = no reliable data)")


def coherence_maps(a: RateMap, b: RateMap, theme="light"):
    t = THEMES[theme]
    panels = [(a.sensor, a.coherence), (b.sensor, b.coherence)]
    return _map_row(panels, _sequential(t), 0, 1, "mean coherence (0 = noise, 1 = perfect)", None, t,
                    "Coherence: how much of the radar signal stays stable between passes")


def scatter(a: RateMap, b: RateMap, c: Comparison, theme="light"):
    t = THEMES[theme]
    ra, rb = a.rate.values.ravel() * 1000, b.rate.values.ravel() * 1000
    ok = np.isfinite(ra) & np.isfinite(rb)
    fig, ax = plt.subplots(figsize=(4.8, 4.5), constrained_layout=True)
    _style(fig, ax, t)
    ax.set_facecolor(t["surface"])
    if ok.sum() > 0:
        lo = float(np.percentile(np.concatenate([ra[ok], rb[ok]]), 0.5))
        hi = float(np.percentile(np.concatenate([ra[ok], rb[ok]]), 99.5))
        pad = (hi - lo) * 0.05 or 1
        lo, hi = lo - pad, hi + pad
        ax.hexbin(ra[ok], rb[ok], gridsize=45, extent=(lo, hi, lo, hi), mincnt=1, bins="log",
                  cmap=_sequential(t), linewidths=0)
        ax.plot([lo, hi], [lo, hi], color=t["muted"], lw=1, ls="--", label="1:1")
        if np.isfinite(c.slope):
            mx, my = ra[ok].mean(), rb[ok].mean()
            xs = np.array([lo, hi])
            ax.plot(xs, my + c.slope * (xs - mx), color=t["ink"], lw=2, label=f"fit, slope {c.slope:.2f}")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        leg = ax.legend(frameon=False, fontsize=8, loc="upper left")
        for txt in leg.get_texts():
            txt.set_color(t["ink2"])
    ax.grid(color=t["grid"], lw=0.5)
    ax.set_axisbelow(True)
    ax.set_xlabel(f"{a.sensor} rate (mm/yr)", fontsize=9)
    ax.set_ylabel(f"{b.sensor} rate (mm/yr)", fontsize=9)
    r = f"r = {c.pearson_r:.2f}" if np.isfinite(c.pearson_r) else "r = n/a"
    ax.set_title(f"Pixel-by-pixel agreement ({r}, n = {c.n_joint:,})", fontsize=10, loc="left", color=t["ink"])
    return fig


def coherence_hist(a: RateMap, b: RateMap, threshold: float, inside, theme="light"):
    t = THEMES[theme]
    fig, ax = plt.subplots(figsize=(4.8, 3.4), constrained_layout=True)
    _style(fig, ax, t)
    ax.set_facecolor(t["surface"])
    bins = np.linspace(0, 1, 41)
    for rm in (a, b):
        v = rm.coherence.values[inside]
        v = v[np.isfinite(v)]
        ax.hist(v, bins=bins, histtype="step", lw=2, color=t["series"][rm.sensor], label=rm.sensor,
                weights=np.full(v.size, 100 / max(v.size, 1)))
    ax.axvline(threshold, color=t["muted"], ls="--", lw=1)
    ax.text(threshold + 0.01, ax.get_ylim()[1] * 0.92, f"threshold {threshold}", color=t["ink2"], fontsize=8)
    ax.grid(axis="y", color=t["grid"], lw=0.5)
    ax.set_axisbelow(True)
    ax.set_xlabel("mean coherence", fontsize=9)
    ax.set_ylabel("% of AOI pixels", fontsize=9)
    leg = ax.legend(frameon=False, fontsize=8)
    for txt in leg.get_texts():
        txt.set_color(t["ink2"])
    ax.set_title("Coherence distribution", fontsize=10, loc="left", color=t["ink"])
    return fig


def timeline(pairs_by_sensor: dict, theme="light"):
    """Each selected pair drawn as a bar from its first to second acquisition date."""
    t = THEMES[theme]
    rows = [(s, p) for s, ps in pairs_by_sensor.items() for p in ps]
    fig, ax = plt.subplots(figsize=(9, 0.35 * max(len(rows), 4) + 1.0), constrained_layout=True)
    _style(fig, ax, t)
    ax.set_facecolor(t["surface"])
    labels = []
    for i, (sensor, (d1, d2)) in enumerate(rows):
        ax.barh(i, (d2 - d1).days, left=matplotlib.dates.date2num(d1), height=0.6, color=t["series"][sensor],
                edgecolor=t["surface"], linewidth=2)
        labels.append(f"{sensor} {d1:%d %b %Y}")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(labels, fontsize=8, color=t["ink2"])
    ax.invert_yaxis()
    ax.xaxis_date()
    ax.grid(axis="x", color=t["grid"], lw=0.5)
    ax.set_axisbelow(True)
    ax.set_title("Interferometric pairs used (bar = time between the two acquisitions)", fontsize=10, loc="left", color=t["ink"])
    return fig


def to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()
