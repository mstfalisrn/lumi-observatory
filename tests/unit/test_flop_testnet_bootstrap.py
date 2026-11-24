"""Testnet discovery: two surfaces publish, the docs carry the live values."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps" / "tools"))

import flop_testnet_bootstrap as boot


def test_origin_strips_the_page_path() -> None:
    assert boot._origin("https://faucet.flop.network/chain") == "https://faucet.flop.network"
    assert boot._origin("https://api.flop.network/v1/models") == "https://api.flop.network"
    assert boot._origin("") == ""


def test_plain_text_drops_scripts_and_tags() -> None:
    html = "<style>x</style><p>Genesis block hash 0xabc</p><script>y</script>"
    assert boot._plain(html).split() == ["Genesis", "block", "hash", "0xabc"]
