#!/usr/bin/env python3
"""
Total CL + EL rewards for Ethereum validators — no beaconcha.in / paid APIs.

Sources (all free / public):
  CL  current balance (Beacon API) + withdrawals (Blockscout) − deposit
  EL  MEV-Boost payments via relays (by pubkey) + fee-recipient builder
      payments verified by block_number (includes bloXroute-only blocks)

Windows (all_time / 1y / 30d / 7d / 24h):
  all_time.cl = balance + withdrawn − deposit
  period.cl   = withdrawals credited in that window
                (unwithdrawn CL balance growth is only in all_time)
  period.el   = MEV payments with timestamp in that window

Results are cached in rewards_cache.json (events appended by validator index).
Already-cached validators with event history are skipped unless --refresh.

Examples:
  python3 validator_rewards.py 1629852
  python3 validator_rewards.py 1633531
  python3 validator_rewards.py --show-cache
  python3 validator_rewards.py 1629852 --refresh
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GWEI = 10**9
ETH = 10**18
DEFAULT_DEPOSIT_ETH = 32.0
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / "rewards_cache.json"
RELAY_MAX_WORKERS = max(1, int(os.environ.get("RELAY_MAX_WORKERS", "8")))

MAINNET_GENESIS_TIME = 1606824023
SECONDS_PER_SLOT = 12

# (name, seconds lookback) — None = all time
REWARD_WINDOWS: list[tuple[str, int | None]] = [
    ("all_time", None),
    ("1y", 365 * 86400),
    ("30d", 30 * 86400),
    ("7d", 7 * 86400),
    ("24h", 86400),
]

DEFAULT_BEACON = os.environ.get(
    "BEACON_URL", "https://ethereum-beacon-api.publicnode.com"
).rstrip("/")
DEFAULT_BLOCKSCOUT = os.environ.get(
    "BLOCKSCOUT_URL", "https://eth.blockscout.com"
).rstrip("/")

DEFAULT_RELAYS = [
    "https://boost-relay.flashbots.net",
    "https://relay.ultrasound.money",
    "https://agnostic-relay.net",
    "https://aestus.live",
    "https://mainnet-relay.securerpc.com",
    "https://titanrelay.xyz",
    "https://relay.edennetwork.io",
]

BLOCK_LOOKUP_RELAYS = DEFAULT_RELAYS + [
    "https://bloxroute.max-profit.blxrbdn.com",
    "https://bloxroute.regulated.blxrbdn.com",
]


@dataclass
class ValidatorRewards:
    id: str
    index: int
    pubkey: str
    status: str
    withdrawal_address: str | None
    deposit_wei: int
    balance_wei: int
    withdrawn_wei: int
    cl_rewards_wei: int
    el_rewards_wei: int
    el_payloads: int
    withdrawals_complete: bool
    withdrawal_events: list[dict[str, Any]] = field(default_factory=list)
    el_events: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_wei(self) -> int:
        return self.cl_rewards_wei + self.el_rewards_wei


def wei_to_eth(wei: int) -> float:
    return wei / ETH


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        return ts // 1000 if ts > 10_000_000_000 else ts
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        ts = int(s)
        return ts // 1000 if ts > 10_000_000_000 else ts
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def slot_to_ts(slot: int, genesis_time: int = MAINNET_GENESIS_TIME) -> int:
    return genesis_time + int(slot) * SECONDS_PER_SLOT


def withdrawal_reward_wei(amount_wei: int, deposit_wei: int) -> int:
    """
    Map a withdrawal credit to CL *rewards* only.
    Full exits return principal (~deposit); only the excess is reward.
    Partial skims are entirely reward.
    """
    if amount_wei >= deposit_wei:
        return amount_wei - deposit_wei
    return amount_wei


def build_period_rewards(
    *,
    balance_wei: int,
    deposit_wei: int,
    withdrawal_events: list[dict[str, Any]],
    el_events: list[dict[str, Any]],
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Derive all_time / 1y / 30d / 7d / 24h CL+EL breakdowns."""
    now_ts = int(now_ts or time.time())
    withdrawn_all = sum(int(e["amount_wei"]) for e in withdrawal_events)
    cl_all = balance_wei + withdrawn_all - deposit_wei
    if cl_all < 0:
        cl_all = 0

    out: dict[str, Any] = {}
    for name, seconds in REWARD_WINDOWS:
        since = None if seconds is None else now_ts - seconds

        if name == "all_time":
            cl_wei = cl_all
            cl_withdrawals = len(withdrawal_events)
            cl_method = "balance+withdrawals-deposit"
        else:
            cl_wei = 0
            cl_withdrawals = 0
            for e in withdrawal_events:
                ts = int(e["ts"])
                if since is not None and ts < since:
                    continue
                cl_wei += withdrawal_reward_wei(int(e["amount_wei"]), deposit_wei)
                cl_withdrawals += 1
            cl_method = "withdrawals_in_window_ex_principal"

        el_wei = 0
        el_blocks = 0
        for e in el_events:
            ts = int(e["ts"])
            if since is not None and ts < since:
                continue
            el_wei += int(e["value_wei"])
            el_blocks += 1

        out[name] = {
            "cl_eth": wei_to_eth(cl_wei),
            "el_eth": wei_to_eth(el_wei),
            "total_eth": wei_to_eth(cl_wei + el_wei),
            "cl_wei": cl_wei,
            "el_wei": el_wei,
            "total_wei": cl_wei + el_wei,
            "el_blocks": el_blocks,
            "cl_withdrawals": cl_withdrawals,
            "cl_method": cl_method,
        }
    return out


