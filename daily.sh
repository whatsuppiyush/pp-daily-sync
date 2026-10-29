#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
PY=${PY:-.venv/bin/python}
LOG=cache/daily-$(date +%F).log; mkdir -p cache
dow=$(date +%u)
dom=$(date +%d)
{
  echo "== $(date)"
  $PY run.py producthunt --min-votes 0   # every launch: under-30 makers get their own bucket and campaign
  [[ $dow == 1 ]] && $PY run.py launches --limit 300
  [[ $dow == 2 ]] && $PY run.py revenue --limit 200
  $PY run.py chrome-store --limit 3000
  $PY run.py shopify-store --limit 1000
  $PY run.py showhn --limit 500 --opt days=2
  [[ $dow == 7 ]] && $PY run.py marketingdb --limit 300      # weekly (Sunday): only listings not seen before, ~10-50 a week
  [[ $dow == 7 ]] && $PY outscraper.py --signal mdb_week --apply --max-usd 0.50   # Sunday: paid fallback for what the crawl missed
  $PY run.py play --limit 300
  [[ $dow == 1 || $dow == 4 ]] && $PY run.py stores --limit 200
  [[ $dow == 3 ]] && $PY run.py hiring --limit 300 --opt query=marketing
  [[ $dow == 3 ]] && $PY run.py hiring --limit 200 --opt query=marketing --opt location=india
  [[ $dow == 6 ]] && $PY run.py hiring --limit 300 --opt query=growth
  [[ $dow == 7 ]] && $PY run.py firefox --limit 500
  [[ $dom == 01 ]] && $PY run.py funded --limit 200
  $PY yelp.py --rotate 2 --limit 40      # 2 category x city searches a day, ~\$0.45 of Apify
  $PY verify.py
  $PY names.py --apply                   # founder names from every source's records onto unnamed leads (DB only)
  $PY run.py retag
  $PY enrich_companies.py                # DR + category for new companies, DR re-checked every 90 days
  $PY sync_outreach.py                   # every campaign's lead state from Instantly into Neon's outreach ledger
  $PY route_outreach.py --apply          # daily: new launches -> BacklinkBot, every other campaign topped up to ~2 weeks
  $PY report.py
} >> "$LOG" 2>&1
