"""KOL queue: drafts stay inert until a referral link exists."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "apps" / "tools"
sys.path.insert(0, str(TOOLS))

import kol_queue

POSTS = {
    "link": "",
    "posts": [
        {"id": "a", "status": "draft", "text": "first post {link}"},
        {"id": "b", "status": "draft", "text": "second post {link}"},
    ],
}


@pytest.fixture()
def queue(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(POSTS), encoding="utf-8")
    monkeypatch.setattr(kol_queue, "QUEUE", path)
    return path


def test_link_must_be_https() -> None:
    with pytest.raises(SystemExit):
        kol_queue.check_link("http://flop.finance/r/x")


def test_render_substitutes_the_link(queue) -> None:
    data = kol_queue.load()
    assert kol_queue.render(data["posts"][0], "https://x/y") == "first post https://x/y"


def test_mark_records_date_and_link(queue, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["kol_queue.py", "--mark", "b", "--link", "https://x/y"])
    assert kol_queue.main() == 0
    saved = json.loads(queue.read_text(encoding="utf-8"))
    marked = next(p for p in saved["posts"] if p["id"] == "b")
    assert marked["status"] == "published"
    assert marked["published_with"] == "https://x/y"


def test_render_without_link_is_refused(queue, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kol_queue.py", "--render"])
    with pytest.raises(SystemExit):
        kol_queue.main()