def rewards_to_record(r: ValidatorRewards, *, fetched_at: str | None = None) -> dict[str, Any]:
    rewards = build_period_rewards(
        balance_wei=r.balance_wei,
        deposit_wei=r.deposit_wei,
        withdrawal_events=r.withdrawal_events,
        el_events=r.el_events,
    )
    return {
        "id": r.id,
        "index": r.index,
        "pubkey": r.pubkey,
        "status": r.status,
        "withdrawal_address": r.withdrawal_address,
        "deposit_wei": r.deposit_wei,
        "balance_wei": r.balance_wei,
        "withdrawn_wei": r.withdrawn_wei,
        "cl_rewards_wei": r.cl_rewards_wei,
        "el_rewards_wei": r.el_rewards_wei,
        "el_payloads": r.el_payloads,
        "withdrawals_complete": r.withdrawals_complete,
        "withdrawal_events": r.withdrawal_events,
        "el_events": r.el_events,
        "notes": r.notes,
        "cl_rewards_eth": wei_to_eth(r.cl_rewards_wei),
        "el_rewards_eth": wei_to_eth(r.el_rewards_wei),
        "total_eth": wei_to_eth(r.total_wei),
        "rewards": rewards,
        "fetched_at": fetched_at or utc_now_iso(),
    }


def record_to_rewards(rec: dict[str, Any]) -> ValidatorRewards:
    return ValidatorRewards(
        id=str(rec.get("id") or rec["index"]),
        index=int(rec["index"]),
        pubkey=str(rec["pubkey"]),
        status=str(rec.get("status") or ""),
        withdrawal_address=rec.get("withdrawal_address"),
        deposit_wei=int(rec["deposit_wei"]),
        balance_wei=int(rec["balance_wei"]),
        withdrawn_wei=int(rec["withdrawn_wei"]),
        cl_rewards_wei=int(rec["cl_rewards_wei"]),
        el_rewards_wei=int(rec["el_rewards_wei"]),
        el_payloads=int(rec.get("el_payloads") or 0),
        withdrawals_complete=bool(rec.get("withdrawals_complete", True)),
        withdrawal_events=list(rec.get("withdrawal_events") or []),
        el_events=list(rec.get("el_events") or []),
        notes=list(rec.get("notes") or []),
    )


def record_has_events(rec: dict[str, Any]) -> bool:
    """Old cache entries without event history cannot serve time windows."""
    return "el_events" in rec and "withdrawal_events" in rec


