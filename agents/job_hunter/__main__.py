"""CLI: python -m agents.job_hunter --target-role "Staff PM" --location "Bangalore" --keywords "AI,marketplace"."""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from core import status_bus

from .agent import JobHunterAgent


def main() -> int:
    load_dotenv()
    status_bus.init_db()

    parser = argparse.ArgumentParser(
        description="Mine LinkedIn jobs via Apify, rank against the master profile, "
                    "and write tailored CV markdowns into the cv-builder project.",
    )
    parser.add_argument("--target-role", required=True, help='e.g. "Staff Product Manager"')
    parser.add_argument("--location", default="", help='e.g. "Bangalore" or "Berlin" or "Remote"')
    parser.add_argument("--keywords", default="", help='Comma-separated, e.g. "AI,marketplace,B2B SaaS"')
    parser.add_argument(
        "--region",
        choices=("india", "europe"),
        default="india",
        help="Which CV template family to target (affects the prompt's templating hints).",
    )
    parser.add_argument(
        "--scrape-limit", type=int, default=10,
        help="Daily cap on jobs surfaced (max 10). Splits 5/5 across LinkedIn + Wellfound, then dedupes.",
    )
    parser.add_argument(
        "--tailor-limit", type=int, default=5,
        help="How many tailored CVs to write (max 10). Must be ≤ scrape-limit.",
    )
    args = parser.parse_args()

    agent = JobHunterAgent()
    result = agent.run(
        task=f"job_hunter · {args.target_role} · {args.location or 'any loc'}",
        target_role=args.target_role,
        location=args.location,
        keywords=args.keywords,
        region=args.region,
        scrape_limit=args.scrape_limit,
        tailor_limit=args.tailor_limit,
    )

    print()
    print(f"✓ Saved → {result['path']}")
    print(f"  Scraped: {result['jobs_scraped']} jobs · CVs written: {result['cvs_written']}")
    for j in result["top_jobs"][:10]:
        print(f"  - [{j['fit']:>3}] cv builder/jobs/{j['slug']}/  ({j['header']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
