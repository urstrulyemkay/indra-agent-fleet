"""CLI entry: python -m agents.startup_lookup [--window-days 14] [--stages ...]"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from .agent import ALLOWED_STAGES, StartupLookupAgent


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="agents.startup_lookup")
    p.add_argument("--window-days", type=int, default=14,
                   help="How far back to look in RSS feeds (default 14).")
    p.add_argument("--stages", default="pre-seed,seed,series_a,series_b,series_c",
                   help=f"Comma list. Allowed: {','.join(sorted(ALLOWED_STAGES))}")
    p.add_argument("--company-cap", type=int, default=10,
                   help="Render top N matched companies (default 10).")
    p.add_argument("--jobs-count", type=int, default=40,
                   help="How many LinkedIn jobs to fetch in the broad search (default 40).")
    p.add_argument("--location", default="India",
                   help="LinkedIn location filter (default 'India').")
    args = p.parse_args(argv)

    stages = tuple(s.strip().lower() for s in args.stages.split(",") if s.strip())
    bad = [s for s in stages if s not in ALLOWED_STAGES]
    if bad:
        print(f"Unknown stages: {bad}. Allowed: {sorted(ALLOWED_STAGES)}", file=sys.stderr)
        return 2

    agent = StartupLookupAgent()
    result = agent.run(
        task=f"startup_lookup · {args.window_days}d · {args.location}",
        window_days=args.window_days,
        stages=stages,
        company_cap=args.company_cap,
        jobs_count=args.jobs_count,
        location=args.location,
    )
    print(f"\nWrote: {result['path']}")
    print(f"Deals: {result['deals']} · Jobs: {result['jobs']} · Matches: {result['matched']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