def enrich_record_windows(rec: dict[str, Any]) -> dict[str, Any]:
    """Recompute rolling windows from stored events (keeps 24h/7d fresh)."""
    out = dict(rec)
    out["rewards"] = build_period_rewards(
        balance_wei=int(rec.get("balance_wei") or 0),
        deposit_wei=int(rec.get("deposit_wei") or 0),
        withdrawal_events=list(rec.get("withdrawal_events") or []),
        el_events=list(rec.get("el_events") or []),
    )
    # Keep top-level all-time aliases in sync
    all_time = out["rewards"]["all_time"]
    out["cl_rewards_wei"] = all_time["cl_wei"]
    out["el_rewards_wei"] = all_time["el_wei"]
    out["el_payloads"] = all_time["el_blocks"]
    out["cl_rewards_eth"] = all_time["cl_eth"]
    out["el_rewards_eth"] = all_time["el_eth"]
    out["total_eth"] = all_time["total_eth"]
    return out


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"updated_at": None, "validators": {}}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return {"updated_at": None, "validators": {}}
    validators = data.get("validators") or {}
    if isinstance(validators, list):
        validators = {str(v["index"]): v for v in validators if "index" in v}
    return {
        "updated_at": data.get("updated_at"),
        "validators": {str(k): v for k, v in validators.items()},
    }


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    cache = {
        "updated_at": utc_now_iso(),
        "validators": cache.get("validators") or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2)
        fh.write("\n")
    tmp.replace(path)


def cache_lookup(cache: dict[str, Any], validator_id: str) -> dict[str, Any] | None:
    validators: dict[str, Any] = cache.get("validators") or {}
    vid = validator_id.lower().removeprefix("0x")
    if validator_id.isdigit() and validator_id in validators:
        return validators[validator_id]
    for rec in validators.values():
        pk = str(rec.get("pubkey") or "").lower().removeprefix("0x")
        rid = str(rec.get("id") or "").lower().removeprefix("0x")
        if vid == pk or vid == rid or validator_id == str(rec.get("index")):
            return rec
    return None


def sum_period_totals(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {}
    for name, _ in REWARD_WINDOWS:
        cl = el = 0
        blocks = 0
        for rec in records:
            rewards = rec.get("rewards") or {}
            period = rewards.get(name) or {}
            cl += int(period.get("cl_wei") or 0)
            el += int(period.get("el_wei") or 0)
            blocks += int(period.get("el_blocks") or 0)
        totals[name] = {
            "cl_eth": wei_to_eth(cl),
            "el_eth": wei_to_eth(el),
            "total_eth": wei_to_eth(cl + el),
            "el_blocks": blocks,
        }
    return totals


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | list | None = None,
    timeout: float = 60.0,
    retries: int = 3,
) -> Any:
    """HTTP JSON via curl (no third-party deps)."""
    last_err: Exception | None = None
    for attempt in range(retries):
        cmd = [
            "curl",
            "-sS",
            "-w",
            "\n%{http_code}",
            "--max-time",
            str(int(timeout)),
            "-H",
            "User-Agent: validator-rewards/1.0",
            "-H",
            "Accept: application/json",
        ]
        if body is not None:
            cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        if method != "GET":
            cmd += ["-X", method]
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, check=False)
            if proc.returncode not in (0, 22) and not proc.stdout:
                err = (proc.stderr or b"").decode(errors="replace").strip()
                raise RuntimeError(err or f"curl exit {proc.returncode}")

            raw = proc.stdout.decode(errors="replace")
            if not raw:
                raise RuntimeError(
                    (proc.stderr or b"").decode(errors="replace") or "empty response"
                )

            if "\n" not in raw:
                raise RuntimeError(f"unexpected curl output: {raw[:200]}")
            body_text, status_s = raw.rsplit("\n", 1)
            status = int(status_s.strip() or "0")

            if status in (400, 404) and "bidtraces" in url:
                return []
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {body_text[:240]}")
            if not body_text:
                return None
            return json.loads(body_text)
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e).lower()
            retryable = any(
                x in msg for x in ("429", "502", "503", "504", "timed out", "timeout")
            )
            if retryable and attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            if attempt + 1 < retries and "http" not in msg:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"request failed: {url}") from last_err


