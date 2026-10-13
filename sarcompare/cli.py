"""Command-line interface.

    sarcompare demo                         # synthetic end-to-end run, no credentials needed
    sarcompare search  config.yaml          # list what is available and what would be used
    sarcompare run     config.yaml          # search, download, compare, report
    sarcompare hyp3    config.yaml [--order] # Sentinel-1 interferograms from ASF HyP3 where none are ready-made
"""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .pipeline import Pipeline


def _load(args) -> Config:
    cfg = Config.from_yaml(args.config)
    if getattr(args, "output_dir", None):
        cfg.output_dir = args.output_dir
    if getattr(args, "max_pairs", None):
        cfg.max_pairs = args.max_pairs
    if getattr(args, "data_dir", None):
        cfg.data_dir = args.data_dir
    if getattr(args, "s1_source", None):
        cfg.s1_source = args.s1_source
    return cfg


def cmd_search(args) -> int:
    p = Pipeline(_load(args))
    nisar, s1 = p.search()
    print("\nSelected pairs:")
    for prod in nisar + s1:
        row = prod.to_row()
        size = f"{row['size_mb']} MB" if row["size_mb"] else ""
        print(f"  {row['sensor']:<11} {row['date1']} → {row['date2']} ({row['span_days']} d) "
              f"{row['direction']} track {row['track']}  {size}")
    return 0


def cmd_run(args) -> int:
    result = Pipeline(_load(args)).run(download=not args.no_download)
    print(f"\nReport: {result.outputs['report']}")
    return 0


def cmd_hyp3(args) -> int:
    """Plan (and with --order, submit) Sentinel-1 interferograms on ASF HyP3 matching the NISAR pairs."""
    from .hyp3 import find_products

    p = Pipeline(_load(args))
    p.search()
    pairs, notes = p.hyp3_plan()
    for n in notes:
        print("  " + n.text)
    print(f"\nSentinel-1 pairs to order from ASF HyP3 ({len(pairs)}):")
    for q in pairs:
        print(f"  {q.date1} → {q.date2} ({q.span_days} d) {q.direction.lower()} track {q.path} {q.platform}, "
              f"covers {q.cover:.0%}\n    {q.reference}\n    {q.secondary}")
    from .hyp3 import HyP3Error, access_problem

    client = p._hyp3()
    info = client.user()
    credits = info.get("remaining_credits")  # the account's user name is not printed
    try:
        access_problem(info)
    except HyP3Error as e:
        print(f"\n{e}")
    print(f"\nHyP3 credits left this month: {'no limit' if credits is None else f'{credits:,.0f}'}; "
          f"one pair costs about {client.cost():.0f}.")
    _, counts, status_notes = find_products(client, p.aoi)
    for n in status_notes:
        print("  " + n.text)
    if args.order or args.check_only:
        res = p.hyp3_order(pairs, validate_only=args.check_only)
        for n in res.notes:
            print("  " + n.text)
        if res.skipped:
            print(f"  {res.skipped} pair(s) were already ordered and were not sent again.")
    elif pairs:
        print("\nNothing was ordered. Add --order to submit these jobs (it uses free HyP3 credits; jobs are "
              "publicly visible). Then run the same config again later: finished results are found automatically.")
    return 0


def cmd_demo(args) -> int:
    from .demo import demo_config

    cfg = demo_config(args.data_dir, args.output_dir)
    result = Pipeline(cfg).run(download=False)
    print(f"\nReport: {result.outputs['report']}")
    return 0


def cmd_gui(args) -> int:
    """Open the graphical interface in the web browser (it runs locally; nothing is uploaded)."""
    from pathlib import Path

    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print("The GUI needs streamlit: pip install -e \".[app]\"", file=sys.stderr)
        return 1
    gui = Path(__file__).with_name("gui.py")
    sys.argv = ["streamlit", "run", str(gui), "--server.port", str(args.port),
                "--browser.gatherUsageStats", "false", "--server.headless", "true" if args.no_browser else "false"]
    return stcli.main()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sarcompare", description="OpenSAR Explorer: compare NISAR and Sentinel-1 ground motion over one area.")
    ap.add_argument("--debug", action="store_true", help="show full tracebacks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="search ASF and show the pairs that would be used")
    s.add_argument("config")
    s.add_argument("--max-pairs", type=int, help="image pairs per satellite (overrides the config)")
    s.add_argument("--s1-source", choices=["AUTO", "ARIA_S1_GUNW", "OPERA_DISP_S1", "HYP3", "LICSAR"],
                   help="Sentinel-1 product type")
    s.set_defaults(func=cmd_search)
    r = sub.add_parser("run", help="full workflow: search, download, compare, report")
    r.add_argument("config")
    r.add_argument("--output-dir")
    r.add_argument("--no-download", action="store_true", help="only use files already present locally")
    r.add_argument("--max-pairs", type=int, help="image pairs per satellite (overrides the config)")
    r.add_argument("--data-dir", help="folder for downloaded files (overrides the config)")
    r.add_argument("--s1-source", choices=["AUTO", "ARIA_S1_GUNW", "OPERA_DISP_S1", "HYP3", "LICSAR"],
                   help="Sentinel-1 product type")
    r.set_defaults(func=cmd_run)
    h = sub.add_parser("hyp3", help="order Sentinel-1 interferograms from ASF HyP3 for the NISAR dates")
    h.add_argument("config")
    h.add_argument("--max-pairs", type=int, help="image pairs per flight direction (overrides the config)")
    h.add_argument("--order", action="store_true", help="submit the planned jobs (uses HyP3 credits)")
    h.add_argument("--check-only", action="store_true",
                   help="ask HyP3 to check the jobs without submitting them (only if HyP3 offers this)")
    h.set_defaults(func=cmd_hyp3)
    d = sub.add_parser("demo", help="run on synthetic data (no account or download needed)")
    d.add_argument("--data-dir", default="data/demo")
    d.add_argument("--output-dir", default="outputs")
    d.set_defaults(func=cmd_demo)
    g = sub.add_parser("gui", help="open the graphical interface in your web browser")
    g.add_argument("--port", type=int, default=8501)
    g.add_argument("--no-browser", action="store_true", help="do not open a browser window automatically")
    g.set_defaults(func=cmd_gui)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        if args.debug:
            raise
        msg = str(e)
        if "cmr.earthdata" in msg or "asf.alaska.edu" in msg or "ConnectionError" in type(e).__name__ \
                or "ProxyError" in type(e).__name__:
            msg = f"could not reach NASA/ASF servers ({type(e).__name__}). Check your internet connection."
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
