"""Pre-flight diagnostic: tests every live call the Poland screen makes.

    python check_setup.py

Run this before main_pl.py. Nearly every failure in this build is a SILENT
one - a renamed symbol reads as "too small", a throttled Yahoo as a market
where companies vanished, and a currency convention Yahoo changes as a 4x
error in every foreign reporter's P/B - and a full run is slow enough that
finding out afterwards is expensive. Each check here names what it protects
against.
"""
from __future__ import annotations

import logging
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        def run():
            t0 = time.time()
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
                print(f"  OK    {name:<44} {detail}  ({time.time() - t0:.1f}s)")
            except Exception as e:                                # noqa: BLE001
                RESULTS.append((name, False, str(e)))
                print(f"  FAIL  {name:<44} {e}")
        return run
    return deco


@check("GPW company search (roster + sectors)")
def roster():
    from config_pl import ScreenConfig
    from providers_pl import GpwProvider
    p = GpwProvider(ScreenConfig())
    r = p.listing_roster()
    if len(r) < 300:
        raise RuntimeError(f"only {len(r)} companies - the search form or rows changed?")
    for sym in ("PKO", "PKN", "KGH", "CDR", "ALE"):
        if sym not in set(r["symbol"]):
            raise RuntimeError(f"{sym} missing from the roster")
    unsect = int(r["sector_code"].eq("").sum())
    if unsect:
        raise RuntimeError(f"{unsect} companies matched no GPW sector code - "
                           "they would screen without peers")
    unknown = sorted(set(r.loc[~r["sector_code"].isin(__import__("config_pl").SECTORS),
                               "sector_code"]))
    tiers = r["tier"].value_counts().to_dict()
    if tiers.get("WIG20", 0) != 20:
        raise RuntimeError(f"{tiers.get('WIG20', 0)} WIG20 members, expected 20 - index "
                           "parsing broke, and the foreign-secondary rule reads it")
    return (f"{len(r)} companies, {r['sector_code'].nunique()} sectors"
            + (f", NEW codes {unknown} (add to config_pl.SECTORS)" if unknown else ""))


@check("Yahoo .info on a Warsaw line (rate limit)")
def info():
    import yfinance as yf
    i = yf.Ticker("PKO.WA").info
    if not i.get("marketCap") or not i.get("trailingPE"):
        raise RuntimeError("fields missing - Yahoo is throttling this IP; wait and retry")
    if i.get("currency") != "PLN":
        raise RuntimeError(f"currency {i.get('currency')!r}, expected PLN")
    return f"PKO BP mcap PLN {i['marketCap'] / 1e9:.0f}bn, P/E {i['trailingPE']:.1f}"


@check("currency convention on a euro reporter")
def fxconv():
    """The whole of repair_foreign_reporters rests on Yahoo's split: zloty
    for market cap, EV and EPS; euros for book value, EBITDA and net income.
    If Yahoo ever converts everything (or nothing), the repair's per-row
    tests refuse rather than mis-repair - but every foreign reporter then
    loses its P/B, and this says why before the run does."""
    import yfinance as yf
    from config_pl import ScreenConfig
    from providers_pl import GpwProvider
    i = yf.Ticker("PCO.WA").info
    if i.get("financialCurrency") != "EUR" or i.get("currency") != "PLN":
        raise RuntimeError(f"Pepco is {i.get('currency')}/{i.get('financialCurrency')}, "
                           "expected PLN quote / EUR accounts - pick another reporter")
    r = GpwProvider(ScreenConfig()).pln_per(["EUR"])["EUR"]
    ev_mc = i["enterpriseValue"] / i["marketCap"]
    k_eps = i["trailingEps"] * i["sharesOutstanding"] / i["netIncomeToCommon"]
    implied_pb = i["regularMarketPrice"] / i["bookValue"]
    if not 0.5 < ev_mc < 2.5:
        raise RuntimeError(f"EV / market cap {ev_mc:.2f} - EV is no longer in zloty")
    if abs(implied_pb / i["priceToBook"] - 1) > 0.05:
        raise RuntimeError("priceToBook is no longer price / bookValue")
    return (f"EUR = {r:.3f} PLN; EV/mcap {ev_mc:.2f} (zloty), EPS x shares / NI "
            f"{k_eps:.1f} (zloty EPS), P/B {i['priceToBook']:.1f} (euro book)")


@check("ISIN -> Yahoo symbol resolution")
def isin():
    import yfinance as yf
    q = yf.Search("PLBZ00000044", max_results=8, news_count=0).quotes or []
    syms = [x.get("symbol") for x in q]
    if not any(str(s).endswith(".WA") for s in syms):
        raise RuntimeError(f"Erste Bank Polska's ISIN resolved to {syms} - renamed lines "
                           "will drop out as unpriced")
    return f"Erste Bank Polska -> {syms}"


@check("batched price download (liquidity)")
def download():
    import yfinance as yf
    px = yf.download(["PKO.WA", "CDR.WA", "DOM.WA"], period="30d", interval="1d",
                     progress=False, auto_adjust=False, group_by="column")
    if px.empty or "Volume" not in px.columns.get_level_values(0):
        raise RuntimeError("no Close/Volume panel")
    n = int(px["Volume"].notna().sum().min())
    if n < 10:
        raise RuntimeError(f"only {n} sessions of volume")
    return f"{n} sessions for 3 tickers"


@check("filed statements")
def statements():
    import yfinance as yf
    tk = yf.Ticker("PKN.WA")
    inc, bs = tk.income_stmt, tk.balance_sheet
    need_i = ("Total Revenue", "Net Income Common Stockholders")
    need_b = ("Common Stock Equity", "Ordinary Shares Number")
    miss = [k for k in need_i if k not in inc.index] + [k for k in need_b if k not in bs.index]
    if miss:
        raise RuntimeError(f"rows missing {miss} - Yahoo renamed statement lines")
    return f"{inc.shape[1]} columns, normalized income: {'Normalized Income' in inc.index}"


@check("FX PLN->USD")
def fx():
    from config_pl import ScreenConfig
    from providers_pl import GpwProvider
    r = GpwProvider(ScreenConfig()).pln_to_usd()
    if r == 0.27:
        raise RuntimeError("both live sources failed - hardcoded fallback in use")
    return f"1 PLN = {r:.4f} USD"


def main() -> int:
    print("Poland screener pre-flight\n")
    for fn in (roster, info, fxconv, isin, download, statements, fx):
        fn()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + ("all checks passed - run python main_pl.py -v"
                  if not bad else f"{len(bad)} check(s) failed: {', '.join(bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
