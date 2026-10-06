"""Poland-specific filters layered on top of the generic screener.

Everything from add_valueup_flags down is Germany's, which is the UK's, which
is Korea's: the quality floor, the absolute screen and the own-history screen
are not country-specific. Three things are local:

  * the top - which listed lines are structurally cheap on GPW
  * repair_foreign_reporters - Yahoo's zloty-over-euro multiples
  * the ownership flags - state control and a controlling parent
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config_pl as K

log = logging.getLogger(__name__)


def _fin_mask(df: pd.DataFrame) -> pd.Series:
    """Banks, insurers, brokers, lessors and debt collectors, by GPW sector.

    Korea had to match the substring "Financial" in a vendor sector string.
    Here GPW's sector code states it, as in Japan and Germany - and real
    estate, which GPW files under its Finance macro-sector, is not one.
    """
    sec = df.get("sector", pd.Series("", index=df.index)).fillna("")
    ind = df.get("industry", pd.Series("", index=df.index)).fillna("")
    return pd.Series([K.is_financial(a, b) for a, b in zip(sec, ind)], index=df.index)


def tag_lines(df: pd.DataFrame) -> pd.DataFrame:
    """Tag every line whose cheapness may be structural rather than a mispricing.

    No share-class step here, unlike Germany: GPW lists one line per company.
    Polish dual-class companies (LPP is the large one) keep their founders'
    voting-preference shares unlisted, so the trap Korea and Germany handle -
    two prices for one set of earnings - does not arise on the exchange.
    """
    df = df.copy()
    name = df["name"].fillna("").astype(str)
    sym = df.get("symbol", df["ticker"].str.replace(r"\.WA$", "", regex=True)) \
            .fillna("").astype(str).str.upper()
    ind = df.get("industry", pd.Series("", index=df.index)).fillna("").astype(str)
    sub = df.get("subsector", pd.Series("", index=df.index)).fillna("").astype(str)
    yind = df.get("yf_industry", pd.Series("", index=df.index)).fillna("").astype(str)
    qtype = df.get("quote_type", pd.Series("", index=df.index)).fillna("").astype(str)
    country = df.get("country", pd.Series("PL", index=df.index)).fillna("").astype(str)
    tier = df.get("tier", pd.Series("", index=df.index)).fillna("").astype(str)

    df["is_investment_co"] = ind.map(K.is_investment_company)
    df["is_reit"] = name.map(K.is_reit) | yind.str.contains("REIT", case=False, na=False)
    df["is_property"] = pd.Series([K.is_landlord(a, b) for a, b in zip(ind, sub)],
                                  index=df.index) & ~df["is_reit"]
    df["is_holdco"] = name.map(K.is_holdco)
    # A foreign issuer outside WIG20, mWIG40 and sWIG80 is a secondary line:
    # Banco Santander, UniCredit, CEZ, MOL, Krka. Its price is set at home and
    # the Warsaw quote follows. Index membership is GPW's own judgement that a
    # foreign-domiciled company's market IS here (Allegro, Pepco, Zabka,
    # AmRest), so those stay. It also drops Warsaw-primary foreigners that
    # fell out of the indices - Kernel, ~95% owned by Namsen since 2023 - and
    # that is correct for a different reason: no free float, no market price.
    df["is_foreign_secondary"] = country.ne("PL") & country.ne("") & tier.eq("")
    df["is_fund_quote"] = qtype.str.upper().isin(["ETF", "MUTUALFUND", "FUND"])
    return df


def apply_polish_filters(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Remove lines whose discount is structural, not a mispricing.

    Listed investment vehicles are the Polish analogue of the UK's investment
    trusts and Germany's PE holdings, and GPW names the class (sector code
    180, "Działalność inwestycyjna"). REITs are excluded for the reason Korea
    excluded them; landlords for Germany's IAS 40 reason, but developers are
    kept (config_pl.LANDLORD_SUBSECTORS). Foreign secondary lines and fund
    quotes have no business in a Polish screen at all.

    Ownership is NOT read here - it costs a request per company, so it runs
    on the size survivors only. See apply_ownership.
    """
    df = tag_lines(df)
    stats = {"listings": len(df)}

    def drop(flag: str, key: str, on: bool):
        nonlocal df
        if not on:
            return
        n = int(df[flag].sum())
        df = df[~df[flag]]
        stats[key] = n

    drop("is_investment_co", "dropped_investment_co", cfg.exclude_investment_companies)
    drop("is_reit", "dropped_reit", cfg.exclude_reits)
    drop("is_property", "dropped_property", cfg.exclude_property)
    drop("is_foreign_secondary", "dropped_foreign_secondary", True)
    drop("is_fund_quote", "dropped_fund_quote", True)
    drop("is_holdco", "dropped_holdco", cfg.exclude_holdcos)

    if not cfg.exclude_holdcos:
        stats["flagged_holdco"] = int(df["is_holdco"].sum())
    stats["after_polish_filters"] = len(df)
    return df.copy(), stats


