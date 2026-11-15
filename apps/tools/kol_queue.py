#!/usr/bin/env python3
"""KOL post queue — drafts that wait for the referral link.

The KOL programme pays weekly in FLOP for activity brought to the network, and the
referral link is the unit of attribution: a post published before the link exists earns
nothing. So the drafts live here with a ``{link}`` slot, and publishing is a two-step:
render with the link, then mark the post as published.

  ko_queue.py --list
  ko_queue.py --render --link https://flop.finance/r/<code>
  ko_queue.py --render --link … --id mcp-tape
  ko_queue.py --mark mcp-tape --link …  (records the published URL and date)

Queue file: docs/kol/queue.json (English copy; the audience is small and technical).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

QUEUE = Path(__file__).resolve().parents[2] / "docs" / "kol" / "queue.json"


def load() -> dict:
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    QUEUE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check_link(link: str) -> str:
    if not link.startswith("https://"):
        raise SystemExit("link must be https")
    return link


def render(post: dict, link: str) -> str:
    text = post["text"].replace("{link}", link)
    if len(text) > 280:
        print(f"  ! {post['id']}: {len(text)} chars — over the 280 limit", file=sys.stderr)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="KOL drafts waiting for the referral link")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--mark", metavar="ID")
    ap.add_argument("--id", dest="only")
    ap.add_argument("--link", default="")
    args = ap.parse_args()

    data = load()
    posts = data.get("posts", [])

    if args.list or not (args.render or args.mark):
        for p in posts:
            status = p.get("status", "draft")
            when = p.get("published_at", "")
            print(f"{p['id']:<14} {status:<10} {when}  {p['text'][:60]}…")
        return 0

    if args.render:
        link = check_link(args.link or data.get("link", ""))
        if not link:
            raise SystemExit("--link is required until the queue has one")
        for p in posts:
            if args.only and p["id"] != args.only:
                continue
            print(f"— {p['id']} ({len(render(p, link))} chars)")
            print(render(p, link))
            print()
        return 0

    if args.mark:
        link = check_link(args.link or data.get("link", ""))
        for p in posts:
            if p["id"] == args.mark:
                p["status"] = "published"
                p["published_at"] = datetime.now(UTC).strftime("%Y-%m-%d")
                p["published_with"] = link
                save(data)
                print(f"marked {args.mark} published")
                return 0
        raise SystemExit(f"no post with id {args.mark}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
