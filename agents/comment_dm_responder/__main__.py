"""CLI: python -m agents.comment_dm_responder --post "..." --comments-file ./comments.txt"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from core import status_bus

from .agent import CommentDMResponderAgent


def main() -> int:
    load_dotenv()
    status_bus.init_db()

    parser = argparse.ArgumentParser(description="Draft public reply + DM for each comment on your IG post.")
    parser.add_argument("--post", default="", help="Brief context for the original post (topic, CTA, what was promised).")
    parser.add_argument("--payload", default="", help="What goes in the DM (link, quiz, content) — the thing the post promised.")
    parser.add_argument("--comments", default="", help="Pasted comment blob, separated by --- lines.")
    parser.add_argument(
        "--comments-file",
        default="",
        help="Path to a text file containing the comment blob.",
    )
    args = parser.parse_args()

    comments = args.comments
    if args.comments_file:
        from pathlib import Path
        comments = (comments + "\n\n" + Path(args.comments_file).read_text()).strip()

    if not comments.strip():
        print("error: --comments or --comments-file is required", file=sys.stderr)
        return 2

    agent = CommentDMResponderAgent()
    result = agent.run(
        task=f"Comment→Reply+DM batch ({args.post[:40] or 'unspecified post'})",
        post_context=args.post,
        dm_payload=args.payload,
        comments=comments,
    )

    print()
    print(f"✓ Saved → {result['path']}")
    print(f"  Comments: {result['comment_count']}  Replies: {result['public_replies']}  DMs: {result['dms_drafted']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
