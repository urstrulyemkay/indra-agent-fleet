"""CLI: python -m agents.digital_delivery --email a@b.com [--email c@d.com ...]
         python -m agents.digital_delivery --list ./emails.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from .agent import DigitalDeliveryAgent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a signed download link for your digital product to subscriber(s)"
    )
    parser.add_argument("--email", action="append", dest="emails", metavar="EMAIL",
                        help="Recipient email (repeat for multiple)")
    parser.add_argument("--list", dest="email_file", metavar="FILE",
                        help="Path to newline-separated email list")
    args = parser.parse_args()

    emails: list[str] = list(args.emails or [])
    if args.email_file:
        p = Path(args.email_file)
        if not p.exists():
            print(f"File not found: {p}", file=sys.stderr)
            sys.exit(1)
        emails += [e.strip() for e in p.read_text().splitlines() if e.strip()]

    if not emails:
        parser.print_help()
        sys.exit(1)

    agent = DigitalDeliveryAgent()
    result = agent.run(task="deliver", emails=emails)
    print(result)


if __name__ == "__main__":
    main()