def _holders(sheet: dict) -> list[dict]:
    out = []
    for h in (sheet or {}).get("shareholdersTab") or []:
        v = h.get("pct_votes")
        v = h.get("pct_shares") if v is None or not np.isfinite(v) else v
        if v is not None and np.isfinite(v):
            # GPW footnotes some holders with " *" (holding via subsidiaries).
            out.append({"name": str(h.get("name", "")).rstrip(" *"), "votes": float(v)})
    return out


def holder_parents(sheets: dict, roster: pd.DataFrame) -> list[str]:
    """ISINs of listed companies that appear as a holder in `sheets` - the
    second hop of a state chain (Pekao <- PZU <- Treasury) needs the middle
    company's own register."""
    stems = {K.name_stem(n): i for n, i in zip(roster["name"], roster["isin"])}
    out = []
    for sheet in sheets.values():
        for h in _holders(sheet):
            i = stems.get(K.name_stem(h["name"]))
            if i and i not in sheets:
                out.append(i)
    return list(dict.fromkeys(out))


def classify_ownership(sheets: dict, names: dict) -> dict:
    """{isin: {state_pct, state_via, ctrl_holder, ctrl_pct}} from GPW registers.

    State: holders that are the state (config_pl.is_state_holder) plus holders
    that are themselves state-controlled listed companies, iterated to a fixed
    point - Pekao is state because PZU (20%) is, and PZU because the Treasury
    holds 34%; PFR's 13% in Pekao counts directly. Portfolio holders (pension
    and investment funds) never count, so "PTE PZU" is not PZU.

    Control: one holder, or a declared concert party ("Porozumienie"), with a
    majority of votes, other than the state.
    """
    stems = {i: K.name_stem(n) for i, n in names.items()}
    state: dict[str, tuple[float, list]] = {}
    for _ in range(5):
        state_stems = {stems[i]: i for i in state if stems.get(i)}
        changed = False
        for isin, sheet in sheets.items():
            if isin in state:
                continue
            pct, via = 0.0, []
            for h in _holders(sheet):
                if K.is_portfolio_holder(h["name"]):
                    continue
                linked = state_stems.get(K.name_stem(h["name"]))
                if K.is_state_holder(h["name"]) or (linked and linked != isin):
                    pct += h["votes"]
                    via.append(f"{h['name']} {h['votes']:.1f}%")
            if pct >= K.STATE_MIN_VOTES:
                state[isin] = (pct, via)
                changed = True
        if not changed:
            break

    out = {}
    for isin, sheet in sheets.items():
        hs = [h for h in _holders(sheet) if not K.is_portfolio_holder(h["name"])]
        top = max(hs, key=lambda h: h["votes"]) if hs else None
        st = state.get(isin)
        ctrl = top is not None and top["votes"] >= K.CONTROL_MIN_VOTES and st is None
        out[isin] = {
            "state_pct": st[0] if st else np.nan,
            "state_via": "; ".join(st[1]) if st else "",
            "ctrl_holder": top["name"] if ctrl else "",
            "ctrl_pct": top["votes"] if ctrl else np.nan,
            "has_register": bool(sheet) and "shareholdersTab" in sheet,
        }
    return out


