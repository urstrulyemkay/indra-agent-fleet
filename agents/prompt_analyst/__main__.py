"""CLI entry-point: python -m agents.prompt_analyst [--weeks 4]"""

from __future__ import annotations

import argparse
import sys

from .agent import PromptAnalystAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse your Claude Code prompt history.")
    parser.add_argument("--weeks", type=int, default=4, help="How many weeks to look back (default 4)")
    args = parser.parse_args()

    agent = PromptAnalystAgent()
    try:
        report = agent.run(
            f"Prompt efficiency analysis — last {args.weeks} weeks",
            weeks=args.weeks,
        )
        print("\n" + "=" * 70)
        print(report)
        print("=" * 70)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
