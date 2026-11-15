#!/usr/bin/env python3
"""FLOP validator preflight — what we can check before FLOP admits us.

The validator role needs no GPU or TEE: a CPU node authoring with BABE, voting with
AlephBFT and serving the assigned DA shard. Admission is administrator-assisted, so the
honest report has two halves: what the host can prove, and what only FLOP can provide
(the onboarding package, the DA peer directory, the funded account).

Checks run in this order and stop at the first hard blocker:

  0 ready        1 usage        2 architecture   3 resources   4 ports
  5 rpc/genesis  6 package      7 checksum       8 da peers    9 identity

Nothing here registers, signs or spends; it only inspects the host and the package.
Environment:
  FLOP_VALIDATOR_DIR   extracted onboarding package (contains README + da-peers.json)
  FLOP_DA_PEERS        explicit path to da-peers.json
  FLOP_VALIDATOR_SS58  the account FLOP will fund
  FLOP_RPC             defaults to https://rpc.devnet.flop.network
  FLOP_GENESIS         expected genesis hash (default: the devnet pin below)
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

DEVNET_GENESIS = "0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631"
DEVNET_RPC = "https://rpc.devnet.flop.network"
REQUIRED_PORTS = {30333: "p2p", 7001: "da"}
MIN_CORES = 4
PREFERRED_CORES = 8
MIN_RAM_GB = 16
MIN_DISK_GB = 100


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        self.lines.append(f"  ok    {label}{(' — ' + detail) if detail else ''}")

    def warn(self, label: str, detail: str = "") -> None:
        self.lines.append(f"  warn  {label}{(' — ' + detail) if detail else ''}")

    def bad(self, label: str, detail: str = "") -> None:
        self.lines.append(f"  BLOCK {label}{(' — ' + detail) if detail else ''}")

    def show(self) -> None:
        for line in self.lines:
            print(line)


def _rpc(method: str, params: list | None = None, rpc: str = DEVNET_RPC) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    if not rpc.startswith(("http://", "https://")):
        raise ValueError("RPC must be http(s)")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        rpc, data=body, headers={"Content-Type": "application/json", "User-Agent": "lumi/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - scheme checked above
        return json.loads(resp.read())


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET6 if socket.has_ipv6 else socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            return True
        except OSError:
            return False


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    rep = Report()
    rpc = os.environ.get("FLOP_RPC", DEVNET_RPC)
    genesis_pin = os.environ.get("FLOP_GENESIS", DEVNET_GENESIS)
    pkg_env = os.environ.get("FLOP_VALIDATOR_DIR", "").strip()
    pkg_dir = Path(pkg_env).expanduser() if pkg_env else None
    peers_env = os.environ.get("FLOP_DA_PEERS", "").strip()
    peers = Path(peers_env) if peers_env else (pkg_dir / "da-peers.json" if pkg_dir else None)
    ss58 = os.environ.get("FLOP_VALIDATOR_SS58", "").strip()

    print("== host ==")
    arch = platform.machine()
    if arch == "x86_64":
        rep.ok("architecture", arch)
    else:
        rep.bad("architecture", f"{arch} — the release targets linux/amd64")

    cores = os.cpu_count() or 0
    if cores >= PREFERRED_CORES:
        rep.ok("cpu", f"{cores} cores")
    elif cores >= MIN_CORES:
        rep.warn("cpu", f"{cores} cores — the guide assumes a dedicated host; {PREFERRED_CORES}+ recommended")
    else:
        rep.bad("cpu", f"{cores} cores — below the {MIN_CORES}-core floor")

    mem_gb = 0.0
    try:
        mem_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError):
        pass
    if mem_gb >= MIN_RAM_GB:
        rep.ok("ram", f"{mem_gb:.0f} GB")
    else:
        rep.bad("ram", f"{mem_gb:.0f} GB — below {MIN_RAM_GB} GB")

    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb >= MIN_DISK_GB:
        rep.ok("disk", f"{free_gb:.0f} GB free")
    else:
        rep.bad("disk", f"{free_gb:.0f} GB free — below {MIN_DISK_GB} GB")

    print("== ports ==")
    blocked = []
    for port, role in REQUIRED_PORTS.items():
        if port_free(port):
            rep.ok(f"tcp/{port}", f"{role} free")
        else:
            rep.bad(f"tcp/{port}", f"{role} already bound")
            blocked.append(port)

    print("== chain identity ==")
    chain_ok = True
    try:
        genesis = _rpc("chain_getBlockHash", [0], rpc).get("result", "")
        if str(genesis).lower() == genesis_pin.lower():
            rep.ok("genesis", genesis[:22] + "…")
        else:
            rep.bad("genesis", f"{str(genesis)[:22]}… != pin {genesis_pin[:22]}…")
            chain_ok = False
        health = _rpc("system_health", [], rpc).get("result", {})
        rep.ok("rpc", f"peers={health.get('peers')} syncing={health.get('isSyncing')}")
    except Exception as exc:
        rep.bad("rpc", f"{type(exc).__name__} — {rpc}")
        chain_ok = False

    print("== package ==")
    pkg_ok = False
    if pkg_dir is not None and pkg_dir.is_dir():
        readme = next((p for p in pkg_dir.iterdir() if p.name.lower().startswith("readme")), None)
        if readme:
            rep.ok("onboarding package", str(pkg_dir))
            pkg_ok = True
        else:
            rep.bad("onboarding package", f"{pkg_dir} has no README — is this the extracted archive?")
    else:
        rep.bad(
            "onboarding package",
            "not present — FLOP supplies it after admission (set FLOP_VALIDATOR_DIR)",
        )

    sums = pkg_dir / "SHA256SUMS" if pkg_dir is not None else None
    if sums and sums.is_file():
        bad = []
        for line in sums.read_text().splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            digest, name = parts[0], parts[1].lstrip("*")
            target = pkg_dir / name
            if target.is_file() and sha256(target) != digest.lower():
                bad.append(name)
        if bad:
            rep.bad("checksums", "mismatch: " + ", ".join(bad[:3]))
        else:
            rep.ok("checksums", "SHA256SUMS verified")

    print("== admission ==")
    if peers and peers.is_file():
        try:
            data = json.loads(peers.read_text())
            count = len(data) if isinstance(data, list) else len(data.get("peers", []))
            rep.ok("da-peers.json", f"{count} routes")
        except ValueError:
            rep.bad("da-peers.json", "unreadable JSON")
    else:
        rep.bad("da-peers.json", "missing — operations supplies the approved private DA routes")

    if ss58:
        rep.ok("account", ss58)
    else:
        rep.bad("account", "FLOP_VALIDATOR_SS58 not set — operations funds a named account")

    rep.show()

    verdict = 0
    if arch != "x86_64":
        verdict = 2
    elif cores < MIN_CORES or mem_gb < MIN_RAM_GB or free_gb < MIN_DISK_GB:
        verdict = 3
    elif blocked:
        verdict = 4
    elif not chain_ok:
        verdict = 5
    elif not pkg_ok:
        verdict = 6
    elif not (peers and peers.is_file()):
        verdict = 8
    elif not ss58:
        verdict = 9

    print()
    if verdict == 0:
        print("verdict: ready for the onboarding steps (admission itself is FLOP's call)")
    else:
        print(f"verdict: blocked (exit {verdict}) — the first BLOCK line above is the fix")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
