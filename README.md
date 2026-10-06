# Poland (GPW Warsaw) Relative Valuation Screener

**Live:** https://shkon1215-netizen.github.io/poland-screener/ · rebuilt each
weekday after the GPW close

Finds Warsaw-listed stocks trading at a discount to their sector peers on P/E,
P/B and EV/EBITDA — and, independently, stocks that are cheap in absolute
terms, and stocks that are cheap against their own filed history. Each row also
carries three years of revenue, EBITDA and net profit, and who controls the
company.

**Defaults:** market cap ≥ USD 600M · median daily traded value ≥ USD 1M ·
≥20% below peer median on ≥2 of 3 metrics · ROE ≥ 5%.

The fifth market after [Korea](https://github.com/shkon1215-netizen/korea-screener),
the UK, Japan and [Germany](https://github.com/shkon1215-netizen/germany-screener).
`screener.py` is the same maths; what is local is the data layer, the currency
repair and the ownership flags.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
python test_poland.py    # offline logic check, no network needed
python check_setup.py    # tests every live call individually
python main_pl.py -v     # full run, ~3 minutes cold, seconds warm
```

```bash
python main_pl.py --skip-liquidity             # size alone gates
python main_pl.py --min-adv 4e6                # the cross-market liquidity bar
python main_pl.py --fallback-peer-keys board   # unscored names vs the whole market
python main_pl.py --exclude-state              # drop state-controlled companies
python main_pl.py --include-property --all     # see why landlords are off
```

Results cache to `.pl_cache/` per session date, so re-runs are fast — and so a
throttled Yahoo cannot silently shrink the universe.

## The dashboard

Every run writes a self-contained `pl_dashboard.html`. Open it directly, or run
`python serve.py` (on Windows, double-click `dashboard.cmd`) to make the
**Refresh** button work. Every threshold on the page is re-evaluated in the
browser; peer medians and sub-floor market caps cannot be, and the page says so.

## Where the data comes from

| | source |
|---|---|
| roster, sector, index membership | GPW's own company search — every Main Market company, GPW sector codes, WIG20 / mWIG40 / sWIG80 |
| shareholders (≥5%), missing market caps | GPW company factsheets |
| multiples, EPS, book, yield | Yahoo `.info` on the `.WA` line |
| liquidity | one batched Yahoo price download, 60-session median traded value |
| filed statements | Yahoo annual income statement and balance sheet |

## Polish traps this handles

**Accounts in euros, price in zloty.** Pepco and Asbis report in EUR / USD.
Yahoo divides a zloty price by a euro book value, so their P/B and EV/EBITDA
read about 4x too high. Each is repaired per company after testing which
currency every field is in; a figure that fails the test is left blank.

**Ownership.** Eight of the twenty WIG20 companies are state-controlled, and
most of the banking sector is a listed subsidiary of a foreign group. Both are
read from GPW's shareholder register and tagged (`state`, `ctrl`), not dropped.

**Landlords vs developers.** Landlords (IAS 40 revaluations in their P/E) are
excluded; developers, whose flats are inventory, are kept.

**Foreign secondary lines** (Banco Santander, UniCredit, ČEZ, MOL, Krka) are
excluded; foreign-domiciled companies in a GPW size index (Allegro, Pepco,
Żabka) stay. **Listed investment vehicles** are excluded by GPW's own sector.

**A thin market.** About 36 companies clear size and liquidity, so only the
finance and trade & services groups have five peers; the rest are left
unscored on the peer screen rather than compared against noise.

Research tool, not investment advice.
