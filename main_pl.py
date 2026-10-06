"""Poland (GPW, Warsaw Main Market) relative-valuation screener.

  python main_pl.py -v                       # full run
  python main_pl.py --skip-liquidity         # size alone gates (see README)
  python main_pl.py --min-adv 4e6            # the cross-market liquidity bar
  python main_pl.py --include-investment-cos # see why this is off by default
  python main_pl.py --discount 0.15 --min-metrics 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

import pandas as pd

import pl_filters as PF
from config_pl import ScreenConfig
from providers_pl import GpwProvider, _gpw_num
from screener import run_screen

D = ScreenConfig()      # argparse defaults come from here, so the two cannot drift


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="pl_screen_results.csv")
    p.add_argument("--min-mcap", type=float, default=D.min_market_cap_usd, help="USD")
    p.add_argument("--min-adv", type=float, default=D.min_adv_usd,
                   help="USD median daily traded value")
    p.add_argument("--discount", type=float, default=D.discount_threshold)
    p.add_argument("--min-metrics", type=int, default=D.min_metrics_passing)
    p.add_argument("--min-peers", type=int, default=D.min_peers)
    p.add_argument("--peer-keys", default=",".join(D.peer_keys))
    p.add_argument("--fallback-peer-keys", default=",".join(D.fallback_peer_keys))
    p.add_argument("--fx", type=float, help="USD per PLN (default: live)")
    p.add_argument("--include-investment-cos", action="store_true",
                   help="keep listed investment vehicles (GPW sector 180); they "
                        "trade at a standing discount to NAV")
    p.add_argument("--include-reits", action="store_true")
    p.add_argument("--include-property", action="store_true",
                   help="keep listed landlords (GTC, CPI Europe...); their P/E carries "
                        "IAS 40 revaluation gains. Developers are kept either way.")
    p.add_argument("--exclude-state", action="store_true",
                   help="drop state-controlled companies instead of tagging them")
    p.add_argument("--exclude-holdcos", action="store_true")
    p.add_argument("--adv-days", type=int, default=D.adv_lookback_days)
    p.add_argument("--skip-liquidity", action="store_true")
    p.add_argument("--min-roe", type=float, default=D.min_roe_pct,
                   help="ROE%% floor applied to survivors; 0 disables")
    p.add_argument("--abs-pbr", type=float, default=D.abs_max_pbr,
                   help="absolute screen: P/B below this")
    p.add_argument("--abs-ev", type=float, default=D.abs_max_ev_ebitda,
                   help="absolute screen: EV/EBITDA below this")
    p.add_argument("--abs-strict-financials", action="store_true",
                   help="require EV/EBITDA of financials too (they have none, so "
                        "none will pass)")
    p.add_argument("--no-abs-roe", action="store_true")
    p.add_argument("--no-abs-fair-pbr", action="store_true",
                   help="drop the P/B < ROE/CoE test")
    p.add_argument("--coe", type=float, default=D.abs_cost_of_equity_pct,
                   help="cost of equity %% for the fair-P/B test")
    p.add_argument("--abs-min-div", type=float, default=D.abs_min_div_yield,
                   help="absolute screen: dividend yield %% floor; 0 disables")
    p.add_argument("--no-financials", action="store_true",
                   help="skip the 3-year revenue/EBITDA/net-profit history")
    p.add_argument("--no-history", action="store_true",
                   help="skip the own-history screen")
    p.add_argument("--hist-discount", type=float, default=D.hist_min_discount)
    p.add_argument("--hist-min-metrics", type=int, default=D.hist_min_metrics)
    p.add_argument("--dashboard", default="pl_dashboard.html",
                   help="self-contained HTML dashboard; pass '' to skip")
    p.add_argument("--all", action="store_true",
                   help="write every scored row, not just passes")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


# Yahoo's long names end in the legal form, with the Polish letters Yahoo
# cannot spell ("Spólka Akcyjna"). The form says nothing a reader needs.
LEGAL_FORM = re.compile(r"[\s,]+(sp[oóÓ]?[lł]ka akcyjna|s\.?\s?a\.?|se|n\.?v\.?|plc|"
                        r"\(soci[eé]t[eé] anonyme\))\s*$", re.IGNORECASE)


def display_name(yahoo: str, gpw: str) -> str:
    """Yahoo's readable casing with GPW's Polish letters.

    GPW's names are correct but upper-case ("POWSZECHNY ZAKŁAD UBEZPIECZEŃ");
    title-casing them would wreck PKO, KGHM and LPP. Yahoo's are readable but
    lose the letters ("Powszechny Zaklad Ubezpieczen"). Each Yahoo word that
    matches a GPW word once accents are stripped takes GPW's letters in
    Yahoo's case.
    """
    from config_pl import plain
    n = str(yahoo or "").strip() or str(gpw or "").strip()
    for _ in range(2):
        n = LEGAL_FORM.sub("", n).strip()
    polish = {plain(w): w for w in str(gpw or "").split()}
    out = []
    for w in n.split(" "):
        g = polish.get(plain(w))
        if g and len(g) == len(w) and g != w.upper():
            w = "".join(gc.lower() if wc.islower() else gc for wc, gc in zip(w, g))
        out.append(w)
    return " ".join(out)


def main() -> int:
    a = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        # Polish names on a Windows console whose code page cannot print them
        # (cp949 here) would otherwise end the run at the results table.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    # yfinance logs every 404 at ERROR; the funnel counts them instead.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    for noisy in ("urllib3", "peewee", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("pl")

    keys = lambda s: tuple(k.strip() for k in s.split(",") if k.strip())  # noqa: E731
    cfg = ScreenConfig(
        min_market_cap_usd=a.min_mcap,
        min_adv_usd=0.0 if a.skip_liquidity else a.min_adv,
        discount_threshold=a.discount,
        min_metrics_passing=a.min_metrics,
        min_peers=a.min_peers,
        min_roe_pct=a.min_roe,
        abs_max_pbr=a.abs_pbr,
        abs_max_ev_ebitda=a.abs_ev,
        abs_require_roe=not a.no_abs_roe,
        abs_financials_pbr_only=not a.abs_strict_financials,
        abs_require_pbr_vs_roe=not a.no_abs_fair_pbr,
        abs_cost_of_equity_pct=a.coe,
        abs_min_div_yield=a.abs_min_div,
        hist_min_discount=a.hist_discount,
        hist_min_metrics=a.hist_min_metrics,
        peer_keys=keys(a.peer_keys),
        fallback_peer_keys=keys(a.fallback_peer_keys),
        exclude_investment_companies=not a.include_investment_cos,
        exclude_reits=not a.include_reits,
        exclude_property=not a.include_property,
        exclude_state=a.exclude_state,
        exclude_holdcos=a.exclude_holdcos,
        adv_lookback_days=a.adv_days,
    )

    prov = GpwProvider(cfg)
    days = prov.recent_business_days(cfg.adv_lookback_days)
    asof = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
    log.info("as of %s", asof)

    # 1. roster: GPW's own company search
    log.info("building roster...")
    roster = prov.listing_roster()
    if roster.empty:
        log.error("empty roster - GPW's company search did not answer or did not parse")
        return 1
    log.info("roster: %d listings (%s)", len(roster),
             ", ".join(f"{t or 'no index'}={n}" for t, n in
                       roster["tier"].value_counts().items()))

    # 2. fundamentals: one yfinance .info per line. Invariant 7 reads as it
    #    does in the UK and Germany: ~400 lines, and one .info returns market
    #    cap and every multiple together, so a separate pre-gate pass would
    #    double the requests to save nothing. Prices for liquidity and three
    #    statement calls per name run only past the size gate.
    log.info("fetching fundamentals for %d tickers...", len(roster))
    snap = prov.snapshot(roster["ticker"].tolist(), asof)

    # 2b. lines Yahoo does not know under GPW's symbol, resolved by ISIN and
    #     refetched - see GpwProvider.resolve_symbols (Erste Bank Polska).
    priced = set(snap.loc[snap["market_cap_local"].notna(), "ticker"])
    miss = roster[~roster["ticker"].isin(priced)]
    resolved = 0
    if not miss.empty:
        remap = prov.resolve_symbols(miss)
        for attempt in ("", "|alt"):
            m = {old: remap[old + attempt] for old in miss["ticker"]
                 if old + attempt in remap and old not in priced}
            if not m:
                continue
            s2 = prov.snapshot(list(m.values()), asof)
            ok = s2[s2["market_cap_local"].notna()]
            back = {v: k for k, v in m.items()}
            for new in ok["ticker"]:
                old = back[new]
                roster.loc[roster["ticker"] == old, "ticker"] = new
                priced.add(old)
                resolved += 1
            snap = pd.concat([snap[~snap["ticker"].isin(ok["ticker"])], ok],
                             ignore_index=True)
    log.info("  %d lines priced after ISIN resolution (%d resolved)",
             int(roster["ticker"].isin(set(snap.loc[snap["market_cap_local"].notna(),
                                                     "ticker"])).sum()), resolved)

    df = roster.merge(snap, on="ticker", how="left")

    # 2c. priced by Yahoo but with no share count, so no market cap - and
    #     fast_info cannot help, since it multiplies by the same missing
    #     count. Recent listings (Niewiadow, ROBYG) arrive this way. GPW's own
    #     factsheet states the market value; read as "too small" instead, a
    #     USD 670m company would fail the size gate silently.
    blank = df["market_cap_local"].isna() & pd.to_numeric(df["close_local"], errors="coerce").gt(0)
    gpw_mcap = 0
    if blank.any():
        sheets = prov.factsheets(df.loc[blank, "isin"].tolist(), tabs=("indicatorsTab",))
        for i in df.index[blank]:
            ind = (sheets.get(df.at[i, "isin"]) or {}).get("indicatorsTab") or {}
            mv = _gpw_num(ind.get("Market value (mln PLN)"))
            if mv > 0:
                df.at[i, "market_cap_local"] = mv * 1e6
                df.at[i, "mcap_source"] = "gpw"
                gpw_mcap += 1
        log.info("  %d of %d market caps Yahoo left blank repaired from GPW", gpw_mcap,
                 int(blank.sum()))
    n_priced = int(df["market_cap_local"].notna().sum())
    # Refuse rather than screen half the market. A healthy run prices nearly
    # every Main Market line; the misses are suspended and delisting names.
    if n_priced < 0.5 * len(df):
        log.error("only %d of %d lines priced. Yahoo is rate-limiting; the cache "
                  "has kept what arrived, so re-running in a few minutes will fill "
                  "the rest. Refusing to screen a partial universe.", n_priced, len(df))
        return 2
    ustats: dict = {"roster": len(roster), "priced": n_priced,
                    "priced_via_isin": resolved,
                    "mcap_via_fast_info": int((df["mcap_source"] == "fast_info").sum()),
                    "mcap_via_gpw": gpw_mcap}

    # 3. Polish universe hygiene
    df, gstats = PF.apply_polish_filters(df, cfg)
    ustats.update(gstats)
    log.info("after Polish universe hygiene: %d", len(df))

    usd_per_pln = a.fx if a.fx else prov.pln_to_usd()
    log.info("FX: 1 PLN = %.4f USD", usd_per_pln)

    # 4. size gate. Yahoo's market cap is in zloty on every .WA line, foreign
    #    reporters included, so the gate needs no currency repair.
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * usd_per_pln
    pre = df[df["market_cap_usd"] >= cfg.min_market_cap_usd].copy()
    ustats["cleared_market_cap"] = len(pre)
    log.info("%d of %d listings cleared USD %.0fm", len(pre), len(df),
             cfg.min_market_cap_usd / 1e6)
    if pre.empty:
        print("Nothing cleared the size gate.")
        return 0

    # 5. liquidity: one batched price download for the size survivors,
    #    fetched even with --skip-liquidity so the measured value reaches the page.
    log.info("fetching %d days of prices for %d names...", cfg.adv_lookback_days, len(pre))
    panel = prov.price_panel(pre["ticker"].tolist(), cfg.adv_lookback_days)
    adv = prov.average_daily_value(panel, pre["ticker"].tolist(), cfg.adv_lookback_days)
    pre = pre.merge(adv, on="ticker", how="left")
    pre["adv_usd"] = pd.to_numeric(pre["adv_local"], errors="coerce") * usd_per_pln

    if not a.skip_liquidity:
        pre = pre[pre["adv_usd"] >= cfg.min_adv_usd]
    ustats["cleared_size_liquidity"] = len(pre)
    log.info("%d cleared size/liquidity", len(pre))
    if pre.empty:
        print("Nothing cleared the size and liquidity gates.")
        return 0

    # 6. ownership, from GPW's register, survivors only - one request per
    #    company, plus the listed companies that hold them (the second hop of
    #    a state chain: Pekao <- PZU <- Treasury).
    sheets = prov.factsheets(pre["isin"].tolist(), tabs=("shareholdersTab",))
    for _ in range(2):
        more = PF.holder_parents(sheets, roster)
        if not more:
            break
        sheets.update(prov.factsheets(more, tabs=("shareholdersTab",)))
    own = PF.classify_ownership(sheets, dict(zip(roster["isin"], roster["name"])))
    pre, ostats = PF.apply_ownership(pre, own, cfg)
    ustats.update(ostats)

    # 7. filed statements, survivors only (invariant 7)
    if not (a.no_financials and a.no_history):
        from providers_pl import fetch_statements
        log.info("fetching filed statements for %d names...", len(pre))
        st = fetch_statements(pre, cfg, asof)
        if len(st.columns) > 1:
            pre = pre.merge(st, on="ticker", how="left")
            if a.no_financials:
                pre = pre.drop(columns=[c for c in pre.columns
                                        if c.startswith(("rev_", "op_", "ebitda_", "np_",
                                                         "fin_years", "fin_n"))])

    # 8. .info onto one basis before anything reads it.
    #    a) a split since the filing (DF.restate_info_for_splits) - first,
    #       because its test compares book value with FILED equity, so book
    #       value must still be in the filing's currency;
    #    b) the currency trap - zloty prices over euro and dollar accounts.
    if "share_basis_g" in pre.columns:
        pre, sstats = PF.restate_info_for_splits(pre)
        ustats.update(sstats)
    fins = pre.get("fin_ccy", pd.Series(dtype=object)).dropna().unique()
    pre, fxstats = PF.repair_foreign_reporters(pre, prov.pln_per(fins))
    ustats.update(fxstats)

    # 9. screen
    res, stats = run_screen(pre, usd_per_pln, cfg)
    if res.empty:
        print("Nothing survived screening.")
        return 0
    res = PF.add_quality_context(res)
    res = PF.add_valueup_flags(res)
    res = PF.flag_ttm_vs_filed(res)
    if cfg.min_roe_pct > 0:
        res, roestats = PF.apply_roe_gate(res, cfg)
        stats = {**stats, **roestats}
    else:
        res["roe_ok"], res["roe_tier"] = True, ""

    res, absstats = PF.apply_absolute_screen(res, cfg)
    stats = {**stats, **absstats}

    if not a.no_history and "hist_pbr" in res.columns:
        res, hstats = PF.apply_history_screen(res, cfg)
        stats = {**stats, **hstats}

    res["name_exchange"] = res["name"]
    yn = res.get("yf_name", pd.Series(index=res.index, dtype=object))
    res["name"] = [display_name(y if isinstance(y, str) else "", g)
                   for y, g in zip(yn, res["name_exchange"])]

    funnel = {**ustats, **stats}
    print("\n--- funnel ---")
    for k, v in funnel.items():
        print(f"  {k:<28} {v}")

    out = res if a.all else res[res["passes_any"]]
    cols = [c for c in PF.pl_output_columns(cfg) if c in res.columns]
    out[cols].to_csv(a.out, index=False, encoding="utf-8-sig")

    hits = res[res["passes_any"]]
    print(f"\n--- {len(hits)} stock(s) passing at least one screen ---")
    if not hits.empty:
        show = hits[["symbol", "name", "industry", "market_cap_usd",
                     "trailing_pe", "price_to_book", "roe_pct", "div_yield",
                     "avg_discount", "screen"]].head(30).copy()
        show["mcap_$m"] = (show.pop("market_cap_usd") / 1e6).round(0).astype("Int64")
        show["avg_discount"] = show["avg_discount"].map(
            lambda x: f"{x:.1%}" if pd.notna(x) else "")
        show["name"] = show["name"].str.slice(0, 28)
        print(show.to_string(index=False))

    meta = {
        "asof": asof, "source": "gpw+yfinance", "board": "MAIN",
        "roster_asof": prov.roster_asof,
        "cmd": "python main_pl.py " + " ".join(sys.argv[1:]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "usd_per_pln": round(usd_per_pln, 4),
        "funnel": funnel,
        "thresholds": {
            "min_mcap_usd": cfg.min_market_cap_usd,
            "min_adv_usd": 0 if a.skip_liquidity else cfg.min_adv_usd,
            "skip_liquidity": bool(a.skip_liquidity),
            "discount": cfg.discount_threshold,
            "min_metrics": cfg.min_metrics_passing,
            "min_peers": cfg.min_peers,
            "min_valid_metrics": cfg.min_valid_metrics,
            "min_roe_pct": cfg.min_roe_pct,
            "roe_good_pct": cfg.roe_good_pct,
            "abs_max_pbr": cfg.abs_max_pbr,
            "abs_max_ev_ebitda": cfg.abs_max_ev_ebitda,
            "abs_require_roe": cfg.abs_require_roe,
            "abs_financials_pbr_only": cfg.abs_financials_pbr_only,
            "abs_require_pbr_vs_roe": cfg.abs_require_pbr_vs_roe,
            "abs_cost_of_equity_pct": cfg.abs_cost_of_equity_pct,
            "abs_min_div_yield": cfg.abs_min_div_yield,
            "hist_min_discount": cfg.hist_min_discount,
            "hist_min_metrics": cfg.hist_min_metrics,
            "hist_min_years": cfg.hist_min_years,
        },
    }
    meta_path = os.path.splitext(a.out)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {a.out} and {meta_path}")

    if a.dashboard:
        try:
            from dashboard import build_dashboard
            build_dashboard(a.out, meta_path, a.dashboard)
            print(f"wrote {a.dashboard}   <- open this")
        except Exception as e:                            # noqa: BLE001
            log.error("dashboard build failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
