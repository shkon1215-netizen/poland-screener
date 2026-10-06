"""Screening engine (Poland build).

sanitize_metrics, compute_peer_benchmarks and score are the Korea build's
maths - the relative-valuation logic does not care which market it is pointed
at, which is the whole premise of porting it. The two differences are local
and both follow the Germany build:

  - financials are identified by GPW's own sector code rather than a vendor
    sector string, so invariant 6 is exact - and real estate, which GPW files
    under its Finance macro-sector, keeps its EV/EBITDA
  - the FX leg carries one scalar, USD per PLN

Do not "improve" the maths here. If a change is needed it is almost certainly
needed in all five markets, and it belongs in Korea's copy first.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from config_pl import METRIC_BOUNDS, METRIC_LABELS, ScreenConfig, is_financial

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 1. Clean metrics
# --------------------------------------------------------------------------
def sanitize_metrics(df: pd.DataFrame, cfg: ScreenConfig) -> pd.DataFrame:
    """Null out invalid multiples.

    This is the single most important step. A negative P/E means the company
    lost money - it is NOT a cheap stock, and letting it through would make
    every loss-maker look like a screaming buy. Same for negative book value
    and negative EBITDA.
    """
    df = df.copy()
    for m in cfg.metrics:
        lo, hi = METRIC_BOUNDS[m]
        v = pd.to_numeric(df.get(m), errors="coerce")
        df[m] = v.where((v > 0) & (v >= lo) & (v <= hi))

    # EV/EBITDA is meaningless for banks, insurers and other financials (no
    # meaningful enterprise value). GPW's sector code names them; Real Estate,
    # which GPW files under Finance, is not one - see config_pl.
    if "ev_to_ebitda" in df.columns and "sector" in df.columns:
        ind = df["industry"] if "industry" in df.columns else pd.Series("", index=df.index)
        fin = pd.Series([is_financial(a, b) for a, b in
                         zip(df["sector"].fillna(""), ind.fillna(""))], index=df.index)
        df.loc[fin, "ev_to_ebitda"] = np.nan
    return df


# --------------------------------------------------------------------------
# 2. Size & liquidity gates
# --------------------------------------------------------------------------
def apply_usd_conversion(df: pd.DataFrame, usd_per_pln: float) -> pd.DataFrame:
    """One scalar rate for the whole market.

    `usd_per_pln` is USD PER PLN (~0.27) and it is MULTIPLIED - Germany's
    direction. The name says which way round so a copied line cannot flip it.
    No subunit trap here: every .WA line quotes and caps in whole zloty, and
    Yahoo's market cap and enterprise value are in zloty even for companies
    that report in euros or dollars (measured - see pl_filters).
    """
    df = df.copy()
    df["fx_to_usd"] = usd_per_pln
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * usd_per_pln
    if "adv_local" in df.columns:
        df["adv_usd"] = pd.to_numeric(df["adv_local"], errors="coerce") * usd_per_pln
    return df


def apply_gates(df: pd.DataFrame, cfg: ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Size, liquidity and classification gates. Foreign secondary listings
    were already removed by pl_filters, so no country filter here."""
    stats: dict[str, int] = {"start": len(df)}
    d = df

    if cfg.exclude_sectors:
        d = d[~d["sector"].isin(cfg.exclude_sectors)]

    d = d[d["market_cap_usd"].notna() & (d["market_cap_usd"] >= cfg.min_market_cap_usd)]
    stats["after_market_cap"] = len(d)

    if "adv_usd" in d.columns and cfg.min_adv_usd > 0:
        d = d[d["adv_usd"].notna() & (d["adv_usd"] >= cfg.min_adv_usd)]
    stats["after_liquidity"] = len(d)

    d = d[d["industry"].fillna("").ne("")]
    stats["after_industry_known"] = len(d)
    return d.copy(), stats


# --------------------------------------------------------------------------
# 3. Peer benchmarks
# --------------------------------------------------------------------------
def _winsorized_median(s: pd.Series, pct: float) -> float:
    s = s.dropna()
    if s.empty:
        return np.nan
    if pct > 0 and len(s) >= 10:
        lo, hi = s.quantile(pct), s.quantile(1 - pct)
        s = s.clip(lo, hi)
    return float(s.median())


