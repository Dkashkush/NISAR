"""Command-line interface.

    sarcompare demo                         # synthetic end-to-end run, no credentials needed
    sarcompare search  config.yaml          # list what is available and what would be used
    sarcompare run     config.yaml          # search, download, compare, report
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


def cmd_demo(args) -> int:
    from .demo import demo_config

    cfg = demo_config(args.data_dir, args.output_dir)
    result = Pipeline(cfg).run(download=False)
    print(f"\nReport: {result.outputs['report']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sarcompare", description="Compare NISAR and Sentinel-1 InSAR over one area.")
    ap.add_argument("--debug", action="store_true", help="show full tracebacks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="search ASF and show the pairs that would be used")
    s.add_argument("config")
    s.set_defaults(func=cmd_search)
    r = sub.add_parser("run", help="full workflow: search, download, compare, report")
    r.add_argument("config")
    r.add_argument("--output-dir")
    r.add_argument("--no-download", action="store_true", help="only use files already present locally")
    r.set_defaults(func=cmd_run)
    d = sub.add_parser("demo", help="run on synthetic data (no account or download needed)")
    d.add_argument("--data-dir", default="data/demo")
    d.add_argument("--output-dir", default="outputs")
    d.set_defaults(func=cmd_demo)
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
