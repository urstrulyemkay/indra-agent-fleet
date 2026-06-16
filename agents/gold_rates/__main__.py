"""CLI: python -m agents.gold_rates"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

from .agent import GoldRatesAgent


def main() -> None:
    agent = GoldRatesAgent()
    result = agent.run(task="report")
    print(result)


if __name__ == "__main__":
    main()