def parse_validators(raw: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in raw:
        for part in item.replace("\n", ",").split(","):
            v = part.strip().lower()
            if v:
                out.append(v)
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def withdrawal_address_from_credentials(creds: str) -> str | None:
    c = creds.lower().removeprefix("0x")
    if len(c) != 64:
        return None
    if c.startswith("01") or c.startswith("02"):
        return "0x" + c[-40:]
    return None


def fetch_validator(beacon: str, validator_id: str) -> dict[str, Any]:
    url = f"{beacon}/eth/v1/beacon/states/head/validators/{urllib.parse.quote(validator_id)}"
    payload = http_json(url)
    if not payload or "data" not in payload:
        raise ValueError(f"validator not found: {validator_id}")
    return payload["data"]


def fetch_genesis_time(beacon: str) -> int:
    try:
        data = http_json(f"{beacon}/eth/v1/beacon/genesis")
        return int(data["data"]["genesis_time"])
    except Exception:
        return MAINNET_GENESIS_TIME


def fetch_withdrawals_by_address(
    blockscout: str,
    address: str,
    *,
    max_pages: int,
) -> tuple[dict[int, list[dict[str, Any]]], bool]:
    """
    Paginate Blockscout address withdrawals.
    Returns (validator_index -> [{amount_wei, ts, block_number}], complete).
    """
    base = f"{blockscout}/api/v2/addresses/{address}/withdrawals"
    params: dict[str, Any] = {}
    by_vi: dict[int, list[dict[str, Any]]] = {}
    pages = 0

    while pages < max_pages:
        qs = urllib.parse.urlencode(params)
        url = f"{base}?{qs}" if qs else base
        data = http_json(url)
        if not data:
            return by_vi, True
        items = data.get("items") or []
        for item in items:
            vi = int(item["validator_index"])
            ts = parse_timestamp(item.get("timestamp"))
            if ts is None:
                continue
            by_vi.setdefault(vi, []).append(
                {
                    "amount_wei": int(item["amount"]),
                    "ts": ts,
                    "block_number": int(item["block_number"])
                    if item.get("block_number") is not None
                    else None,
                }
            )
        nxt = data.get("next_page_params")
        pages += 1
        if not items or not nxt:
            return by_vi, True
        params = dict(nxt)
        if pages % 20 == 0:
            print(
                f"  … {address[:10]} withdrawals page {pages} "
                f"({len(by_vi)} validators seen)",
                file=sys.stderr,
            )
        time.sleep(0.05)

    return by_vi, False


def fetch_el_from_relays(
    pubkey: str, relays: list[str], genesis_time: int
) -> dict[int, dict[str, Any]]:
    """MEV-Boost delivered payloads keyed by slot."""
    pk = pubkey.lower() if pubkey.startswith("0x") else "0x" + pubkey.lower()
    by_slot: dict[int, dict[str, Any]] = {}

    def pull_relay(relay: str) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        cursor: int | None = None
        for _ in range(500):
            q: dict[str, Any] = {"proposer_pubkey": pk, "limit": 100}
            if cursor is not None:
                q["cursor"] = cursor
            url = (
                f"{relay.rstrip('/')}/relay/v1/data/bidtraces/"
                f"proposer_payload_delivered?{urllib.parse.urlencode(q)}"
            )
            try:
                batch = http_json(url, timeout=45.0, retries=2)
            except Exception:
                return collected
            if not isinstance(batch, list) or not batch:
                break

            matched = [
                row
                for row in batch
                if str(row.get("proposer_pubkey", "")).lower() == pk
            ]
            if not matched:
                break

            collected.extend(matched)
            if len(matched) < len(batch) or len(batch) < 100:
                break
            slots = [int(x["slot"]) for x in matched if "slot" in x]
            if not slots:
                break
            next_cursor = min(slots)
            if cursor is not None and next_cursor >= cursor:
                break
            cursor = next_cursor
            time.sleep(0.05)
        return collected

    workers = min(RELAY_MAX_WORKERS, max(1, len(relays)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(pull_relay, r) for r in relays]
        for fut in as_completed(futs):
            for row in fut.result():
                try:
                    slot = int(row["slot"])
                    value = int(row["value"])
                    block_number = int(row["block_number"]) if row.get("block_number") else None
                except (KeyError, TypeError, ValueError):
                    continue
                prev = by_slot.get(slot)
                if prev is None or value > int(prev["value_wei"]):
                    by_slot[slot] = {
                        "slot": slot,
                        "value_wei": value,
                        "ts": slot_to_ts(slot, genesis_time),
                        "block_number": block_number,
                    }
    return by_slot


def _sender_is_mev_builder(from_obj: dict[str, Any] | None) -> bool:
    if not from_obj:
        return False
    tags = (from_obj.get("metadata") or {}).get("tags") or []
    for tag in tags:
        slug = (tag.get("slug") or "").lower()
        name = (tag.get("name") or "").lower()
        if slug == "mev-builder" or "mev builder" in name or "builder" in slug:
            return True
    return False


def fetch_inbound_mev_candidates(
    blockscout: str,
    fee_recipient: str,
    *,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Inbound native txs to fee recipient that look like builder MEV payments."""
    base = f"{blockscout}/api/v2/addresses/{fee_recipient}/transactions"
    params: dict[str, Any] = {"filter": "to"}
    out: list[dict[str, Any]] = []
    pages = 0
    while pages < max_pages:
        qs = urllib.parse.urlencode(params)
        data = http_json(f"{base}?{qs}")
        if not data:
            break
        items = data.get("items") or []
        for tx in items:
            result = str(tx.get("result") or "").lower()
            status = str(tx.get("status") or "").lower()
            if result not in ("success", "ok") and status not in ("ok", "success", "1"):
                continue
            value = int(tx.get("value") or 0)
            if value <= 0:
                continue
            from_obj = tx.get("from") if isinstance(tx.get("from"), dict) else None
            if not _sender_is_mev_builder(from_obj):
                continue
            block_number = tx.get("block_number")
            if block_number is None:
                continue
            ts = parse_timestamp(tx.get("timestamp"))
            out.append(
                {
                    "block_number": int(block_number),
                    "value": value,
                    "hash": tx.get("hash"),
                    "from": (from_obj or {}).get("hash"),
                    "ts": ts,
                }
            )
        nxt = data.get("next_page_params")
        pages += 1
        if not items or not nxt:
            break
        params = dict(nxt)
        params.setdefault("filter", "to")
        time.sleep(0.05)
    return out


def verify_payload_for_block(
    relays: list[str],
    block_number: int,
    pubkey: str,
) -> tuple[int, int] | None:
    """Return (slot, value_wei) if a relay delivered a payload for this pubkey at block."""
    pk = pubkey.lower() if pubkey.startswith("0x") else "0x" + pubkey.lower()
    for relay in relays:
        url = (
            f"{relay.rstrip('/')}/relay/v1/data/bidtraces/proposer_payload_delivered?"
            f"{urllib.parse.urlencode({'block_number': block_number, 'limit': 10})}"
        )
        try:
            batch = http_json(url, timeout=30.0, retries=2)
        except Exception:
            continue
        if not isinstance(batch, list):
            continue
        for row in batch:
            if str(row.get("proposer_pubkey", "")).lower() != pk:
                continue
            try:
                return int(row["slot"]), int(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def fetch_el_events(
    pubkey: str,
    fee_recipient: str | None,
    blockscout: str,
    relays: list[str],
    lookup_relays: list[str],
    genesis_time: int,
) -> list[dict[str, Any]]:
    """All MEV-Boost EL payments for a validator, with timestamps."""
    by_slot = fetch_el_from_relays(pubkey, relays, genesis_time)

    if fee_recipient:
        candidates = fetch_inbound_mev_candidates(blockscout, fee_recipient)
        for cand in candidates:
            verified = verify_payload_for_block(
                lookup_relays, cand["block_number"], pubkey
            )
            if verified is None:
                continue
            slot, value = verified
            ts = cand.get("ts") or slot_to_ts(slot, genesis_time)
            prev = by_slot.get(slot)
            if prev is None or value > int(prev["value_wei"]):
                by_slot[slot] = {
                    "slot": slot,
                    "value_wei": value,
                    "ts": int(ts),
                    "block_number": cand["block_number"],
                }

    return sorted(by_slot.values(), key=lambda e: int(e["ts"]))


def print_table(rows: list[ValidatorRewards]) -> None:
    headers = ["index", "status", "all_time", "1y", "30d", "7d", "24h", "MEV #"]
    table: list[list[str]] = []
    for r in rows:
        rewards = build_period_rewards(
            balance_wei=r.balance_wei,
            deposit_wei=r.deposit_wei,
            withdrawal_events=r.withdrawal_events,
            el_events=r.el_events,
        )
        table.append(
            [
                str(r.index),
                r.status,
                f"{rewards['all_time']['total_eth']:.4f}",
                f"{rewards['1y']['total_eth']:.4f}",
                f"{rewards['30d']['total_eth']:.4f}",
                f"{rewards['7d']['total_eth']:.4f}",
                f"{rewards['24h']['total_eth']:.4f}",
                str(r.el_payloads),
            ]
        )

    widths = [len(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in table:
        print(fmt(row))


def compute_rewards_for_validator(
    vid: str,
    data: dict[str, Any],
    *,
    blockscout: str,
    relays: list[str],
    deposit_wei: int,
    cl_balance_only: bool,
    skip_el: bool,
    fee_recipient_arg: str | None,
    max_withdrawal_pages: int,
    withdrawal_cache: dict[str, tuple[dict[int, list[dict[str, Any]]], bool]],
    genesis_time: int,
) -> ValidatorRewards:
    notes: list[str] = []
    index = int(data["index"])
    pubkey = data["validator"]["pubkey"]
    status = data["status"]
    balance_wei = int(data["balance"]) * GWEI
    creds = data["validator"]["withdrawal_credentials"]
    withdrawal_address = withdrawal_address_from_credentials(creds)

    withdrawal_events: list[dict[str, Any]] = []
    withdrawals_complete = True
    if cl_balance_only:
        notes.append("CL balance-only (withdrawals not fetched)")
        withdrawals_complete = False
    elif withdrawal_address is None:
        notes.append("BLS/0x00 credentials: no EL withdrawals; CL uses balance only")
    else:
        if withdrawal_address not in withdrawal_cache:
            print(f"fetching withdrawals for {withdrawal_address} …", file=sys.stderr)
            withdrawal_cache[withdrawal_address] = fetch_withdrawals_by_address(
                blockscout, withdrawal_address, max_pages=max_withdrawal_pages
            )
        by_vi, complete = withdrawal_cache[withdrawal_address]
        withdrawal_events = list(by_vi.get(index, []))
        withdrawals_complete = complete
        if not complete:
            notes.append(
                f"withdrawal history truncated at {max_withdrawal_pages} pages; "
                "CL may be understated — raise --max-withdrawal-pages"
            )

    withdrawn_wei = sum(int(e["amount_wei"]) for e in withdrawal_events)
    cl_rewards_wei = balance_wei + withdrawn_wei - deposit_wei
    if cl_rewards_wei < 0:
        notes.append("negative CL (check --deposit-eth or incomplete withdrawals)")
        cl_rewards_wei = 0

    el_events: list[dict[str, Any]] = []
    if skip_el:
        notes.append("EL skipped")
    else:
        fee_recipient = (fee_recipient_arg or withdrawal_address or "").lower() or None
        print(
            f"fetching EL for index {index} "
            f"(fee recipient {fee_recipient or 'unknown'}) …",
            file=sys.stderr,
        )
        el_events = fetch_el_events(
            pubkey,
            fee_recipient,
            blockscout,
            relays,
            BLOCK_LOOKUP_RELAYS,
            genesis_time,
        )
        notes.append(
            "EL = MEV-Boost payments (relays + fee-recipient builder txs); "
            "excludes vanilla local-block tips"
        )
        notes.append(
            "period CL (1y/30d/7d/24h) = withdrawals in window minus principal; "
            "unwithdrawn CL is only in all_time"
        )

    el_rewards_wei = sum(int(e["value_wei"]) for e in el_events)
    el_payloads = len(el_events)

    return ValidatorRewards(
        id=vid,
        index=index,
        pubkey=pubkey,
        status=status,
        withdrawal_address=withdrawal_address,
        deposit_wei=deposit_wei,
        balance_wei=balance_wei,
        withdrawn_wei=withdrawn_wei,
        cl_rewards_wei=cl_rewards_wei,
        el_rewards_wei=el_rewards_wei,
        el_payloads=el_payloads,
        withdrawals_complete=withdrawals_complete,
        withdrawal_events=withdrawal_events,
        el_events=el_events,
        notes=notes,
    )


def get_cache_payload(cache_path: Path | None = None) -> dict[str, Any]:
    path = (cache_path or DEFAULT_CACHE_PATH).expanduser().resolve()
    cache = load_cache(path)
    records = [
        enrich_record_windows(r)
        for r in sorted(
            (cache.get("validators") or {}).values(),
            key=lambda r: int(r["index"]),
        )
    ]
    return {
        "cache": str(path),
        "updated_at": cache.get("updated_at"),
        "validators": records,
        "totals": sum_period_totals(records),
    }


def collect_rewards(
    validator_ids: Iterable[str],
    *,
    cache_path: Path | None = None,
    beacon: str = DEFAULT_BEACON,
    blockscout: str = DEFAULT_BLOCKSCOUT,
    relays: list[str] | None = None,
    deposit_eth: float = DEFAULT_DEPOSIT_ETH,
    fee_recipient: str | None = None,
    cl_balance_only: bool = False,
    skip_el: bool = False,
    max_withdrawal_pages: int = 500,
    refresh: bool = False,
    include_events: bool = True,
) -> dict[str, Any]:
    """
    Resolve rewards for validators (cache-aware). Returns the same JSON shape
    as the CLI (validators, run_totals, cache_totals, errors).
    """
    path = (cache_path or DEFAULT_CACHE_PATH).expanduser().resolve()
    cache = load_cache(path)
    validators = parse_validators(validator_ids)
    if not validators:
        raise ValueError("provide at least one validator index or pubkey")

    beacon = beacon.rstrip("/")
    blockscout = blockscout.rstrip("/")
    relay_list = relays or list(DEFAULT_RELAYS)
    deposit_wei = int(deposit_eth * ETH)
    genesis_time = fetch_genesis_time(beacon)

    response_records: list[dict[str, Any]] = []
    errors: list[str] = []
    withdrawal_cache: dict[str, tuple[dict[int, list[dict[str, Any]]], bool]] = {}
    cache_dirty = False

    for vid in validators:
        cached = None if refresh else cache_lookup(cache, vid)
        if cached is not None and not record_has_events(cached):
            print(
                f"cache stale {vid} → missing event history, re-fetching",
                file=sys.stderr,
            )
            cached = None

        if cached is not None:
            fetched_at = cached.get("fetched_at") or "?"
            print(
                f"cache hit  {vid} → index {cached['index']}  (fetched_at={fetched_at})",
                file=sys.stderr,
            )
            rec = enrich_record_windows(cached)
            notes = list(rec.get("notes") or [])
            if not any("from cache" in n for n in notes):
                notes.append(f"from cache ({fetched_at})")
            rec["notes"] = notes
            response_records.append(rec)
            continue

        try:
            data = fetch_validator(beacon, vid)
            print(f"ok  {vid} → index {data['index']}", file=sys.stderr)
            row = compute_rewards_for_validator(
                vid,
                data,
                blockscout=blockscout,
                relays=relay_list,
                deposit_wei=deposit_wei,
                cl_balance_only=cl_balance_only,
                skip_el=skip_el,
                fee_recipient_arg=fee_recipient,
                max_withdrawal_pages=max_withdrawal_pages,
                withdrawal_cache=withdrawal_cache,
                genesis_time=genesis_time,
            )
            rec = rewards_to_record(row)
            cache.setdefault("validators", {})[str(row.index)] = rec
            response_records.append(rec)
            cache_dirty = True
            print(f"cached index {row.index} → {path.name}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{vid}: {e}")
            print(f"err {vid}: {e}", file=sys.stderr)

    if cache_dirty:
        save_cache(path, cache)
        print(f"wrote cache {path}", file=sys.stderr)

    all_records = [
        enrich_record_windows(r) for r in (cache.get("validators") or {}).values()
    ]

    if not include_events:
        response_records = [_strip_events(r) for r in response_records]

    return {
        "cache": str(path),
        "validators": response_records,
        "run_totals": sum_period_totals(response_records),
        "cache_totals": sum_period_totals(all_records),
        "errors": errors,
    }


def _strip_events(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    out.pop("withdrawal_events", None)
    out.pop("el_events", None)
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("validators", nargs="*", help="Validator index(es) and/or pubkey(s)")
    p.add_argument("--file", "-f", help="File with indices/pubkeys (comma or newline)")
    p.add_argument("--beacon", default=DEFAULT_BEACON, help="Beacon API base URL")
    p.add_argument(
        "--blockscout",
        default=DEFAULT_BLOCKSCOUT,
        help="Blockscout base URL (withdrawal history)",
    )
    p.add_argument(
        "--deposit-eth",
        type=float,
        default=DEFAULT_DEPOSIT_ETH,
        help="ETH deposited per validator (default 32)",
    )
    p.add_argument(
        "--relays",
        default=",".join(DEFAULT_RELAYS),
        help="Comma-separated MEV-Boost relay base URLs (pubkey filter)",
    )
    p.add_argument(
        "--fee-recipient",
        help="Fee recipient for EL/MEV payments (default: withdrawal address)",
    )
    p.add_argument(
        "--cl-balance-only",
        action="store_true",
        help="CL = balance − deposit only (skip withdrawal history; faster)",
    )
    p.add_argument(
        "--max-withdrawal-pages",
        type=int,
        default=500,
        help="Max Blockscout pages per withdrawal address (50 rows/page)",
    )
    p.add_argument("--skip-el", action="store_true", help="Skip MEV-Boost EL lookup")
    p.add_argument(
        "--cache",
        default=str(DEFAULT_CACHE_PATH),
        help=f"Local JSON cache path (default: {DEFAULT_CACHE_PATH.name})",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch even if validator is already in the cache",
    )
    p.add_argument(
        "--show-cache",
        action="store_true",
        help="Return all cached validators as JSON (no fetch) and exit",
    )
    p.add_argument(
        "--table",
        action="store_true",
        help="Print a human-readable table instead of JSON",
    )
    p.add_argument(
        "--no-events",
        action="store_true",
        help="Omit withdrawal_events / el_events from JSON output",
    )
    args = p.parse_args()

    cache_path = Path(args.cache).expanduser().resolve()

    if args.show_cache:
        payload = get_cache_payload(cache_path)
        if args.no_events:
            payload["validators"] = [_strip_events(r) for r in payload["validators"]]
        if args.table:
            rows = [record_to_rewards(r) for r in (load_cache(cache_path).get("validators") or {}).values()]
            rows = sorted(rows, key=lambda r: r.index)
            if not rows:
                print(f"cache empty: {cache_path}")
            else:
                print(f"cache: {cache_path}  updated_at={payload.get('updated_at')}")
                print_table(rows)
        else:
            print(json.dumps(payload, indent=2))
        return 0

    ids = list(args.validators)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            ids.extend(fh.read().splitlines())
    validators = parse_validators(ids)
    if not validators:
        p.error("provide at least one validator index/pubkey (or --file / --show-cache)")

    relays = [r.strip() for r in args.relays.split(",") if r.strip()]
    try:
        payload = collect_rewards(
            validators,
            cache_path=cache_path,
            beacon=args.beacon,
            blockscout=args.blockscout,
            relays=relays,
            deposit_eth=args.deposit_eth,
            fee_recipient=args.fee_recipient,
            cl_balance_only=args.cl_balance_only,
            skip_el=args.skip_el,
            max_withdrawal_pages=args.max_withdrawal_pages,
            refresh=args.refresh,
            include_events=not args.no_events,
        )
    except ValueError as e:
        p.error(str(e))

    if args.table:
        rows = [record_to_rewards(r) for r in payload["validators"]]
        # re-attach events for table windows if stripped
        if rows:
            print_table(rows)
        if payload["errors"]:
            print("\nErrors:", file=sys.stderr)
            for err in payload["errors"]:
                print(f"  {err}", file=sys.stderr)
    else:
        print(json.dumps(payload, indent=2))

    return 1 if payload["errors"] and not payload["validators"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
