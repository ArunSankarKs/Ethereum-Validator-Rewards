# Validator rewards (CL + EL)

Small Flask API that totals Ethereum validator rewards:

- **CL** — balance + withdrawals − 32 ETH
- **EL** — MEV-Boost payments (relays + fee-recipient builder txs)

Also breaks out **all_time / 1y / 30d / 7d / 24h**. Uses free public endpoints (no beaconcha.in). Results are cached under `data/`.

## Run

```bash
docker compose up -d --build
```

API: `http://localhost:5001`

```bash
curl http://localhost:5001/health
curl "http://localhost:5001/api/rewards?validators=847291,1203847"
```

Stop with `docker compose down`. Logs: `docker compose logs -f`.

## API

| | |
|--|--|
| `GET /api/rewards?validators=847291,1203847` | fetch / return from cache |
| `GET /api/rewards/<id>` | one validator |
| `POST /api/rewards` | `{"validators":["1","2"],"refresh":true}` |
| `GET /api/cache` | everything in the cache |

Useful query flags: `refresh=true`, `events=true`, `skip_el=true`.

First fetch for a new validator can take a couple minutes. Later calls hit the cache.

## Local (no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
# or: python validator_rewards.py 847291
```

Needs `curl` on the PATH.

## Notes

- Period CL (1y/30d/…) is withdrawals in that window (principal stripped on full exits). Unwithdrawn CL only shows up in `all_time`.
- EL skips vanilla local-block tips; MEV-Boost only.
- Cache file: `data/rewards_cache.json` (mounted as a volume in Compose).
