#!/usr/bin/env python3
"""FLOP chain client — the four agent-grant legs, through the published Python SDK.

``flop-sdk`` 0.2.0 (2026-10-29) ships the pieces the protocol needs from us:

* ``flop_sdk.agent_wallet.agent_transfer`` — the ``agent_transfer`` leg
* ``FlopClient.buy_verified_inference`` — a compute purchase that opens the channel,
  pays the miner and returns an ``InferenceReceipt``
* ``SubstrateClient`` — balances, HTLCs, PoUI submission

Two of our four legs are therefore reachable. ``open_channel``, ``top_up_escrow`` and
``force_open`` are not exposed by the SDK yet; this connector reports ``sdk_call_missing``
for them rather than inventing an interface.

Controls:
  FLOP_CHAIN_RPC      node RPC endpoint (no verified public devnet value yet)
  FLOP_CHAIN_SEED     hex seed for the account; defaults to the DID key on disk
  FLOP_CHAIN_SS58     default 42
  FLOP_SDK_ENABLED    default false — chain calls are refused until an endpoint exists

The SDK is an optional extra (``pip install .[flop]``): the import is lazy so the rest of
LUMI keeps working without substrate-interface installed.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path

DEFAULT_SEED_PATH = "/root/secrets/lumi-observatory/did.ed25519"
SDK_LEGS = ("agent_transfer", "compute_completion")
MISSING_LEGS = ("open_channel", "top_up_escrow", "force_open")


def _classify(exc: Exception) -> dict:
    """Name the failure so a runtime gap is not mistaken for a funding problem."""
    text = str(exc)
    if "Unsupported runtime spec version" in text:
        return {"reason": "sdk_runtime_unsupported"}
    if "Insufficient" in text or "FundsUnavailable" in text or "balance" in text.lower():
        return {"reason": "insufficient_balance"}
    if "TEE" in text or "tee" in text:
        return {"reason": "tee_requirement"}
    return {"reason": "chain_error"}


class FlopChainError(RuntimeError):
    pass


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class FlopChainConnector:
    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        seed_path: str = DEFAULT_SEED_PATH,
        ss58_format: int | None = None,
        client_factory=None,
        enabled: bool | None = None,
    ) -> None:
        self.rpc_url = (rpc_url if rpc_url is not None else os.environ.get("FLOP_CHAIN_RPC", "")).strip()
        self.seed_path = seed_path
        self.ss58_format = int(ss58_format or os.environ.get("FLOP_CHAIN_SS58", "42"))
        self._client_factory = client_factory
        self._enabled = _flag("FLOP_SDK_ENABLED") if enabled is None else enabled
        self._client = None

    # -- capability ---------------------------------------------------------------
    def seed_hex(self) -> str:
        inline = os.environ.get("FLOP_CHAIN_SEED", "").strip()
        if inline:
            return inline if inline.startswith("0x") else "0x" + inline
        raw = Path(self.seed_path).read_bytes()
        if len(raw) != 32:
            raise FlopChainError(f"seed must be 32 bytes, got {len(raw)}")
        return "0x" + raw.hex()

    def available(self) -> bool:
        if not self._enabled or not self.rpc_url:
            return False
        try:
            self.seed_hex()
        except OSError:
            return False
        try:
            import flop_sdk  # noqa: F401
        except ImportError:
            return False
        return True

    def reason(self) -> str | None:
        if not self._enabled:
            return "sdk_disabled"
        if not self.rpc_url:
            return "rpc_unset"
        try:
            self.seed_hex()
        except OSError:
            return "seed_missing"
        try:
            import flop_sdk  # noqa: F401
        except ImportError:
            return "sdk_missing"
        return None

    def _build_client(self):
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory(self.rpc_url, self.seed_hex(), self.ss58_format)
            return self._client
        from flop_sdk import FlopClient, FlopClientOptions  # local import: optional extra

        self._client = FlopClient(
            FlopClientOptions(rpc_url=self.rpc_url, seed=self.seed_hex(), ss58_format=self.ss58_format)
        )
        return self._client

    # -- legs ---------------------------------------------------------------------
    async def buy_inference(self, model: str, prompt: str, max_flop: float) -> dict:
        """Buy verified inference: opens the channel, pays the miner, returns a receipt."""
        reason = self.reason()
        if reason:
            return {"ok": False, "reason": reason, "detail": "chain client unavailable"}
        try:
            client = await asyncio.to_thread(self._build_client)
            params = {"model": model, "prompt": prompt, "max_flop": float(max_flop)}
            receipt = client.buy_verified_inference(params)
            # 0.3.0 turned the client async; 0.2.x was sync. Support both.
            if inspect.isawaitable(receipt):
                receipt = await receipt
            return {
                "ok": True,
                "tx": getattr(receipt, "tx_hash", ""),
                "hash_lock": getattr(receipt, "hash_lock", ""),
                "gn_used": getattr(receipt, "gn_used", None),
                "block": getattr(receipt, "block_number", None),
            }
        except Exception as exc:  # SDK raises its own FLOPError tree
            return {"ok": False, **_classify(exc), "detail": str(exc)[:300]}

    async def agent_transfer(self, destination: str, amount: float) -> dict:
        reason = self.reason()
        if reason:
            return {"ok": False, "reason": reason, "detail": "chain client unavailable"}
        try:
            client = await asyncio.to_thread(self._build_client)

            def _call():
                from flop_sdk.agent_wallet import agent_transfer

                return agent_transfer(client, destination, float(amount))

            tx = await asyncio.to_thread(_call)
            if inspect.isawaitable(tx):
                tx = await tx
            return {"ok": True, "tx": tx, "destination": destination, "amount": amount}
        except Exception as exc:
            return {"ok": False, **_classify(exc), "detail": str(exc)[:300]}

    async def call(self, leg: str, **kw) -> dict:
        if leg == "compute_completion":
            return await self.buy_inference(
                model=kw.get("model", ""), prompt=kw.get("prompt", ""), max_flop=kw.get("max_flop", 0)
            )
        if leg == "agent_transfer":
            return await self.agent_transfer(kw.get("destination", ""), kw.get("amount", 0))
        if leg in MISSING_LEGS:
            return {
                "ok": False,
                "reason": "sdk_call_missing",
                "detail": f"flop-sdk 0.2.0 does not expose {leg} yet",
            }
        return {"ok": False, "reason": "unknown_call", "detail": leg}
