"""Offline validation of the Poland screener. No network required.

Run: python test_poland.py

Every planted case is something a naive screener gets wrong. The currency
repair is the important one - it is Poland's counterpart of the UK's pence
trap, and a 4x error in either direction is planted, along with the cases
where the repair must refuse rather than guess.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import config_pl as K
import pl_filters as PF
from config_pl import ScreenConfig
from providers_pl import _gpw_num, parse_company_rows, to_yahoo
from screener import run_screen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

rng = np.random.default_rng(7)

USD_PER_PLN = 0.26
MCAP = 4.0e9           # ~USD 1.0bn, clears the gate
ADV = 6.0e6            # ~USD 1.6m, clears the gate

FAILURES: list[str] = []


def ok(cond, label):
    print(f"  {'OK  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILURES.append(label)


def base(**kw):
    r = dict(name="", symbol="", isin="", board="MAIN", tier="mWIG40",
             sector_code="420", subsector="industrial machinery", country="PL",
             quote_type="EQUITY", market_cap_local=MCAP, adv_local=ADV, close_local=50.0,
             currency="PLN", fin_ccy="PLN",
             trailing_pe=np.nan, price_to_book=np.nan, ev_to_ebitda=np.nan,
             trailing_eps=4.0, book_value_ps=32.0, div_yield=1.5, yf_industry="")
    r.update(kw)
    r["industry"], r["sector"] = K.sector_names(r["sector_code"])
    r["ticker"] = to_yahoo(r["symbol"])
    if not r["isin"]:
        r["isin"] = "PL" + (r["symbol"] + "XXXXXXX")[:7] + "00" + "1"
    return r


def make_universe() -> pd.DataFrame:
    rows = []
    # Two GPW sectors with different multiple levels; Banks gets its own.
    specs = [("420", 15.0, 1.8, 9.0, 12), ("820", 30.0, 4.5, 18.0, 10),
             ("110", 10.0, 1.4, np.nan, 8)]
    n = 0
    for code, pe, pb, ev, count in specs:
        for _ in range(count):
            n += 1
            rows.append(base(symbol=f"P{n:02d}", name=f"PEER {n} SPÓŁKA AKCYJNA",
                             sector_code=code,
                             trailing_pe=pe * rng.uniform(0.93, 1.09),
                             price_to_book=pb * rng.uniform(0.93, 1.09),
                             ev_to_ebitda=ev * rng.uniform(0.93, 1.09),
                             trailing_eps=pb / pe * 32.0 * rng.uniform(0.95, 1.05),
                             market_cap_local=MCAP * rng.uniform(0.9, 2.5),
                             adv_local=ADV * rng.uniform(0.9, 2.5)))

    # PASS: genuinely cheap vs machinery peers, good ROE, pays a dividend.
    rows.append(base(symbol="GOOD", name="GENUINE VALUE SPÓŁKA AKCYJNA",
                     trailing_pe=8.5, price_to_book=0.75, ev_to_ebitda=5.0,
                     trailing_eps=5.0, book_value_ps=40.0, div_yield=4.2))
    # FAIL: listed investment vehicle (GPW sector 180) - cheap on everything.
    rows.append(base(symbol="MCI", name="MCI CAPITAL ASI SPÓŁKA AKCYJNA", sector_code="180",
                     subsector="Investment", trailing_pe=4.0, price_to_book=0.6,
                     ev_to_ebitda=3.0, trailing_eps=4.0, book_value_ps=40.0, div_yield=3.0))
    # FAIL: landlord (GPW "real estate rent"). P/E of 3.5 from an IAS 40 gain.
    rows.append(base(symbol="GTC", name="GLOBE TRADE CENTRE SPÓŁKA AKCYJNA", sector_code="140",
                     subsector="real estate rent", trailing_pe=3.5, price_to_book=0.4,
                     ev_to_ebitda=18.0, trailing_eps=14.0, book_value_ps=110.0))
    # KEEP: developer (GPW "real estate sales") - an earnings multiple, not an
    # appraisal. Its own sector is thin, and it must NOT fall back to banks.
    rows.append(base(symbol="DOM", name="DOM DEVELOPMENT SPÓŁKA AKCYJNA", sector_code="140",
                     subsector="real estate sales", trailing_pe=8.9, price_to_book=2.9,
                     ev_to_ebitda=8.1, trailing_eps=26.0, book_value_ps=80.0, div_yield=6.3))
    # FAIL: foreign issuer outside the size indices - a secondary line.
    rows.append(base(symbol="SAN", isin="ES0113900J37", name="BANCO SANTANDER S.A.",
                     country="ES", tier="", sector_code="110", trailing_pe=7.0,
                     price_to_book=0.9, trailing_eps=6.0, book_value_ps=45.0, div_yield=5.0))
    # KEEP: foreign-domiciled, Warsaw-primary, in WIG20 (the Allegro shape).
    rows.append(base(symbol="ALE", isin="LU2237380790", name="ALLEGRO.EU S.A.", country="LU",
                     tier="WIG20", sector_code="820", trailing_pe=29.0, price_to_book=4.6,
                     ev_to_ebitda=17.0))
    # FAIL (ROE): cheap holdco with 2% ROE - arithmetic, not a discount.
    rows.append(base(symbol="HOLD", name="RODZINNA HOLDING SPÓŁKA AKCYJNA",
                     trailing_pe=9.0, price_to_book=0.55, ev_to_ebitda=5.5,
                     trailing_eps=0.8, book_value_ps=40.0, div_yield=2.5))
    # FAIL: loss-maker. Yahoo sometimes reports P/E 0; it must read as missing.
    rows.append(base(symbol="LOSS", name="STRATA SPÓŁKA AKCYJNA", trailing_pe=0.0,
                     price_to_book=0.6, trailing_eps=-2.0, book_value_ps=30.0))
    # A bank: EV/EBITDA suppressed even though Yahoo gives one, and it clears
    # the absolute screen via the P/B + ROE carve-out.
    rows.append(base(symbol="BANK", name="TANI BANK SPÓŁKA AKCYJNA", sector_code="110",
                     subsector="commercial banks", trailing_pe=6.0, price_to_book=0.8,
                     ev_to_ebitda=4.0, trailing_eps=4.0, book_value_ps=30.0, div_yield=7.0))
    # Thin sector: alone in Automotive. Falls back to the macro-sector
    # (Consumer Goods) - which here is also thin, so it stays unscored.
    rows.append(base(symbol="LONE", name="SAMOTNY SPÓŁKA AKCYJNA", sector_code="540",
                     trailing_pe=16.0, price_to_book=1.9, ev_to_ebitda=9.5))
    # Alone in Construction, but Industrials (machinery) is populated: falls back.
    rows.append(base(symbol="BUD", name="BUDOWA SPÓŁKA AKCYJNA", sector_code="410",
                     trailing_pe=16.0, price_to_book=1.9, ev_to_ebitda=9.5))
    # FAIL (size): too small.
    rows.append(base(symbol="TINY", name="MALA SPÓŁKA AKCYJNA", market_cap_local=5e8,
                     trailing_pe=6.0, price_to_book=0.5, ev_to_ebitda=3.0))
    return pd.DataFrame(rows)


def check_helpers():
    print("=== helpers ===")
    ok(to_yahoo("PKO") == "PKO.WA" and to_yahoo("11B") == "11B.WA", "GPW symbol -> .WA")
    ok(K.sector_names("110") == ("Banks", "Finance"), "sector code 110 -> Banks / Finance")
    ok(K.sector_names("140") == ("Real Estate", "Real Estate"),
       "real estate is its own macro bucket, not Finance")
    ok(K.sector_names("999", "Nowy sektor") == ("Nowy sektor", ""),
       "an unknown code keeps GPW's Polish label rather than vanishing")
    ok(K.is_financial("Finance", "Banks") and K.is_financial("Finance", "Debt Collection"),
       "banks and debt collectors are financials")
    ok(not K.is_financial("Finance", "Real Estate"), "real estate is NOT a financial")
    ok(K.is_landlord("Real Estate", "real estate rent")
       and not K.is_landlord("Real Estate", "real estate sales"),
       "landlords and developers told apart by GPW subsector")
    ok(K.size_tier("WIG, sWIG80, WIG20") == "WIG20" and K.size_tier("WIG, WIG-Poland") == "",
       "largest size index read off the index list; WIG alone is not one")
    ok(_gpw_num("2,624.21") == 2624.21 and np.isnan(_gpw_num("---")), "GPW number format")

    html = """<tr><td><a href="company-factsheet?isin=PLPKO0000016"><strong class="name">
      POWSZECHNA KASA OSZCZĘDNOŚCI BANK POLSKI SPÓŁKA AKCYJNA <span class="grey">(PKO)</span>
      </strong></a><small class="grey">Main Market |
      WIG20,  WIG30,
      WIG | commercial banks</small></td></tr>
      <tr><td><a href="company-factsheet?isin=LT0000128621"><strong>AB INTER RAO LIETUVA
      <span>(IRL)</span></strong></a><small>Main Market | power</small></td></tr>"""
    rows = parse_company_rows(html)
    ok(len(rows) == 2 and rows[0]["symbol"] == "PKO" and rows[0]["isin"] == "PLPKO0000016"
       and rows[0]["subsector"] == "commercial banks" and K.size_tier(rows[0]["indices"]) == "WIG20",
       "company-search row parsed: ISIN, symbol, indices, subsector")
    ok(rows[1]["indices"] == "" and rows[1]["subsector"] == "power",
       "a row with no index memberships still parses")

    ok(K.name_stem("POWSZECHNY ZAKŁAD UBEZPIECZEŃ SPÓŁKA AKCYJNA")
       == K.name_stem("Powszechny Zakład Ubezpieczeń S.A."),
       "a holder name matches the listed company's name across legal forms")
    ok(K.is_state_holder("Skarb Państwa") and K.is_state_holder("PKP S.A.")
       and K.is_state_holder("Polski Fundusz Rozwoju S.A."), "state holders recognised")
    ok(K.is_portfolio_holder("Powszechne Towarzystwo Emerytalne PZU S.A.")
       and K.is_portfolio_holder("Nationale-Nederlanden OFE")
       and not K.is_portfolio_holder("Commerzbank AG"),
       "pension and fund managers are portfolio holders; a parent bank is not")


def check_ownership():
    print("\n=== ownership from GPW's register ===")
    h = lambda n, v: {"name": n, "pct_shares": v, "pct_votes": v}  # noqa: E731
    names = {"PZU": "POWSZECHNY ZAKŁAD UBEZPIECZEŃ SPÓŁKA AKCYJNA",
             "PEO": "BANK POLSKA KASA OPIEKI SPÓŁKA AKCYJNA",
             "ING": "ING BANK ŚLĄSKI SPÓŁKA AKCYJNA",
             "KTY": "GRUPA KĘTY SPÓŁKA AKCYJNA",
             "MIX": "MIESZANY SPÓŁKA AKCYJNA"}
    sheets = {
        "PZU": {"shareholdersTab": [h("Skarb Państwa *", 34.19)]},
        # Pekao: PZU 20% (itself state) + PFR 12.8% -> state at 32.8%.
        "PEO": {"shareholdersTab": [h("Powszechny Zakład Ubezpieczeń Spółka Akcyjna", 20.0),
                                    h("Polski Fundusz Rozwoju S.A.", 12.8),
                                    h("Nationale-Nederlanden OFE", 6.0)]},
        "ING": {"shareholdersTab": [h("ING Bank N.V.", 75.0)]},
        # Pension funds only: neither state nor controlled.
        "KTY": {"shareholdersTab": [h("Nationale-Nederlanden OFE", 12.0),
                                    h("Allianz Polska OFE", 9.0)]},
        # PTE PZU's 30% is pension money, not PZU's stake; nothing else.
        "MIX": {"shareholdersTab": [h("Powszechne Towarzystwo Emerytalne PZU S.A.", 30.0)]},
    }
    own = PF.classify_ownership(sheets, names)
    ok(own["PZU"]["state_pct"] == 34.19, "Treasury 34% -> state")
    ok(abs(own["PEO"]["state_pct"] - 32.8) < 1e-9,
       "Pekao is state through PZU (itself state) plus PFR")
    ok(own["ING"]["ctrl_holder"] == "ING Bank N.V." and np.isnan(own["ING"]["state_pct"]),
       "foreign parent with 75% -> majority-controlled, not state")
    ok(own["KTY"]["ctrl_holder"] == "" and np.isnan(own["KTY"]["state_pct"]),
       "pension funds neither control nor make a company state")
    ok(np.isnan(own["MIX"]["state_pct"]), "PTE PZU is not PZU: no state chain through a pension fund")
    ok(own["PZU"]["ctrl_holder"] == "", "a state holder is reported as state, not as ctrl")


def check_currency():
    print("\n=== currency repair (zloty quote, foreign accounts) ===")
    EUR, RON = 4.38, 0.86
    rates = {"PLN": 1.0, "EUR": EUR, "RON": RON}
    shares = 100e6
    rows = []
    # EUR reporter, Yahoo's convention: EPS in zloty, BPS / EBITDA / NI in euros.
    # True P/B 2.0: price 87.6 PLN, book EUR 10/share = PLN 43.8.
    rows.append(dict(symbol="EURO", currency="PLN", fin_ccy="EUR", close_local=87.6,
                     market_cap_local=87.6 * shares, yf_shares=shares,
                     trailing_eps=8.76, trailing_pe=10.0, yf_ni=200e6, yf_roe=0.2,
                     book_value_ps=10.0, price_to_book=8.76,
                     yf_ev=87.6 * shares * 1.1, yf_ebitda=300e6, ev_to_ebitda=87.6 * 1.1 / 3,
                     lf_equity=1000e6, lf_shares=shares, share_basis_g=1.0))
    # Same company, but Yahoo's EPS implies twice the reported net income
    # (the Pepco shape): P/E refused, P/B still repaired from the filing.
    r = dict(rows[0]); r.update(symbol="PEPC", trailing_eps=17.52, trailing_pe=5.0)
    rows.append(r)
    # No statements: the book test falls back to Yahoo's unit-free ROE.
    r = dict(rows[0]); r.update(symbol="NOST", lf_equity=np.nan, lf_shares=np.nan)
    rows.append(r)
    # A currency too close to the zloty to tell the bases apart.
    r = dict(rows[0]); r.update(symbol="ROMA", fin_ccy="RON")
    rows.append(r)
    # A zloty reporter: untouched.
    rows.append(dict(symbol="PLNX", currency="PLN", fin_ccy="PLN", close_local=50.0,
                     market_cap_local=50.0 * shares, yf_shares=shares,
                     trailing_eps=5.0, trailing_pe=10.0, yf_ni=500e6, yf_roe=0.15,
                     book_value_ps=33.0, price_to_book=1.5, yf_ev=6e9, yf_ebitda=1e9,
                     ev_to_ebitda=6.0, lf_equity=3.3e9, lf_shares=shares, share_basis_g=1.0))
    df, stats = PF.repair_foreign_reporters(pd.DataFrame(rows), rates)
    i = df.set_index("symbol")
    ok(abs(i.loc["EURO", "price_to_book"] - 2.0) < 1e-9,
       f"euro book converted: P/B 8.76 -> {i.loc['EURO', 'price_to_book']:.2f}")
    ok(abs(i.loc["EURO", "ev_to_ebitda"] - 87.6 * 1.1 / 3 / EUR) < 1e-9,
       f"zloty EV over euro EBITDA converted ({i.loc['EURO', 'ev_to_ebitda']:.2f})")
    ok(i.loc["EURO", "trailing_pe"] == 10.0, "zloty EPS confirmed by test, P/E untouched")
    ok(abs(i.loc["EURO", "trailing_eps"] / i.loc["EURO", "book_value_ps"] - 0.2) < 1e-9,
       "ROE from EPS / BPS back on one currency (20%)")
    ok(np.isnan(i.loc["PEPC", "trailing_pe"]) and abs(i.loc["PEPC", "price_to_book"] - 2.0) < 1e-9,
       "EPS inconsistent with net income: P/E refused, P/B still repaired from the filing")
    ok(abs(i.loc["NOST", "price_to_book"] - 2.0) < 1e-9,
       "no statements: book basis read from Yahoo's ROE instead")
    ok(i.loc["ROMA", ["trailing_pe", "price_to_book", "ev_to_ebitda"]].isna().all()
       and i.loc["ROMA", "fx_note"] == "fx basis untestable",
       "a currency near 1 PLN cannot be tested -> refused, not guessed")
    ok(i.loc["PLNX", "price_to_book"] == 1.5 and i.loc["PLNX", "ev_to_ebitda"] == 6.0
       and i.loc["PLNX", "fx_note"] == "", "zloty reporter untouched")
    ok(stats["foreign_reporters"] == 4 and stats["fx_pe_refused"] == 1,
       f"funnel counts the repair ({stats})")


def check_statements():
    from providers_pl import build_statement_record, cagr
    print("\n=== filed statements ===")
    years = pd.to_datetime(["2021-12-31", "2022-12-31", "2023-12-31",
                            "2024-12-31", "2025-12-31"])

    def stmt(rows: dict) -> pd.DataFrame:
        df = pd.DataFrame({k: [np.nan] + list(v) for k, v in rows.items()}, index=years).T
        return df[df.columns[::-1]]

    def company(ni=(100e6, 110e6, 120e6, 130e6), shares=(1e8,) * 4):
        inc = stmt({"Total Revenue": (1000e6, 1100e6, 1200e6, 1300e6),
                    "Operating Income": (150e6, 160e6, 170e6, 180e6),
                    "EBITDA": (200e6, 210e6, 220e6, 230e6),
                    "Net Income Common Stockholders": ni})
        bs = stmt({"Common Stock Equity": (1000e6,) * 4, "Ordinary Shares Number": shares,
                   "Total Debt": (300e6,) * 4, "Cash And Cash Equivalents": (100e6,) * 4})
        return inc, bs

    px = pd.Series(20.0, index=pd.date_range("2020-01-01", "2026-10-02", freq="B"))
    inc, bs = company()
    rec = build_statement_record(inc, bs, px, None, "PLN", "PLN", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_years"] == "2022,2023,2024,2025", "Yahoo's empty padded year is not a filed year")
    ok(rec["hist_pbr"] == [2.0] * 4, f"P/B history in zloty ({rec['hist_pbr']})")
    ok(rec["fin_years"] == "2023,2024,2025" and rec["rev_y3"] == 1300.0,
       "3-year history is the last three filed years, in millions")
    ok(np.isnan(cagr([-50, 10, 20])), "CAGR undefined on a negative base")

    # A EURO reporter quoted in zloty - the Pepco shape. PLN 2bn at 0.228
    # EUR/PLN is EUR 456m over EUR 1bn of equity: P/B 0.456.
    fx = pd.Series(0.228, index=px.index)
    rec = build_statement_record(inc, bs, px, fx, "PLN", "EUR", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_pbr"] == [0.456] * 4, f"EUR reporter converted at each year-end ({rec['hist_pbr']})")
    rec = build_statement_record(inc, bs, px, None, "PLN", "EUR", close_now=20.0, mcap_now=2e9)
    ok(all(v is None for v in rec["hist_pbr"]), "EUR reporter without FX -> no benchmark")

    inc2, bs2 = company(shares=(1e7, 1e7, 1e8, 1e8))
    rec = build_statement_record(inc2, bs2, px, None, "PLN", "PLN", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_note"] == "share-count break" and rec["hist_pbr"] == [],
       "share split -> no history, not a 90% 'premium'")


def check_history_screen():
    print("\n=== own-history screen ===")
    cfg = ScreenConfig()

    def row(sym, pers, pbrs, evxs, now_mcap, sector="Industrials",
            industry="Electrical Engineering", roe_ok=True):
        return dict(symbol=sym, ticker=sym + ".WA", sector=sector, industry=industry,
                    hist_per=pers, hist_pbr=pbrs, hist_evx=evxs,
                    market_cap_local=now_mcap, fx_now=1.0, lf_ni=100e6,
                    lf_equity=1000e6, lf_ebitda=200e6, lf_debt=300e6,
                    lf_cash=100e6, lf_mi=0.0, passes=False, abs_passes=False,
                    roe_ok=roe_ok, avg_discount=0.0, hist_note="")

    df = pd.DataFrame([
        row("DRTD", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9),
        row("LOWR", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9, roe_ok=False),
        row("THIN", [20, None, None, 21], [2.0, None, None, 2.1], [10, None, None, 11], 1.0e9),
        row("BANK", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9,
            sector="Finance", industry="Banks"),
        row("DEVL", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9,
            sector="Real Estate", industry="Real Estate"),
    ])
    res, _ = PF.apply_history_screen(df, cfg)
    idx = res.set_index("symbol")
    d = idx.loc["DRTD"]
    ok(bool(d["hist_passes"]) and d["screen"] == "history", "de-rated name passes on history alone")
    ok(abs(d["per_now"] - 10.0) < 1e-9, "today measured on the history's basis")
    ok(not bool(idx.loc["LOWR", "hist_passes"]), "ROE floor applies to the history screen")
    ok(idx.loc["THIN", "hist_n_valid"] == 0, "fewer than 3 usable years -> no benchmark")
    ok(pd.isna(idx.loc["BANK", "hist_evx_med"]) and pd.isna(idx.loc["BANK", "evx_now"]),
       "banks: EV/EBITDA skipped in history and today")
    ok(pd.notna(idx.loc["DEVL", "evx_now"]), "real estate keeps EV/EBITDA (not a financial)")


def main() -> int:
    check_helpers()
    check_ownership()
    check_currency()
    cfg = ScreenConfig()
    df = make_universe()

    df, gstats = PF.apply_polish_filters(df, cfg)
    df["market_cap_usd"] = df["market_cap_local"] * USD_PER_PLN
    df = df[df["market_cap_usd"] >= cfg.min_market_cap_usd]
    res, stats = run_screen(df, USD_PER_PLN, cfg)
    res = PF.add_quality_context(res)
    res = PF.add_valueup_flags(res)
    res, _ = PF.apply_roe_gate(res, cfg)
    res, _ = PF.apply_absolute_screen(res, cfg)
    idx = res.set_index("symbol")

    print("\n=== universe hygiene ===")
    print(f"  funnel: {gstats}")
    for sym, why in [("MCI", "investment vehicle excluded by GPW sector"),
                     ("GTC", "landlord excluded (IAS 40 P/E)"),
                     ("SAN", "foreign secondary line excluded"),
                     ("TINY", "below the size floor")]:
        ok(sym not in idx.index, why)
    ok("DOM" in idx.index, "developer kept - flats are inventory, the P/E is earnings")
    ok("ALE" in idx.index, "foreign-domiciled WIG20 member kept")

    print("\n=== screens ===")
    ok(bool(idx.loc["GOOD", "passes"]), "genuinely cheap name passes the peer screen")
    ok(not bool(idx.loc["HOLD", "passes"]) and idx.loc["HOLD", "n_metrics_passing"] >= 2,
       "cheap holdco with 2% ROE fails on ROE, not on cheapness")
    ok(pd.isna(idx.loc["LOSS", "trailing_pe"]), "P/E of 0 read as missing")
    ok(pd.isna(idx.loc["BANK", "ev_to_ebitda"]), "bank's EV/EBITDA suppressed (GPW sector)")
    ok(bool(idx.loc["BANK", "abs_passes"]) and bool(idx.loc["BANK", "abs_via_carveout"]),
       "bank clears the absolute screen via the P/B + ROE carve-out")
    ok(idx.loc["BUD", "trailing_pe_peer_basis"] == "sector",
       f"thin sector falls back to the macro-sector "
       f"({idx.loc['BUD', 'trailing_pe_peer_basis']!r})")
    ok(idx.loc["GOOD", "trailing_pe_peer_basis"] == "industry",
       "a populated sector benchmarks within itself")
    ok(idx.loc["DOM", "trailing_pe_peer_basis"] == "" and pd.isna(idx.loc["DOM", "avg_discount"]),
       "a lone developer is left unscored, never benchmarked against banks")
    ok(idx.loc["LONE", "trailing_pe_peer_n"] == 0, "a name with no cohort at either level is unscored")

    check_statements()
    check_history_screen()

    print("\n=== passing ===")
    hits = res[res["passes_any"]][["symbol", "name", "industry", "trailing_pe",
                                   "price_to_book", "roe_pct", "screen"]]
    print(hits.round(2).to_string(index=False) if not hits.empty else "  (none)")
    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
