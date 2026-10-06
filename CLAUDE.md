# CLAUDE.md — Poland (GPW Warsaw) Valuation Screener

Fifth market after Korea, the UK, Japan and Germany. Same maths (`screener.py`),
same three screens (vs peers, outright, vs own history), same thresholds,
except the liquidity gate (see below). Universe: GPW Main Market (402 companies).

## Run order

```bash
python test_poland.py     # offline, must pass
python check_setup.py     # seven live checks
python main_pl.py -v      # full run, ~3 min cold, seconds warm (.pl_cache/)
python serve.py           # dashboard with working Refresh (dashboard.cmd)
python sync_dashboard_from_korea.py ../Korea/dashboard.py   # dashboard.py is generated
```

## Polish-specific invariants

1. **Roster = GPW's own company search** (AJAX POST with the page's full form;
   omitting any filter group returns nothing). Sector via one POST per code.
   GPW resets connections and rejects a bare UA - every request retries.
2. **Peers: GPW sector → macro-sector (hundreds digit)**, except Real Estate
   (140) gets its own macro: GPW files it under Finance, which benchmarked
   developers against banks.
3. **Currency trap** (`repair_foreign_reporters`): on PLN lines of EUR/USD
   reporters Yahoo's EPS/mcap/EV are PLN but book value, EBITDA, NI are in the
   reporting currency → P/B, EV/EBITDA, ROE off ~4x. Repaired per row by test;
   refused (NaN) when a test fails. Validated vs GPW P/BV (Asbis 6.94 vs 6.92).
   Runs after `restate_info_for_splits`.
4. **Market cap from GPW** when Yahoo has a price but no share count
   (Niewiadów, ROBYG) - `mcap_via_gpw` in the funnel.
5. **Ownership from GPW's shareholder register** (factsheet shareholdersTab,
   weekly cache): `state` = Treasury/state funds/state-controlled listed cos
   ≥25% votes (chained: Pekao ← PZU ← Treasury); `ctrl` = one holder ≥50%.
   Pension/TFI holders never count. Flags, not gates (`--exclude-state`).
6. Excluded: investment vehicles (sector 180), landlords ("real estate rent"
   only — developers stay), foreign issuers outside WIG20/mWIG40/sWIG80
   (Santander, UniCredit, CEZ, MOL, Krka).
7. One listed line per company on GPW - no share-class step.

## Liquidity gate: USD 1m (not 4m) — measured 2026-10-05

| ADV floor | names | relative / absolute / history / any |
|---|---|---|
| none | 53 | 9 / 2 / 1 / 11 |
| USD 1m (default) | 36 | 4 / 2 / 1 / 7 |
| USD 4m | 23 | 2 / 1 / 1 / 4 |

At 4m the screen is WIG20 plus a handful. 1m mostly removes free-float-starved
controlled names (BNP BP, Energa, Polenergia, ASEE).

## Known gaps

1. **Thin cohorts**: with 36 survivors only Finance and Trade & Services reach
   5 peers; ~16 names (energy, mining, IT, telecoms, consumer) are unscored on
   the relative screen. `--fallback-peer-keys board` benchmarks them against
   the whole surviving market (100% coverage) - an option, not the default.
2. NewConnect not read (nothing clears USD 600m).
3. Ownership needs GPW's register; if the factsheet calls fail, flags are
   simply absent (`ownership_unknown` in the funnel), not guessed.

## Publishing

`.github/workflows/screen.yml` runs at 17:00 UTC on weekdays (after the GPW
close in both CET and CEST), builds `site/`, and deploys to GitHub Pages, with
a retry after a pause for Yahoo throttling.

Live: https://shkon1215-netizen.github.io/poland-screener/. Unlisted -
`noindex` plus a blanket `robots.txt` - but the repo is public, which free
Pages requires. No screen output is committed.

**GPW and Yahoo both answer GitHub's runners** - confirmed on the first run,
2026-10-06: roster 402 / 44 sector codes, 38 GPW factsheets fetched with no
failures, no retry needed, and the funnel matched the local run line for line
(53 / 36 / 7). If the roster step ever fails in CI while working locally,
suspect GPW's WAF blocking the runner range before a code change.

A docs-only push does not trigger a run (`paths-ignore: **.md`); start one with
`gh workflow run screen.yml`.

Research tool, not investment advice.
