# Running a FLOP validator

The validator role is the cheapest way into the network: BABE block authoring,
AlephBFT finality and a DA worker on a **CPU-only** host — no GPU, no TEE. It is also
a separate genesis cohort (1.2B FLOP, 1,000 seats), so it is worth preparing even when
the agent side is blocked.

Admission is administrator-assisted. Nothing in this repository registers, funds or
signs anything: FLOP sends an onboarding package and a private DA peer directory, and
their operations team promotes the node. Treat the missing package as a blocker, not
as an invitation to improvise an endpoint.

## 1. Check the host before asking for anything

```bash
FLOP_VALIDATOR_SS58=<your account> python apps/tools/flop_validator_preflight.py
```

It checks, in order: `linux/amd64`, cores (4 floor, 8 preferred), RAM (16 GB floor),
free disk (100 GB floor), the P2P and DA ports (`30333`, `7001`), the RPC and its
**genesis pin**, the extracted package and its `SHA256SUMS`, `da-peers.json`, and the
named account. The exit code names the first blocker:

| exit | meaning |
|---|---|
| 0 | host and package agree; admission itself is FLOP's call |
| 2 | not `linux/amd64` |
| 3 | cores, RAM or disk below the floor |
| 4 | `30333` or `7001` already bound |
| 5 | RPC unreachable or genesis mismatch |
| 6 | onboarding package missing |
| 8 | `da-peers.json` missing |
| 9 | no account supplied |

A genesis mismatch means the host is pointed at the wrong network: stop before signing
anything.

## 2. What only FLOP can supply

- the versioned operator release (archive + checksum) and its supported network;
- the funded account and the current stake requirement;
- the approved private DA routes (`da-peers.json`);
- promotion after registration — "ready" is not "active".

## 3. Onboarding sequence (as published)

1. Verify the archive checksum, extract it on the dedicated host, install its
   dependencies with the lockfile (`--frozen-lockfile --ignore-scripts`), then run the
   package's `onboard prepare`.
2. Send operations the printed hostname, the public SS58 address and the host's
   **public** SSH key, then confirm the key fingerprint over a second channel. The
   public key encrypts the enrollment key; it grants no access in either direction.
3. Receive access, funding and `da-peers.json`, then run `onboard join`. It waits for
   sync, registers with owner-bound keys and restores a loopback-only RPC.
4. After operations confirms activation, run `onboard verify` against your own node and
   confirm session/BABE/Aleph keys **and advancing finality**.
5. Upgrades follow the rolling-update manual: no re-registration, no genesis change, and
   a rollback path that preserves data.

## 4. What we keep on our side

Keys stay on the host. The account is named in configuration, never derived from chat,
and no key, transaction hash or peer list is committed to this repository. The
preflight reads only public material and writes nothing.