def apply_ownership(df: pd.DataFrame, own: dict, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Attach the ownership flags; drop state-controlled names only if asked.

    Flags, not gates, by default: the state controls eight of the twenty
    WIG20 companies and a majority holder most of the banking sector, so
    dropping either would leave little of the market - and a cheap SOE may be
    exactly what a reader is looking for, priced for its politics.
    """
    df = df.copy()

    def get(i, k, d):
        return own.get(i, {}).get(k, d)

    isin = df["isin"].astype(str)
    df["state_pct"] = [get(i, "state_pct", np.nan) for i in isin]
    df["state_holder"] = [get(i, "state_via", "") for i in isin]
    df["is_state"] = df["state_holder"].ne("")
    df["ctrl_holder"] = [get(i, "ctrl_holder", "") for i in isin]
    df["ctrl_pct"] = [get(i, "ctrl_pct", np.nan) for i in isin]
    df["is_controlled"] = df["ctrl_holder"].ne("")
    has = pd.Series([bool(get(i, "has_register", False)) for i in isin], index=df.index)
    stats = {"ownership_read": int(has.sum()),
             "ownership_unknown": int((~has).sum()),
             "flagged_state": int(df["is_state"].sum()),
             "flagged_majority_holder": int(df["is_controlled"].sum())}
    if cfg.exclude_state:
        stats["dropped_state"] = int(df["is_state"].sum())
        df = df[~df["is_state"]]
    return df, stats


# A reporting currency whose rate to the zloty is within this factor of 1
# cannot be told apart from the zloty by magnitude, so neither basis can be
# tested. Every currency on GPW today is far outside it (EUR 4.3, USD 3.6,
# CZK 0.17, HUF 0.011); RON at ~0.85 would not be.
FX_TEST_BAND = 1.6


def _nearer(k: pd.Series, r: pd.Series) -> pd.Series:
    """'quote' where k is nearer r than 1 in log terms, 'reporting' where
    nearer 1, '' where it is within FX_TEST_BAND of neither or untestable."""
    lk = np.log(k.where(k > 0))
    d_rep, d_quo = lk.abs(), (lk - np.log(r)).abs()
    tol = np.log(FX_TEST_BAND)
    out = np.where(d_quo < np.minimum(d_rep, tol), "quote",
                   np.where(d_rep < np.minimum(d_quo, tol), "reporting", ""))
    return pd.Series(out, index=k.index)


def repair_foreign_reporters(df: pd.DataFrame, pln_per: dict) -> tuple[pd.DataFrame, dict]:
    """Yahoo's multiples on zloty lines of companies that report in another
    currency - Poland's version of the UK's pence-and-pounds trap, and bigger.

    Measured 2026-10-05 on thirteen foreign reporters (EUR, USD, HUF, CZK, UAH, AUD):
    Yahoo's marketCap and enterpriseValue are in zloty, and so is trailingEps
    (EPS x shares / netIncomeToCommon = 3.65 for Asbis = the USD rate). But
    bookValue, ebitda and netIncomeToCommon are in the REPORTING currency. So

      priceToBook        zloty price / euro book      off by the rate (Pepco 119.5)
      enterpriseToEbitda zloty EV / dollar EBITDA     off by the rate (Asbis 55 -> 14)
      EPS / BPS (ROE)    zloty / euro                 off by the rate
      trailingPE         zloty / zloty                right
      dividendYield      zloty / zloty                right

    Every one of those is tested per row, never assumed - Japan's lesson that
    Yahoo restates fields on its own schedule. With r = PLN per reporting unit:

      EPS  k = EPS x shares / net income            ~r: zloty (keep)    ~1: reporting (x r)
      BPS  k = BPS x filed shares / filed equity    ~1: reporting (x r) ~r: zloty (keep)
           (no statements: (EPS / BPS) / Yahoo's unit-free ROE, ~r: reporting)
      EV   EV / market cap in [0.25, 4]             zloty EV; EBITDA is filed, so / r

    The book test reads the filing, not EPS, so a bad EPS cannot contaminate
    it. A test that lands near neither, or cannot run, refuses the multiple it
    vouches for (NaN) - a 4x error is not a rounding question. Run after the
    statements are merged, so the filed figures are there to test against.

    Checked against GPW's own factsheet P/BV on 2026-10-05: Asbis repaired to
    6.94 (GPW 6.92), Pepco to 27.3 (GPW 28.6). GPW puts Pepco's P/E at 30.9
    against Yahoo's 11.9 - the EPS test refusing Yahoo's was right.
    """
    df = df.copy()
    def n(c: str) -> pd.Series:
        if c not in df.columns:
            return pd.Series(np.nan, index=df.index)
        return pd.to_numeric(df[c], errors="coerce")
    fin = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    quote = df.get("currency", pd.Series("PLN", index=df.index)).fillna("PLN").astype(str)
    foreign = fin.ne("") & fin.ne(quote) & quote.eq("PLN")
    r = fin.map(lambda c: pln_per.get(c, np.nan)).astype(float)
    df["fx_rep_to_pln"] = r.where(foreign, 1.0)
    df["fx_note"] = ""
    stats = {"foreign_reporters": int(foreign.sum())}
    if not foreign.any():
        return df, stats

    untestable = foreign & ~(np.abs(np.log(r)) > np.log(FX_TEST_BAND))
    live = foreign & ~untestable

    eps, bps, sh = n("trailing_eps"), n("book_value_ps"), n("yf_shares")
    ni, roe = n("yf_ni"), n("yf_roe")
    ev, mcap, ebitda = n("yf_ev"), n("market_cap_local"), n("yf_ebitda")

    # EPS. Zloty on every line measured; tested anyway.
    eps_basis = _nearer((eps * sh / ni).where(ni.abs() > 0), r)
    eps_rep = live & eps_basis.eq("reporting")
    eps_bad = live & eps_basis.eq("") & eps.notna()
    eps_pln = eps.where(~eps_rep, eps * r)

    # Book value. Against the latest filing where there is one: BPS x filed
    # shares lands on filed equity (k ~1) only if BPS is in the reporting
    # currency. Annual vs latest-quarter drift is well inside the band
    # (Huuuge 1.36 on buybacks, against a rate of 3.5). Without statements,
    # Yahoo's unit-free ROE is read back out of EPS / BPS instead: matched
    # only after converting BPS means BPS was filed (k ~r).
    # Filed shares on TODAY's basis: restate_info_for_splits has already put
    # book value there, so the filed count must follow it.
    eq = n("lf_equity")
    lsh = n("lf_shares") * n("share_basis_g").fillna(1.0)
    by_filing = _nearer((bps * lsh / eq).where((eq > 0) & (lsh > 0)), r)
    by_roe = _nearer(((eps_pln / bps) / roe).where((roe != 0) & (bps > 0)), r)         .map({"quote": "reporting", "reporting": "quote", "": ""})
    has_filing = (eq > 0) & (lsh > 0) & bps.notna()
    bps_basis = by_filing.where(has_filing, by_roe)
    bps_rep = live & bps_basis.eq("reporting")
    bps_bad = live & bps_basis.eq("") & bps.notna()

    ev_pln = live & (ev / mcap).between(0.25, 4.0)

    df["trailing_eps"] = eps_pln.where(~eps_bad)
    df["trailing_pe"] = n("trailing_pe").where(~eps_rep, n("trailing_pe") / r).where(~eps_bad)
    df["book_value_ps"] = bps.where(~bps_rep, bps * r).where(~bps_bad)
    df["price_to_book"] = n("price_to_book").where(~bps_rep, n("price_to_book") / r) \
                                            .where(~bps_bad)
    # EV/EBITDA recomputed from Yahoo's own parts rather than divided, so a
    # missing enterpriseToEbitda with both parts present still gets a value.
    evx = (ev / (ebitda * r)).where(ebitda > 0)
    df["ev_to_ebitda"] = n("ev_to_ebitda").where(~foreign, evx.where(ev_pln))
    for c in ("trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps", "book_value_ps"):
        df.loc[untestable, c] = np.nan

    notes = []
    for i in df.index[foreign]:
        if untestable[i]:
            notes.append((i, "fx basis untestable"))
            continue
        bits = []
        if eps_bad[i]:
            bits.append("P/E refused")
        if bps_rep[i]:
            bits.append("P/B repaired")
        elif bps_bad[i]:
            bits.append("P/B refused")
        bits.append("EV/EBITDA repaired" if ev_pln[i] and pd.notna(evx[i])
                    else "EV/EBITDA refused")
        notes.append((i, ", ".join(bits)))
    for i, t in notes:
        df.at[i, "fx_note"] = t

    stats.update({
        "fx_untestable": int(untestable.sum()),
        "fx_eps_on_reporting_basis": int(eps_rep.sum()),
        "fx_pe_refused": int(eps_bad.sum()),
        "fx_pb_repaired": int(bps_rep.sum()),
        "fx_pb_refused": int(bps_bad.sum()),
        "fx_ev_repaired": int((ev_pln & evx.notna()).sum()),
    })
    log.info("currency repair: %d foreign reporters - %s", int(foreign.sum()),
             ", ".join(f"{df.at[i, 'symbol'] if 'symbol' in df else i}: {t}" for i, t in notes))
    return df, stats

def add_valueup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Low-P/B flags.

    Korea had a live policy catalyst attached to almost exactly this screen -
    KRX publicly identifies firms whose PBR sits in the bottom 20% of their
    industry. Poland has no counterpart: no disclosure regime is keyed to
    valuation. What closes Polish discounts is usually a tender offer (the
    takeover code's 50% / 66% call thresholds) or, for the state-controlled
    third of the WIG20, a change in policy - neither computable from this
    data.

    So these two columns are kept for continuity and for sorting, not as a
    catalyst.
    """
    df = df.copy()
    rank = df.get("price_to_book_pct_rank")
    df["pbr_bottom20_industry"] = (rank <= 0.20) if rank is not None else False
    df["pbr_below_1"] = df["price_to_book"] < 1.0
    return df


def add_quality_context(df: pd.DataFrame) -> pd.DataFrame:
    """ROE derived from EPS and BPS rather than taken as a vendor field.

    Same reasoning as Korea: the ratio is then consistent with the very P/E
    and P/B being screened on. It is unit-free only once EPS and BPS are in
    the same currency - Yahoo hands out zloty EPS against euro BPS for foreign
    reporters, so this must run after repair_foreign_reporters.
    """
    df = df.copy()
    eps = pd.to_numeric(df.get("trailing_eps"), errors="coerce")
    bps = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    df["roe_pct"] = np.where((bps > 0) & eps.notna(), eps / bps * 100.0, np.nan)
    df["div_yield"] = pd.to_numeric(df.get("div_yield"), errors="coerce")
    df["pays_dividend"] = df["div_yield"].fillna(0) > 0
    return df


def flag_ttm_vs_filed(df: pd.DataFrame) -> pd.DataFrame:
    """Profitable over the trailing twelve months, loss-making in the last
    filed year. A flag, never a gate.

    The peer and absolute screens use Yahoo's trailing multiples in every
    market, and Yahoo's trailing twelve months is the sum of the last four
    quarters - one-offs included. Germany's K+S is the case that found this:
    two quarters of very large non-operating gains turned a filed loss of
    EUR 1.1bn into TTM net income of +EUR 1.06bn and a P/E of 2.6.

    It is not a sign error - the quarters genuinely sum that way - so the
    trailing figure is left alone and the row is marked instead. The test is
    deliberately narrow: TTM profit against a normalized filed LOSS. A ratio
    test (TTM P/E far below filed P/E) would also catch genuine turnarounds,
    which is noise, not a warning.
    """
    df = df.copy()
    pe = pd.to_numeric(df.get("trailing_pe"), errors="coerce")
    lf = pd.to_numeric(df.get("lf_ni"), errors="coerce")
    df["ttm_vs_filed_loss"] = pe.notna() & (lf < 0)
    return df


def apply_roe_gate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Require survivors to actually earn something.

    Runs after scoring, never before: it narrows `passes` and leaves
    avg_discount alone, so the peer cohorts still contain the low-ROE names
    that make them representative. Missing ROE fails.
    """
    df = df.copy()
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)
    df["roe_tier"] = np.select(
        [roe >= 15.0, roe >= cfg.roe_good_pct, roe >= cfg.min_roe_pct],
        ["strong", "good", "marginal"], default="fail")

    before = int(df["passes"].sum())
    df["passes"] = df["passes"] & df["roe_ok"]
    after = int(df["passes"].sum())
    stats = {f"dropped_roe_below_{cfg.min_roe_pct:g}": before - after,
             "passing_after_roe": after}
    df = df.sort_values(["passes", "avg_discount"], ascending=[False, False])
    return df, stats


def apply_absolute_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Absolute cheapness, independent of the peer comparison.

    Identical in structure to the Korea version, including the financials
    carve-out: EV/EBITDA is suppressed for banks and insurers, so a strict
    both-metrics rule would exclude every Polish bank and insurer however far
    below book it traded.
    """
    df = df.copy()
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    ev = pd.to_numeric(df.get("ev_to_ebitda"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    dy = pd.to_numeric(df.get("div_yield"), errors="coerce")
    fin = _fin_mask(df)

    df["abs_pbr_ok"] = pbr.notna() & (pbr < cfg.abs_max_pbr)
    df["abs_ev_ok"] = ev.notna() & (ev < cfg.abs_max_ev_ebitda)
    df["abs_ev_missing"] = ev.isna()
    df["abs_roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)

    fair_pbr = roe / cfg.abs_cost_of_equity_pct
    df["abs_fair_pbr"] = fair_pbr
    df["abs_pbr_vs_roe_ok"] = pbr.notna() & fair_pbr.notna() & (pbr < fair_pbr)
    df["abs_div_ok"] = dy.notna() & (dy >= cfg.abs_min_div_yield)

    core = df["abs_pbr_ok"] & df["abs_ev_ok"]
    if cfg.abs_financials_pbr_only:
        core = core | (fin & df["abs_pbr_ok"])
        df["abs_via_carveout"] = fin & df["abs_pbr_ok"] & ~df["abs_ev_ok"]
    else:
        df["abs_via_carveout"] = False

    if cfg.abs_require_roe:
        core = core & df["abs_roe_ok"]
    if cfg.abs_require_pbr_vs_roe:
        core = core & df["abs_pbr_vs_roe_ok"]
    if cfg.abs_min_div_yield > 0:
        core = core & df["abs_div_ok"]
    df["abs_passes"] = core

    rel = df["passes"].astype(bool)
    absp = df["abs_passes"].astype(bool)
    df["screen"] = np.select([rel & absp, rel & ~absp, ~rel & absp],
                             ["both", "relative", "absolute"], default="")
    df["passes_any"] = rel | absp

    stats = {
        f"abs_pbr_under_{cfg.abs_max_pbr:g}": int(df["abs_pbr_ok"].sum()),
        f"abs_ev_under_{cfg.abs_max_ev_ebitda:g}": int(df["abs_ev_ok"].sum()),
        "abs_pbr_below_fair": int(df["abs_pbr_vs_roe_ok"].sum()),
        f"abs_div_over_{cfg.abs_min_div_yield:g}pct": int(df["abs_div_ok"].sum()),
        "abs_passing": int(absp.sum()),
        "abs_via_financial_carveout": int((absp & df["abs_via_carveout"]).sum()),
        "abs_new_vs_relative": int((absp & ~rel).sum()),
        "passing_either_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def restate_info_for_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Yahoo's .info per-share fields on today's share basis, after a split.

    The peer and absolute screens read P/E, P/B, EPS, book value and yield
    straight from .info. Yahoo restates those for splits on its own schedule,
    and not consistently: after Johnson Matthey's Aug 2026 4-for-3
    consolidation its bookValue was already on today's 125.9m shares (16.01 =
    FY2026 equity / 125.9m) while its filed statements were still on the old
    167.9m; in the Japan build bookValue was restated while dividendRate was
    not. So the basis is tested per row, never assumed either way.

    `share_basis_g` comes from build_statement_record: the split / consolidation
    factor since the latest filing that today's market cap demands (1 for
    almost everyone). Where it is not 1, filed equity over (book value x filed
    shares) says which basis .info is on: ~1 means the old one, ~g the new.

      old basis   EPS, book value and yield divided by g; P/E and P/B
                  multiplied by it. ROE, their ratio, does not move.
      new basis   per-share fields are right; the dividend cannot be checked
                  the same way (in Japan it lagged when book value did not),
                  so the yield is left missing rather than trusted.
      neither     P/E, P/B and yield missing - no basis to vouch for.

    EV/EBITDA needs nothing: Yahoo builds EV from today's market cap.
    """
    df = df.copy()
    g = pd.to_numeric(df.get("share_basis_g"), errors="coerce").fillna(1.0)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    sh = pd.to_numeric(df.get("lf_shares"), errors="coerce")
    bv = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    k = eq / (bv * sh)
    moved = np.abs(np.log(g)) > 0.02
    tol = np.log(1.25)
    old = moved & (np.abs(np.log(k)) < tol)
    new = moved & ~old & (np.abs(np.log(k / g)) < tol)
    unclear = moved & ~old & ~new

    for c, op in (("trailing_eps", "div"), ("book_value_ps", "div"), ("div_yield", "div"),
                  ("trailing_pe", "mul"), ("price_to_book", "mul")):
        if c in df.columns:
            v = pd.to_numeric(df[c], errors="coerce")
            df[c] = v.where(~old, v / g if op == "div" else v * g)
    if "div_yield" in df.columns:
        df.loc[new, "div_yield"] = np.nan
    for c in ("trailing_pe", "price_to_book", "div_yield"):
        if c in df.columns:
            df.loc[unclear, c] = np.nan

    df["split_note"] = np.select([old, new, unclear],
                                 ["info restated for split", "yield unverified after split",
                                  "info basis unclear"], default="")
    stats = {"info_restated_for_split": int(old.sum()),
             "info_yield_unverified": int(new.sum()),
             "info_basis_unclear": int(unclear.sum())}
    if moved.any():
        log.info("split basis: %d .info rows restated, %d yields unverified, %d unclear",
                 *stats.values())
    return df, stats


HIST_METRICS = (("per", "trailing_pe"), ("pbr", "price_to_book"),
                ("evx", "ev_to_ebitda"))


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, np.ndarray)) else []


def add_history_now(df: pd.DataFrame) -> pd.DataFrame:
    """Today's P/E, P/B and EV/EBITDA on the SAME basis as the history.

    Each historical year is that year-end market value over that year's filed
    totals (providers_pl.build_statement_record). Today's value has to be
    built the same way - today's market value over the latest filing - or the
    comparison measures the gap between two definitions rather than a change
    in valuation.

    That is not hypothetical. yfinance's own trailingPE divides by the last
    twelve months, including interims: the UK build measured Shell at 10.5
    against 15.2 on the filed-year basis, a 31% gap that would read as a 31%
    discount to history on its own. Korea learned the same lesson on
    EV/EBITDA, where mixing providers put only 18 of 30 within +/-25%; its fix
    was to rebuild the current value from the history's own source, which is
    what this does.

    So these three columns feed the own-history screen ONLY. The peer and
    absolute screens keep yfinance's figures, because they compare companies
    with each other on a common vendor definition, not a company with itself.

    Out-of-bounds values become NaN here rather than in the screen, so the
    dashboard receives exactly what Python tested and cannot disagree with it.
    """
    df = df.copy()
    n = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    mv = n("market_cap_local") * n("fx_now")      # quote-major -> reporting ccy
    ni, eq, eb = n("lf_ni"), n("lf_equity"), n("lf_ebitda")
    mi = n("lf_mi").fillna(0.0)
    df["per_now"] = (mv / ni).where(ni > 0)
    df["pbr_now"] = (mv / eq).where(eq > 0)
    df["evx_now"] = ((mv + n("lf_debt") - n("lf_cash") + mi) / eb).where(eb > 0)
    df.loc[_fin_mask(df), "evx_now"] = np.nan          # invariant 6
    for col, (_, bound_key) in zip(("per_now", "pbr_now", "evx_now"), HIST_METRICS):
        lo, hi = K.METRIC_BOUNDS[bound_key]
        df[col] = df[col].where((df[col] >= lo) & (df[col] <= hi))
    return df


def apply_history_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Cheap against the company's own filed history - the third screen.

    A port of the Korea build's apply_history_screen, held to the same rules:
    the benchmark is a median (invariant 4); a non-positive or out-of-bounds
    multiple is missing, never cheap, in history as well as today
    (invariant 2), so a loss year drops out of the benchmark rather than
    dragging it; EV/EBITDA is skipped for financials (invariant 6); fewer than
    `hist_min_years` usable years is no benchmark; the ROE floor applies.

    One difference, and it is a data limit rather than a choice: Yahoo carries
    FOUR filed years for Polish companies, as for UK and German ones, where
    WiseReport gave Korea five, so the benchmark is a four-year median.
    """
    df = add_history_now(df)
    fin = _fin_mask(df)

    disc_cols = []
    for key, bound_key in HIST_METRICS:
        lo, hi = K.METRIC_BOUNDS[bound_key]
        vals = df.get(f"hist_{key}", pd.Series([[]] * len(df), index=df.index)) \
                 .map(_as_list)
        for i in range(5):
            df[f"hist_{key}_y{i + 1}"] = vals.map(
                lambda v, i=i: v[i] if i < len(v) and v[i] is not None else np.nan)

        def bench(v):
            ok = [float(x) for x in v if x is not None and np.isfinite(float(x))
                  and lo <= float(x) <= hi]
            return float(np.median(ok)) if len(ok) >= cfg.hist_min_years else np.nan

        med = vals.map(bench)
        if key == "evx":
            med = med.where(~fin)
        cur = df[f"{key}_now"]
        df[f"hist_{key}_med"] = med
        df[f"hist_{key}_disc"] = (med - cur) / med
        disc_cols.append(f"hist_{key}_disc")

    discs = df[disc_cols]
    df["hist_n_valid"] = discs.notna().sum(axis=1)
    df["hist_n_pass"] = (discs >= cfg.hist_min_discount).sum(axis=1)
    # Averages every metric with data, including the failing ones - the same
    # rule as avg_discount (invariant 5).
    df["hist_avg_disc"] = discs.mean(axis=1, skipna=True)

    roe_ok = df.get("roe_ok", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    hp = df["hist_n_pass"] >= cfg.hist_min_metrics
    if cfg.hist_require_roe:
        hp = hp & roe_ok
    df["hist_passes"] = hp

    # Three screens now, so `screen` names every one a row cleared.
    rel = df["passes"].astype(bool)
    absp = df.get("abs_passes", pd.Series(False, index=df.index)).astype(bool)
    parts = pd.DataFrame({"relative": rel, "absolute": absp, "history": hp})
    df["screen"] = parts.apply(lambda r: " + ".join(k for k, v in r.items() if v), axis=1)
    df["passes_any"] = rel | absp | hp

    note = df.get("hist_note", pd.Series("", index=df.index)).fillna("")
    stats = {
        "hist_with_benchmark": int((df["hist_n_valid"] > 0).sum()),
        # Every name a guard refused a benchmark, by reason - so a data problem
        # shows up as a count in the funnel rather than as names quietly
        # missing from the third screen.
        "hist_share_break": int((note == "share-count break").sum()),
        "hist_basis_mismatch": int((note == "price/share basis mismatch").sum()),
        "hist_stale_filing": int((note == "stale filings").sum()),
        "hist_no_reporting_ccy": int((note == "no reporting currency").sum()),
        "hist_normalized_earnings": int((df.get("hist_earnings", pd.Series("", index=df.index))
                                         == "normalized").sum()),
        f"hist_passing_{cfg.hist_min_discount:.0%}": int(hp.sum()),
        "hist_new_vs_other_screens": int((hp & ~rel & ~absp).sum()),
        "passing_any_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def pl_output_columns(cfg: K.ScreenConfig) -> list[str]:
    cols = ["ticker", "symbol", "isin", "name", "name_exchange", "board", "tier", "sector", "industry",
            "subsector", "sector_code", "yf_industry", "country", "market_cap_usd", "mcap_source",
            "adv_usd", "close_local", "currency", "fx_rep_to_pln", "fx_note",
            "is_state", "state_pct", "state_holder", "is_controlled", "ctrl_holder",
            "ctrl_pct"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount",
                 f"{m}_peer_n", f"{m}_pct_rank"]
    # Own filed history: today's same-basis values, the median benchmark, the
    # discount to it, and each year's value for the tooltip.
    cols += ["hist_years", "hist_note", "hist_earnings", "share_basis_g", "split_note", "per_now", "pbr_now", "evx_now",
             "hist_n_valid", "hist_n_pass", "hist_avg_disc", "hist_passes"]
    for m in ("per", "pbr", "evx"):
        cols += [f"hist_{m}_med", f"hist_{m}_disc"]
        cols += [f"hist_{m}_y{i}" for i in range(1, 6)]
    # Three-year history: millions of the REPORTING currency, oldest first.
    cols += ["fin_years", "fin_n", "fin_ccy"]
    for m in ("rev", "op", "ebitda", "np"):
        cols += [f"{m}_y1", f"{m}_y2", f"{m}_y3", f"{m}_cagr"]
    cols += ["screen", "passes_any", "abs_passes", "abs_pbr_ok", "abs_ev_ok",
             "abs_pbr_vs_roe_ok", "abs_div_ok", "abs_roe_ok", "abs_fair_pbr",
             "abs_via_carveout", "ttm_vs_filed_loss",
             "roe_pct", "roe_ok", "roe_tier", "div_yield", "pays_dividend",
             "pbr_below_1", "pbr_bottom20_industry", "is_holdco",
             "n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