def compute_peer_benchmarks(df: pd.DataFrame, cfg: ScreenConfig) -> pd.DataFrame:
    """Attach peer median + peer count + percentile rank for each metric.

    Peer groups are built on cfg.peer_keys (here: GPW's sector code).
    If a group has fewer than min_peers valid observations for a metric, we
    fall back to cfg.fallback_peer_keys (here: GPW's macro-sector). If that is still too thin,
    the metric is left unscored rather than benchmarked against noise.
    """
    df = df.copy()

    for m in cfg.metrics:
        med = pd.Series(np.nan, index=df.index, dtype=float)
        cnt = pd.Series(0, index=df.index, dtype=int)
        basis = pd.Series("", index=df.index, dtype=object)
        rank = pd.Series(np.nan, index=df.index, dtype=float)

        for keys, label in ((list(cfg.peer_keys), "x".join(cfg.peer_keys)),
                            (list(cfg.fallback_peer_keys), "x".join(cfg.fallback_peer_keys))):
            need = med.isna()
            if not need.any():
                break
            g = df.groupby(keys, dropna=False)[m]
            # Peer count excludes the stock itself so a lone name can't
            # benchmark against its own multiple.
            valid = df[m].notna()
            grp_n = g.transform("count")
            self_n = valid.astype(int)
            peer_n = (grp_n - self_n).clip(lower=0)

            grp_med = g.transform(lambda s: _winsorized_median(s, cfg.winsor_pct))
            grp_rank = g.rank(pct=True)

            ok = need & (peer_n >= cfg.min_peers) & grp_med.notna() & (grp_med > 0)
            med[ok] = grp_med[ok]
            cnt[ok] = peer_n[ok]
            rank[ok] = grp_rank[ok]
            basis[ok] = label

        df[f"{m}_peer_median"] = med
        df[f"{m}_peer_n"] = cnt
        df[f"{m}_peer_basis"] = basis
        df[f"{m}_pct_rank"] = rank
        # Positive discount = cheaper than peers.
        df[f"{m}_discount"] = (med - df[m]) / med
    return df


# --------------------------------------------------------------------------
# 4. Score & rank
# --------------------------------------------------------------------------
def score(df: pd.DataFrame, cfg: ScreenConfig) -> pd.DataFrame:
    df = df.copy()
    disc_cols = [f"{m}_discount" for m in cfg.metrics]

    df["n_valid_metrics"] = df[disc_cols].notna().sum(axis=1)
    df["n_metrics_passing"] = (df[disc_cols] >= cfg.discount_threshold).sum(axis=1)
    df["metrics_passing"] = df.apply(
        lambda r: ", ".join(
            METRIC_LABELS[m] for m in cfg.metrics
            if pd.notna(r[f"{m}_discount"]) and r[f"{m}_discount"] >= cfg.discount_threshold
        ), axis=1)

    # Headline score: mean discount across metrics that have data. Averaging
    # only the passing ones would reward a stock that is 40% cheap on P/B and
    # 30% expensive on P/E.
    df["avg_discount"] = df[disc_cols].mean(axis=1, skipna=True)
    df["median_pct_rank"] = df[[f"{m}_pct_rank" for m in cfg.metrics]].median(axis=1, skipna=True)

    df["passes"] = (
        (df["n_valid_metrics"] >= cfg.min_valid_metrics)
        & (df["n_metrics_passing"] >= cfg.min_metrics_passing)
    )
    return df.sort_values(["passes", "avg_discount"], ascending=[False, False])


def run_screen(df: pd.DataFrame, usd_per_pln: float, cfg: ScreenConfig):
    df = sanitize_metrics(df, cfg)
    df = apply_usd_conversion(df, usd_per_pln)
    df, stats = apply_gates(df, cfg)
    if df.empty:
        return df, stats
    df = compute_peer_benchmarks(df, cfg)
    df = score(df, cfg)
    stats["passing"] = int(df["passes"].sum())
    return df, stats


def output_columns(cfg: ScreenConfig) -> list[str]:
    cols = ["ticker", "name", "board", "sector", "industry",
            "market_cap_usd", "adv_usd"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount", f"{m}_peer_n", f"{m}_pct_rank"]
    cols += ["n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
