#!/usr/bin/env python3
"""Flask API for Ethereum validator CL + EL rewards."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from flask import Flask, jsonify, request

import validator_rewards as vr

APP_DIR = Path(__file__).resolve().parent
CACHE_PATH = Path(
    os.environ.get("REWARDS_CACHE", APP_DIR / "data" / "rewards_cache.json")
).expanduser()


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_validator_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            parts.extend(str(item).replace("\n", ",").split(","))
        return vr.parse_validators(parts)
    return vr.parse_validators([str(raw)])


def create_app() -> Flask:
    if not shutil.which("curl"):
        print("WARNING: curl not found on PATH", file=sys.stderr)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    legacy = APP_DIR / "rewards_cache.json"
    if legacy.exists() and not CACHE_PATH.exists():
        try:
            legacy.replace(CACHE_PATH)
        except OSError:
            pass

    application = Flask(__name__)
    application.json.sort_keys = False

    @application.get("/")
    def index():
        return jsonify(
            {
                "name": "validator-rewards-api",
                "endpoints": {
                    "GET /health": "Liveness check",
                    "GET /api/cache": "All cached validators + period totals",
                    "GET /api/rewards/<validator_id>": "One validator (index or pubkey)",
                    "GET /api/rewards?validators=847291,1203847": "One or more validators",
                    "POST /api/rewards": 'JSON body: {"validators":[...],"refresh":false}',
                },
                "query_params": {
                    "validators": "Comma-separated indices/pubkeys",
                    "refresh": "true to bypass cache and re-fetch",
                    "events": "true to include withdrawal_events/el_events (default false)",
                    "skip_el": "true to skip MEV/EL lookup",
                    "cl_balance_only": "true for balance-only CL",
                },
                "rewards_windows": [name for name, _ in vr.REWARD_WINDOWS],
                "cache": str(CACHE_PATH),
            }
        )

    @application.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "cache_exists": CACHE_PATH.exists(),
                "curl": bool(shutil.which("curl")),
            }
        )

    @application.get("/api/cache")
    def api_cache():
        payload = vr.get_cache_payload(CACHE_PATH)
        if not _truthy(request.args.get("events"), default=False):
            payload["validators"] = [vr._strip_events(r) for r in payload["validators"]]
        return jsonify(payload)

    @application.get("/api/rewards/<validator_id>")
    def api_rewards_one(validator_id: str):
        return _rewards_response([validator_id])

    @application.get("/api/rewards")
    def api_rewards_get():
        validators = _parse_validator_list(request.args.get("validators"))
        if not validators:
            return jsonify({"error": "pass ?validators=index1,index2"}), 400
        return _rewards_response(validators)

    @application.post("/api/rewards")
    def api_rewards_post():
        body = request.get_json(silent=True) or {}
        validators = _parse_validator_list(
            body.get("validators") or body.get("validator") or body.get("ids")
        )
        if not validators:
            return jsonify({"error": "JSON body must include validators: [...]"}), 400
        return _rewards_response(
            validators,
            refresh=_truthy(
                str(body["refresh"]) if "refresh" in body else request.args.get("refresh")
            ),
            include_events=_truthy(
                str(body["events"]) if "events" in body else request.args.get("events"),
                default=False,
            ),
            skip_el=_truthy(
                str(body["skip_el"]) if "skip_el" in body else request.args.get("skip_el")
            ),
            cl_balance_only=_truthy(
                str(body["cl_balance_only"])
                if "cl_balance_only" in body
                else request.args.get("cl_balance_only")
            ),
            deposit_eth=float(
                body.get("deposit_eth", request.args.get("deposit_eth", 32))
            ),
            fee_recipient=body.get("fee_recipient") or request.args.get("fee_recipient"),
        )

    def _rewards_response(
        validators: list[str],
        *,
        refresh: bool | None = None,
        include_events: bool | None = None,
        skip_el: bool | None = None,
        cl_balance_only: bool | None = None,
        deposit_eth: float | None = None,
        fee_recipient: str | None = None,
    ):
        if refresh is None:
            refresh = _truthy(request.args.get("refresh"))
        if include_events is None:
            include_events = _truthy(request.args.get("events"), default=False)
        if skip_el is None:
            skip_el = _truthy(request.args.get("skip_el"))
        if cl_balance_only is None:
            cl_balance_only = _truthy(request.args.get("cl_balance_only"))
        if deposit_eth is None:
            deposit_eth = float(request.args.get("deposit_eth", 32))
        if fee_recipient is None:
            fee_recipient = request.args.get("fee_recipient")

        try:
            payload = vr.collect_rewards(
                validators,
                cache_path=CACHE_PATH,
                refresh=refresh,
                include_events=include_events,
                skip_el=skip_el,
                cl_balance_only=cl_balance_only,
                deposit_eth=deposit_eth,
                fee_recipient=fee_recipient,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 502

        status = 200
        if payload.get("errors") and not payload.get("validators"):
            status = 502
        elif payload.get("errors"):
            status = 207
        return jsonify(payload), status

    return application


app = create_app()


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    threads = max(1, int(os.environ.get("WAITRESS_THREADS", "8")))
    channel_timeout = int(os.environ.get("WAITRESS_CHANNEL_TIMEOUT", "300"))

    try:
        from waitress import serve
    except ImportError:
        print("waitress not installed; falling back to Flask dev server", file=sys.stderr)
        app.run(host=host, port=port, debug=False, threaded=True)
        return

    print(
        f"Serving on http://{host}:{port}  threads={threads}  "
        f"timeout={channel_timeout}s  cache={CACHE_PATH}",
        file=sys.stderr,
    )
    serve(
        app,
        host=host,
        port=port,
        threads=threads,
        channel_timeout=channel_timeout,
        ident="validator-rewards",
    )


if __name__ == "__main__":
    main()
