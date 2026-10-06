"""Data providers for the Poland (GPW, Warsaw) screener.

  roster       GPW's own company search (gpw.pl/list-of-companies), the AJAX
               endpoint the page itself calls. One request returns every Main
               Market company with ISIN, GPW symbol, GPW's English subsector
               and every index it belongs to; one request per sector code then
               files each company under GPW's sector (Banks, Construction...)
               and macro-sector (the code's hundreds digit). This is the Polish
               counterpart of Deutsche Börse's workbook and Japan's data_j.xlsx:
               the whole market, classified by the operator.

  fundamentals yfinance .info on the Warsaw line (SYMBOL.WA), one call per
               ticker, fast_info as the market-cap fallback - Germany's
               snapshot(), plus the four .info fields the currency repair
               tests against (enterprise value, EBITDA, net income, ROE).

  liquidity    one batched yf.download for the size survivors: a true median
               daily traded value over the lookback. GPW is effectively the
               only venue for these lines, so unlike Xetra this is close to
               the whole market's volume.

  statements   Yahoo annual income statement + balance sheet + seven years of
               daily closes, for the 3-year history and the own-history
               screen. build_statement_record is the UK build's, unchanged.

THE CURRENCY TRAP: 38 of the ~394 priced Main Market lines report in another
currency while quoting in zloty (2026-10-05: 24 EUR, 8 USD, a few HUF, CZK,
UAH, AUD, CAD). Most are foreign secondaries or small, so on the default
gates the trap reaches two WIG20/mWIG40 names, Pepco and Asbis - and gets
both wrong by 4x. AmRest sits just under the size floor. Yahoo's
market cap, enterprise value, EPS and dividend yield on those lines are in
zloty; its book value per share, EBITDA and net income are in the REPORTING
currency. So priceToBook and enterpriseToEbitda divide zloty by euros - off by
4.3x for a euro reporter, 3.6x for a dollar one - and EPS / BPS mixes the two.
Measured 2026-10-05 on thirteen reporters in six currencies. snapshot() returns
what Yahoo says; pl_filters.repair_foreign_reporters fixes it, per row, with a
test rather than an assumption. FX for that repair is pln_per() below.

GPW'S WEB SERVER resets a connection now and then (a WAF, by the look of it),
and refuses a bare "Mozilla/5.0" user agent outright. Every request retries
with backoff and sends a full browser UA; the roster is cached per day because
it costs ~45 requests.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import re
import time

import numpy as np
import pandas as pd
import requests

import config_pl as K
from config_pl import ScreenConfig

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en"}

GPW_PAGE = "https://www.gpw.pl/list-of-companies"
GPW_AJAX = "https://www.gpw.pl/ajaxindex.php"
YAHOO_SUFFIX = ".WA"

# Every field snapshot() promises to return. Declared rather than inferred so a
# fully throttled fetch still produces a correctly-shaped frame.
SNAPSHOT_FIELDS = (
    "yf_name", "currency", "market_cap_local", "mcap_source", "close_local",
    "trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps",
    "book_value_ps", "div_yield", "yf_sector", "yf_industry", "yf_country",
    "quote_type", "yf_shares",
    # The currency the ACCOUNTS are in. Pepco quotes in zloty and reports in
    # euros; anything dividing a market value by a reported figure needs both.
    "fin_ccy",
    # The currency repair's evidence: Yahoo's own enterprise value (zloty),
    # trailing EBITDA and net income to common (reporting currency) and its
    # unit-free ROE. See pl_filters.repair_foreign_reporters.
    "yf_ev", "yf_ebitda", "yf_ni", "yf_roe",
)


def out_schema(df: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        df = pd.DataFrame({"ticker": tickers})
    df = df.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
    return df[df["ticker"].isin(tickers)].reset_index(drop=True)


def to_yahoo(symbol: str) -> str:
    """GPW symbol -> Yahoo symbol. PKO -> PKO.WA, 11B -> 11B.WA.

    A first guess: Yahoo files a handful of lines under a symbol GPW has since
    changed (a rename, a merger). resolve_symbols repairs those by ISIN.
    """
    t = re.sub(r"[^A-Z0-9]", "", str(symbol).strip().upper())
    return f"{t}{YAHOO_SUFFIX}" if t else ""


def _gpw_num(v) -> float:
    """GPW's English pages write 2,624.21 and '---' for missing."""
    try:
        return float(str(v).replace(",", "").replace("\xa0", "").replace(" ", ""))
    except ValueError:
        return float("nan")


def parse_company_rows(html: str) -> list[dict]:
    """The rows GPW's company search returns. Each is a link to the company
    factsheet carrying the ISIN, the name with the symbol in brackets, and a
    grey line of "market | index, index, ... | subsector"."""
    import warnings
    from bs4 import BeautifulSoup
    try:
        from bs4 import XMLParsedAsHTMLWarning
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    except ImportError:                                   # older bs4
        pass
    out = []
    for tr in BeautifulSoup(html, "lxml").find_all("tr"):
        a = tr.find("a", href=re.compile(r"isin="))
        small = tr.find("small")
        if not a or not small:
            continue
        isin = a["href"].split("isin=", 1)[1].strip()
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        m = re.match(r"(.*)\(([A-Z0-9]+)\)\s*$", text)
        parts = [re.sub(r"\s+", " ", p).strip() for p in small.get_text(" ").split("|")]
        out.append({
            "isin": isin,
            "name": (m.group(1) if m else text).strip(),
            "symbol": m.group(2) if m else "",
            "market": parts[0] if parts else "",
            "indices": ", ".join(t.strip() for t in parts[1].split(",") if t.strip())
                       if len(parts) > 2 else "",
            "subsector": parts[-1] if len(parts) > 1 else "",
        })
    return out


class GpwProvider:
    def __init__(self, cfg: ScreenConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.roster_asof = ""

    # -- http --------------------------------------------------------------
    def _req(self, method: str, url: str, tries: int = 8, **kw) -> requests.Response:
        """GPW resets connections intermittently; Yahoo's calls do not go
        through here. Backoff is short because a reset is instant, not a
        throttle."""
        last = None
        for i in range(tries):
            try:
                r = self.s.request(method, url, timeout=60, **kw)
                r.raise_for_status()
                return r
            except Exception as e:                        # noqa: BLE001
                last = e
                time.sleep(1.5 + i)
        raise RuntimeError(f"{method} {url} failed after {tries} tries: {last}")

    # -- calendar ----------------------------------------------------------
    def recent_business_days(self, n: int) -> list[str]:
        """Real GPW sessions, read off a liquid line - Polish holidays
        (Corpus Christi, 15 August, Epiphany, 24 December) match no generic
        calendar."""
        import yfinance as yf
        try:
            h = yf.Ticker("PKO.WA").history(period=f"{max(n * 2, 120)}d")
            if not h.empty:
                return [d.strftime("%Y-%m-%d") for d in h.index[-n:]]
        except Exception as e:
            log.warning("calendar via yfinance failed: %s", e)
        days = pd.bdate_range(end=pd.Timestamp.today(), periods=n)
        return [d.strftime("%Y-%m-%d") for d in days]

    # -- roster ------------------------------------------------------------
    def _search_form(self) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """The company search's own form fields, read off the page, minus the
        sector boxes - so a filter GPW adds later is sent the way the page
        sends it, rather than silently left out (left out, the search returns
        nothing: every filter group must be present)."""
        from bs4 import BeautifulSoup
        html = self._req("GET", GPW_PAGE).text
        form = BeautifulSoup(html, "lxml").find("form", id="search-form")
        if form is None:
            raise RuntimeError("company search form not found on the page")
        base: list[tuple[str, str]] = []
        sectors: dict[str, str] = {}
        for inp in form.find_all("input"):
            n = inp.get("name")
            if not n:
                continue
            if inp.get("type") == "checkbox":
                if not inp.has_attr("checked"):
                    continue
                if n.startswith("sector["):
                    sectors[str(inp.get("value"))] = inp.parent.get_text(" ", strip=True)
                    continue
                base.append((n, inp.get("value", "on")))
            else:
                base.append((n, "1000" if n == "limit" else inp.get("value", "")))
        return base, sectors

    def listing_roster(self) -> pd.DataFrame:
        """Every company on GPW's Main Market, with GPW's classification.

        The search answers with HTML rows, not a file, so the sector code is
        recovered by asking once per code - ~44 requests, cached for the day.
        A company the per-sector pass misses keeps an empty sector and is
        counted; check_setup.py asserts there are none.
        """
        cols = ["ticker", "symbol", "isin", "name", "board", "sector", "industry",
                "subsector", "sector_code", "country", "tier", "indices"]
        today = pd.Timestamp.today().strftime("%Y-%m-%d")
        path = os.path.join(self.cfg.cache_dir, f"roster_{today}.csv")
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, dtype=str, keep_default_na=False)
                # Names are re-derived from the code, so a change to
                # config_pl's labels or macro buckets reaches a cached roster.
                names = [K.sector_names(c, i) for c, i in zip(df["sector_code"], df["industry"])]
                df["industry"] = [n[0] for n in names]
                df["sector"] = [n[1] for n in names]
                self.roster_asof = today
                log.info("  roster: %d companies from today's cache", len(df))
                return df[cols]
            except Exception as e:                        # noqa: BLE001
                log.warning("  roster cache unreadable (%s), refetching", e)

        try:
            base, sectors = self._search_form()
            everything = base + [(f"sector[{c}]", c) for c in sectors]
            rows = parse_company_rows(self._req("POST", GPW_AJAX, data=everything).text)
            log.info("  Main Market: %d companies, %d sector codes", len(rows), len(sectors))
            code_of: dict[str, tuple[str, str]] = {}
            for i, (code, label) in enumerate(sectors.items(), 1):
                part = parse_company_rows(
                    self._req("POST", GPW_AJAX, data=base + [(f"sector[{code}]", code)]).text)
                for r in part:
                    code_of[r["isin"]] = (code, label)
                log.info("  sector %d/%d %s: %d", i, len(sectors), code, len(part))
                time.sleep(0.2)
        except Exception as e:                            # noqa: BLE001
            log.error("GPW company search failed: %s", e)
            return pd.DataFrame(columns=cols)

        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=cols)
        df["sector_code"] = df["isin"].map(lambda i: code_of.get(i, ("", ""))[0])
        names = [K.sector_names(*code_of.get(i, ("", ""))) for i in df["isin"]]
        df["industry"] = [n[0] for n in names]
        df["sector"] = [n[1] for n in names]
        df["board"] = "MAIN"
        # Country of domicile, from the ISIN prefix. GPW's own country filter
        # would cost another ~35 requests to say the same thing.
        df["country"] = df["isin"].str[:2]
        df["tier"] = df["indices"].map(K.size_tier)
        df["ticker"] = df["symbol"].map(to_yahoo)
        df = df[df["ticker"].ne("")].drop_duplicates(subset=["isin"], keep="first")
        missing = int(df["sector_code"].eq("").sum())
        if missing:
            log.warning("  %d companies matched no sector code", missing)
        self.roster_asof = today
        df = df[cols].reset_index(drop=True)
        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            df.to_csv(path, index=False, encoding="utf-8")
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write roster cache: %s", e)
        return df

    # -- company factsheets ------------------------------------------------
    def factsheets(self, isins: list[str], tabs=("indicatorsTab", "shareholdersTab"),
                   max_age_days: float = 7.0) -> dict[str, dict]:
        """GPW's own per-company tabs, cached for a week.

        indicatorsTab     shares issued, market value, book value, P/BV, P/E
        shareholdersTab   every holder of 5% or more: name, shares, votes

        One request per tab per company, so only ever asked for the few dozen
        names that need it: the size survivors (ownership) and the lines Yahoo
        prices without a share count (market cap). A shareholder register
        changes on a filing, not daily, hence the week.

        Returns {isin: {"indicators": {label: value}, "holders": [...]}}.
        """
        from bs4 import BeautifulSoup
        path = os.path.join(self.cfg.cache_dir, "gpw_factsheets.json")
        cache: dict = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    cache = json.load(fh)
            except Exception:                             # noqa: BLE001
                cache = {}
        now = time.time()
        out: dict[str, dict] = {}
        fetched = failed = 0
        for isin in dict.fromkeys(str(i) for i in isins if i):
            rec = cache.get(isin) or {}
            fresh = (now - rec.get("_t", 0)) < max_age_days * 86400
            if fresh and all(t in rec for t in tabs):
                out[isin] = rec
                continue
            rec = {"_t": now}
            try:
                for tab in tabs:
                    html = self._req("GET", f"{GPW_AJAX}?start={tab}&format=html&action="
                                     f"GPWListaSp&gls_isin={isin}&lang=EN", tries=5).text
                    rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                            for tr in BeautifulSoup(html, "lxml").find_all("tr")]
                    rows = [r for r in rows if r]
                    if tab == "shareholdersTab":
                        rec[tab] = [{"name": r[0], "pct_shares": _gpw_num(r[2]),
                                     "pct_votes": _gpw_num(r[4])}
                                    for r in rows[1:] if len(r) >= 5]
                    else:
                        rec[tab] = {r[0].rstrip(":").strip(): r[-1] for r in rows if len(r) >= 2}
                    time.sleep(0.15)
                fetched += 1
            except Exception as e:                        # noqa: BLE001
                log.warning("  GPW factsheet %s: %s", isin, e)
                failed += 1
                continue
            cache[isin] = rec
            out[isin] = rec
        if fetched:
            try:
                os.makedirs(self.cfg.cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, ensure_ascii=False)
            except Exception as e:                        # noqa: BLE001
                log.warning("  could not write factsheet cache: %s", e)
        log.info("  GPW factsheets: %d requested, %d fetched, %d failed",
                 len(set(isins)), fetched, failed)
        return out

    # -- symbol resolution -------------------------------------------------
    def resolve_symbols(self, rows: pd.DataFrame) -> dict[str, str]:
        """Yahoo symbols for lines whose GPW symbol Yahoo does not know.

        The case that found this: Santander Bank Polska became Erste Bank
        Polska in 2026 and GPW's symbol moved from SPL to EBP; SPL.WA now
        answers with nothing. A miss reads as "no market cap", which is
        indistinguishable from "too small" - so a WIG20 bank would fail the
        size gate silently. Yahoo's search resolves an ISIN exactly; Warsaw
        quotes are preferred and a hit only on a foreign exchange is left
        unresolved, because that line's primary market is not here. Cached
        for a week - an ISIN-to-symbol mapping changes on a corporate action.

        Returns {roster ticker: resolved Yahoo symbol}, only for lines that
        resolved to something different.
        """
        import yfinance as yf
        path = os.path.join(self.cfg.cache_dir, "isin_symbols.json")
        cache: dict = {}
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < 7 * 86400:
            try:
                with open(path, encoding="utf-8") as fh:
                    cache = json.load(fh)
            except Exception:                             # noqa: BLE001
                cache = {}

        out: dict[str, str] = {}
        for _, r in rows.iterrows():
            isin, old = str(r["isin"]), str(r["ticker"])
            if isin not in cache:
                try:
                    quotes = yf.Search(isin, max_results=8, news_count=0).quotes or []
                    syms = [str(q.get("symbol") or "") for q in quotes]
                except Exception as e:                    # noqa: BLE001
                    log.debug("ISIN search %s: %s", isin, e)
                    continue
                cache[isin] = [x for x in syms if x.endswith(YAHOO_SUFFIX)]
                time.sleep(self.cfg.request_delay)
            cand = cache.get(isin) or []
            if cand and cand[0] != old:
                out[old] = cand[0]
                if len(cand) > 1:
                    out[old + "|alt"] = cand[1]
        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write ISIN cache: %s", e)
        n = len([k for k in out if not k.endswith("|alt")])
        log.info("  resolved %d of %d unpriced lines by ISIN", n, len(rows))
        return out

    # -- fundamentals ------------------------------------------------------
    def _cache_path(self, asof: str) -> str:
        return os.path.join(self.cfg.cache_dir, f"snapshot_{asof or 'latest'}.csv")

    def snapshot(self, tickers: list[str], asof: str = "") -> pd.DataFrame:
        """One yfinance .info per ticker, fast_info when .info has no market cap.

        Cached per session date for the reason the UK build found: Yahoo
        throttles by returning dicts with fields MISSING, not by raising, so
        successive runs must fill gaps rather than re-ask for everything.
        """
        import yfinance as yf

        cached = pd.DataFrame()
        path = self._cache_path(asof)
        if os.path.exists(path):
            age_h = (time.time() - os.path.getmtime(path)) / 3600.0
            if age_h <= self.cfg.cache_ttl_hours:
                try:
                    cached = pd.read_csv(path)
                    log.info("  cache: %d rows from %s (%.1fh old)",
                             len(cached), os.path.basename(path), age_h)
                except Exception as e:                    # noqa: BLE001
                    log.warning("  cache unreadable (%s), refetching", e)
                stale = [f for f in SNAPSHOT_FIELDS if f not in cached.columns]
                if stale and not cached.empty:
                    log.info("  cache predates field(s) %s - refetching",
                             ", ".join(stale))
                    cached = pd.DataFrame()

        have = set()
        if not cached.empty and "market_cap_local" in cached.columns:
            have = set(cached.loc[cached["market_cap_local"].notna(), "ticker"])
        todo = [t for t in tickers if t not in have]
        if not todo:
            log.info("  all %d tickers served from cache", len(tickers))
            return cached[cached["ticker"].isin(tickers)].reset_index(drop=True)

        # The limit is global (it rejects the crumb request), so probe once
        # single-threaded before opening the pool.
        try:
            yf.Ticker(todo[0]).info
        except Exception as e:                            # noqa: BLE001
            if "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e):
                log.error("Yahoo is rate-limiting this IP (%s). Nothing was "
                          "fetched; wait a few minutes and re-run - the cache "
                          "keeps whatever has already arrived.", type(e).__name__)
                return out_schema(cached, tickers)
            log.debug("probe ticker %s failed non-fatally: %s", todo[0], e)

        def one(t: str) -> dict:
            rec = {"ticker": t}
            i = {}
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            for attempt in (0, 1):
                try:
                    i = yf.Ticker(t).info or {}
                    if i.get("marketCap") is not None or i.get("regularMarketPrice"):
                        break
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s attempt %d: %s", t, attempt, e)
                    if "RateLimit" in type(e).__name__:
                        return rec
                if attempt == 0:
                    time.sleep(0.5 + self.cfg.request_delay)
            if not i:
                return rec
            price = i.get("regularMarketPrice") or i.get("currentPrice")
            mcap, src = i.get("marketCap"), "info"
            if mcap is None and price:
                # Germany's Allianz case. fast_info is price x Yahoo's
                # share-count history; a separate request, so only when needed.
                try:
                    mcap = yf.Ticker(t).fast_info.get("marketCap")
                    src = "fast_info" if mcap else ""
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s fast_info: %s", t, e)
                    src = ""
            rec.update({
                "yf_name": i.get("longName") or i.get("shortName"),
                "currency": i.get("currency"),
                "market_cap_local": mcap,
                "mcap_source": src if mcap else "",
                "close_local": price,
                "trailing_pe": i.get("trailingPE"),
                "price_to_book": i.get("priceToBook"),
                "ev_to_ebitda": i.get("enterpriseToEbitda"),
                # EPS is in the QUOTE currency on every .WA line measured; book
                # value is in the REPORTING currency. Equal for zloty reporters;
                # see the module docstring for everyone else.
                "trailing_eps": i.get("trailingEps"),
                "book_value_ps": i.get("bookValue"),
                # Percent (3.09 = 3.09%), the same scale as the 2% floor. Zloty
                # over zloty on every line measured, reporters in euros and
                # dollars included, so it needs no repair.
                "div_yield": i.get("dividendYield"),
                "yf_sector": i.get("sector"),
                "yf_industry": i.get("industry"),
                "yf_country": i.get("country"),
                "quote_type": i.get("quoteType"),
                "yf_shares": i.get("impliedSharesOutstanding") or i.get("sharesOutstanding"),
                "fin_ccy": i.get("financialCurrency"),
                "yf_ev": i.get("enterpriseValue"),
                "yf_ebitda": i.get("ebitda"),
                "yf_ni": i.get("netIncomeToCommon"),
                "yf_roe": i.get("returnOnEquity"),
            })
            return rec

        log.info("  fetching %d tickers (%d already cached)", len(todo), len(have))
        rows: list[dict] = []
        with cf.ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            for n, rec in enumerate(ex.map(one, todo), 1):
                rows.append(rec)
                if n % 50 == 0:
                    log.info("  fetched %d/%d", n, len(todo))

        fresh = pd.DataFrame(rows)
        parts = [d for d in (cached, fresh) if not d.empty]
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        out = out.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
        # Priced rows first so a cached hit beats a fresh throttled blank.
        out = out.sort_values("market_cap_local", na_position="last")
        out = out.drop_duplicates(subset=["ticker"], keep="first")

        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            out.to_csv(self._cache_path(asof), index=False, encoding="utf-8")
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write cache: %s", e)

        out = out[out["ticker"].isin(tickers)].reset_index(drop=True)
        got = int(out["market_cap_local"].notna().sum())
        fb = int((out["mcap_source"] == "fast_info").sum())
        log.info("  %d/%d tickers have a market cap (%d via fast_info)",
                 got, len(tickers), fb)
        return out

    # -- liquidity ---------------------------------------------------------
    def price_panel(self, tickers: list[str], days: int, chunk: int = 80) -> pd.DataFrame:
        """Daily close and volume for every ticker at once.

        yf.download is a different, batched endpoint from .info and does not
        share its throttle in practice (Japan measured ~20 requests for 661
        names). So the liquidity gate costs a handful of requests and still
        runs before any per-ticker statement work.
        """
        import yfinance as yf
        period = f"{max(int(days * 1.6), 30)}d"
        frames = []
        for i in range(0, len(tickers), chunk):
            part = tickers[i:i + chunk]
            try:
                px = yf.download(part, period=period, interval="1d", progress=False,
                                 auto_adjust=False, actions=False, threads=True,
                                 group_by="column")
            except Exception as e:                                # noqa: BLE001
                log.warning("price chunk %d failed: %s", i // chunk, e)
                continue
            if px is not None and not px.empty:
                frames.append(px)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    @staticmethod
    def average_daily_value(panel: pd.DataFrame, tickers: list[str],
                            days: int) -> pd.DataFrame:
        """Median daily traded value in PLN over the last `days` sessions.

        Median, not mean: one index-rebalance day can be twenty times a name's
        normal volume.
        """
        if panel.empty:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        try:
            close, vol = panel["Close"], panel["Volume"]
        except KeyError:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        if isinstance(close, pd.Series):                      # single ticker
            close, vol = close.to_frame(tickers[0]), vol.to_frame(tickers[0])
        rows = []
        for t in tickers:
            if t not in close.columns or t not in vol.columns:
                continue
            c = close[t]
            v = vol[t]
            if isinstance(c, pd.DataFrame):                   # duplicate column
                c, v = c.iloc[:, 0], v.iloc[:, 0]
            tv = (pd.to_numeric(c, errors="coerce")
                  * pd.to_numeric(v, errors="coerce")).dropna().tail(days)
            if tv.empty:
                continue
            rows.append({"ticker": t, "adv_local": float(tv.median()),
                         "adv_sessions": int(len(tv))})
        return pd.DataFrame(rows)

    # -- FX ----------------------------------------------------------------
    def _rate(self, base: str, quote: str, lo: float, hi: float) -> float:
        """Units of `quote` per one `base`: Yahoo, then the ECB via Frankfurter."""
        import yfinance as yf
        try:
            h = yf.Ticker(f"{base}{quote}=X").history(period="5d")
            if not h.empty:
                rate = float(h["Close"].iloc[-1])
                if lo < rate < hi:
                    return rate
        except Exception as e:                            # noqa: BLE001
            log.info("FX %s/%s via Yahoo failed (%s)", base, quote, e)
        try:
            r = self.s.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}",
                           timeout=20)
            rate = float(r.json()["rates"][quote])
            if lo < rate < hi:
                log.info("FX %s/%s from Frankfurter (ECB reference)", base, quote)
                return rate
        except Exception as e:                            # noqa: BLE001
            log.warning("FX %s/%s via Frankfurter failed: %s", base, quote, e)
        return float("nan")

    def pln_to_usd(self) -> float:
        """USD per PLN - Germany's direction (USD per unit of local currency,
        multiplied). The name says which way round it is so a copied line
        cannot flip it silently."""
        rate = self._rate("PLN", "USD", 0.12, 0.5)
        if np.isfinite(rate):
            return rate
        log.warning("FX: both live sources failed, using the hardcoded 0.27. "
                    "The USD size gate is only as good as this number.")
        return 0.27

    def pln_per(self, currencies) -> dict[str, float]:
        """PLN per one unit of each reporting currency, for the currency repair.

        A rate that cannot be found is NaN, and the repair then refuses the
        affected multiples for that company rather than guessing - a guessed
        rate is exactly the 4x error the repair exists to remove.
        """
        out = {"PLN": 1.0}
        for c in sorted({str(c) for c in currencies if isinstance(c, str) and c}):
            if c not in out:
                out[c] = self._rate(c, "PLN", 1e-4, 1e3)
                log.info("  FX: 1 %s = %.4f PLN", c, out[c])
        return out


# ---------------------------------------------------------------------------
# Filed statements: three-year history and the own-history benchmark
# ---------------------------------------------------------------------------
# Ported from the UK build unchanged in logic, via Germany. The worked examples
# in the comments below (NatWest, Reckitt, Shell, Craneware) were measured in
# London; the rules they justify are Yahoo's, so they hold for .WA lines too.
# The Polish cases that exercise them are in test_poland.py.
# Row names Yahoo uses in its annual statements, first match wins. Net income
# is taken to COMMON shareholders where Yahoo separates it: NatWest's differs
# by ~6% because of AT1 coupons, and a P/E is a price for the common equity.
IS_ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "op": ("Operating Income", "Total Operating Income As Reported"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "np": ("Net Income Common Stockholders", "Net Income"),
    # Yahoo's "normalized" lines strip exactly its Total Unusual Items row. The
    # 3-year growth history stays on the REPORTED rows above, as Korea's does
    # - that is what happened. The own-history VALUATION uses these instead,
    # because a one-off is not a change in what the business is worth: Reckitt
    # sold Essential Home in 2025, reported EBITDA jumped 4,760 vs 3,966
    # normalized and net income 3,182 vs 2,535, and on reported figures it
    # passed the history screen on P/E and EV/EBITDA while its P/B - which a
    # disposal gain does not move - sat only 15% below its history. For an
    # ordinary year the two lines agree to within a few percent.
    "np_norm": ("Normalized Income",),
    "ebitda_norm": ("Normalized EBITDA",),
}
BS_ROWS = {
    "equity": ("Common Stock Equity", "Stockholders Equity"),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "debt": ("Total Debt",),
    "cash": ("Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments"),
    "mi": ("Minority Interest",),
}

# Quote currencies Yahoo reports in minor units, and the major unit each is.
MINOR_CCY = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
             "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# Consecutive filed share counts outside this band are treated as a corporate
# action (consolidation, split, rights issue) rather than buybacks. Shell's
# heavy buybacks move ~7% a year, so the band is wide enough to leave them
# alone. It cannot catch small consolidations; it catches the ones that would
# otherwise manufacture a 30%+ "discount" out of a unit change.
SHARE_BREAK_BAND = (0.67, 1.5)
# Today's price x latest filed shares must land near today's market cap,
# once any split or consolidation since the filing is allowed for. If it does
# not, the price series and the share count are on different bases and every
# historical market value built from them is wrong by the same factor.
#
# Measured 2026-10-06 on names with no split since their filing: UK 1st-99th
# percentile 0.75-1.07 (max 1.12), Germany 0.91-1.10. Below 1 is shares issued
# since the filing (IQE 0.73, Rockhopper 0.79), above it buybacks. The old
# 0.6-1.6 admitted an unrestated 3:2 split (0.67) or 4:3 consolidation (1.33)
# - a fake 25-33% move in every past valuation. 0.7-1.25 refuses both and
# keeps every healthy name measured.
NOW_BASIS_BAND = (0.7, 1.25)


def _splits_after(splits, when) -> list:
    """Split / consolidation ratios Yahoo recorded after `when`, oldest first.

    Yahoo reports a consolidation as a ratio below one (Johnson Matthey's
    4-for-3 in Aug 2026 is 0.75) and a demerger price adjustment the same way,
    so the caller decides what a ratio means by whether it explains the share
    count, never by its size alone.
    """
    if splits is None or len(splits) == 0:
        return []
    s = pd.to_numeric(splits, errors="coerce")
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize() \
        if getattr(s.index, "tz", None) is not None else pd.to_datetime(s.index).normalize()
    s = s[(s.index > when) & (s > 0) & (s != 1)].sort_index()
    return [float(v) for v in s.tolist()]
# A latest filing older than this is not the latest filing - Yahoo has missed
# one. "Today" is then today's price over earnings from two years ago, while
# yfinance's own twelve-month figure knows better: Craneware read 52.6x on
# the same basis against 28.6x trailing, and passed the history screen on that
# gap. 13 of 251 survivors (Sept 2026) had a newest filed year of 2024. Eighteen
# months leaves room for a late filer without admitting a skipped year.
MAX_FILING_AGE_DAYS = 548

# The statements cache stores DERIVED records, not raw frames (seven years of
# daily prices per name would be ~40x larger). So a change to
# build_statement_record does not reach names already cached - bump this and
# the next run refetches instead of serving numbers the new code would not
# produce.
STATEMENTS_CACHE_VERSION = 4


def cagr(values: list) -> float:
    """Compound annual growth across the span the values actually cover.

    Identical to the Korea build: undefined when the base is zero or negative.
    A company that lost money three years ago has no meaningful growth RATE,
    and inventing one is worse than reporting nothing. The yearly figures
    always ship alongside, so nothing is hidden by this.
    """
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return np.nan
    return (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1.0


def major_ccy(ccy) -> tuple[str, float]:
    """(major currency, divisor) for a Yahoo quote currency."""
    c = str(ccy or "")
    return MINOR_CCY.get(c, (c, 1.0))


def _row(df: pd.DataFrame, names) -> pd.Series:
    """First matching statement row, indexed by fiscal year-end, oldest first."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s.sort_index()
    return pd.Series(dtype=float)


def _at(series: pd.Series, when: pd.Timestamp, tolerance_days: int = 10) -> float:
    """Last value on or before `when`, if it is within `tolerance_days`. A
    fiscal year-end that falls in a data gap is missing, not the nearest price
    from months earlier."""
    if series is None or series.empty:
        return np.nan
    s = series.loc[:when].dropna()
    if s.empty or (when - s.index[-1]).days > tolerance_days:
        return np.nan
    return float(s.iloc[-1])


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return np.nan
    return f if np.isfinite(f) else np.nan


def build_statement_record(inc: pd.DataFrame, bs: pd.DataFrame,
                           px: pd.Series, fx: pd.Series | None,
                           quote_ccy: str, fin_ccy: str,
                           close_now: float, mcap_now: float,
                           fin_years: int = 3, max_hist: int = 5,
                           asof: str | pd.Timestamp | None = None,
                           splits: pd.Series | None = None) -> dict:
    """Everything the two statement-based features need, from raw frames.

    Pure: no network, so test_poland.py can exercise every trap offline. `px` is
    the daily close in QUOTE units (zloty for every .WA line); `fx` converts the
    quote's MAJOR currency into the reporting currency (None when they are the
    same); `close_now` and `mcap_now` are in the quote's major currency.

    Every multiple is a market value over a reported total, never a price over
    a per-share figure. Yahoo's per-share rows are in reporting-currency units
    against a price in the quote currency (Pepco: euros against zloty), and
    its EPS row is frequently rounded to zero; totals over totals sidestep both.

    Three-year history: the last `fin_years` filed years, oldest first, in
    millions of the REPORTING currency. Deliberately not converted - a growth
    rate should describe the business, not the euro against the zloty.

    Own history: for each filed year, that year-end market value over that
    year's filed figures, converted into the reporting currency at that
    year-end rate. Plus the latest filing's components, so the screen can put
    TODAY's market value over them on exactly the same definitions - see
    pl_filters.apply_history_screen for why that matters.
    """
    rec: dict = {"fin_ccy": fin_ccy or ""}

    rows = {k: _row(inc, v) for k, v in IS_ROWS.items()}
    rows.update({k: _row(bs, v) for k, v in BS_ROWS.items()})

    # A fiscal year counts as filed when it has revenue or net income. Yahoo
    # pads the frame with an extra, entirely empty oldest column (2021 in
    # every UK name checked, and in German and Polish ones too), and that must not be read
    # as a zero year.
    dates = sorted(set(rows["rev"].dropna().index) | set(rows["np"].dropna().index))
    if not dates:
        rec["hist_note"] = "no filed years"
        return rec

    # ---- three-year history ---------------------------------------------
    fy = dates[-fin_years:]
    rec["fin_years"] = ",".join(d.strftime("%Y") for d in fy)
    rec["fin_n"] = len(fy)
    for key in ("rev", "op", "ebitda", "np"):
        series = [_num(rows[key].get(d)) / 1e6 for d in fy]
        for i, v in enumerate(series, 1):
            rec[f"{key}_y{i}"] = round(v, 1) if np.isfinite(v) else np.nan
        rec[f"{key}_cagr"] = cagr(series)

    # ---- own history ------------------------------------------------------
    hy = dates[-max_hist:]
    rec["hist_years"] = ",".join(d.strftime("%Y") for d in hy)
    q_major, q_div = major_ccy(quote_ccy)
    same_ccy = bool(fin_ccy) and fin_ccy == q_major
    # No reporting currency is not the same as zloty. Assuming it would
    # treat a dollar reporter's accounts as pounds and scale every historical
    # multiple by the exchange rate - a fake discount or premium of 20-35%.
    # Missing means no benchmark (invariant 2), never a guess.
    if not fin_ccy:
        rec["hist_note"] = "no reporting currency"

    def fx_at(when):
        if same_ccy:
            return 1.0
        return _at(fx, when) if fx is not None else np.nan

    shares = [_num(rows["shares"].get(d)) for d in hy]
    lf = hy[-1]

    # Earnings basis for the VALUATION: normalized where Yahoo has it for
    # every year in the window, reported otherwise - never a mix within one
    # company's series, because a history that switches definition part-way
    # compares a year with itself on two different measures (see IS_ROWS).
    def pick(norm_key, rep_key):
        n = rows[norm_key]
        if all(np.isfinite(_num(n.get(d))) for d in hy):
            return n, "normalized"
        return rows[rep_key], "reported"
    ni_row, ni_basis = pick("np_norm", "np")
    eb_row, _ = pick("ebitda_norm", "ebitda")
    rec["hist_earnings"] = ni_basis

    # Guard 0: a stale latest filing. Checked first, because every other
    # number in the record would be struck against it.
    if asof is not None and "hist_note" not in rec:
        age = (pd.Timestamp(asof).normalize() - lf).days
        if age > MAX_FILING_AGE_DAYS:
            rec["hist_note"] = "stale filings"

    # Guard 1: a share count that jumps between filings is a corporate action.
    # The price series is split-adjusted while filed counts are not
    # necessarily, so a market value built across the break is wrong by the
    # split ratio - which the screen would read as a huge discount.
    valid = [s for s in shares if np.isfinite(s) and s > 0]
    for a, b in zip(valid, valid[1:]):
        if not (SHARE_BREAK_BAND[0] <= b / a <= SHARE_BREAK_BAND[1]):
            rec["hist_note"] = "share-count break"
            break

    # Guard 2: today's price x latest filed shares against today's market cap,
    # allowing for a split or consolidation Yahoo has not restated yet.
    #
    # Yahoo's price series is adjusted for every split it records; its filed
    # share counts catch up later. Johnson Matthey consolidated 4-for-3 in Aug
    # 2026: the prices were rescaled at once, the FY2026 filing still carried
    # the old 167.9m shares against 125.9m today. The ratio here read 1.33,
    # inside the old 0.6-1.6 band, so every past market value was a third too
    # high and the history screen showed a ~20% discount to its own history
    # that did not exist. A 3:2 split would do the same in the other
    # direction. So, as the Japan build does: among "no change" and each
    # suffix of the ratios recorded since the filing, take the one that
    # explains today's count, restate every filed year by it, and refuse a gap
    # nothing explains.
    s_lf = _num(rows["shares"].get(lf))
    g = 1.0
    if "hist_note" not in rec and np.isfinite(s_lf) and s_lf > 0:
        mc = _num(mcap_now)
        ratio = (_num(close_now) * s_lf / mc) if mc else np.nan
        rec["basis_ratio"] = ratio
        if np.isfinite(ratio) and ratio > 0:
            fs = _splits_after(splits, lf)
            cands = [1.0] + [float(np.prod(fs[k:])) for k in range(len(fs))]
            g = min(cands, key=lambda c: abs(np.log(ratio * c)))
        if not (np.isfinite(ratio) and NOW_BASIS_BAND[0] <= ratio * g <= NOW_BASIS_BAND[1]):
            rec["hist_note"] = "price/share basis mismatch"
            g = 1.0
    # The factor the latest filing's per-share basis is off by: also what the
    # screen uses to check Yahoo's .info per-share fields (see the filters).
    rec["share_basis_g"] = g
    rec["lf_shares"] = s_lf
    shares = [s * g for s in shares]

    per, pbr, evx = [], [], []
    for d, sh in zip(hy, shares):
        p = _at(px, d) / q_div if px is not None else np.nan
        mv = p * sh * fx_at(d)        # year-end market value, reporting ccy
        ni, eq = _num(ni_row.get(d)), _num(rows["equity"].get(d))
        eb = _num(eb_row.get(d))
        debt, cash = _num(rows["debt"].get(d)), _num(rows["cash"].get(d))
        mi = _num(rows["mi"].get(d))
        mi = 0.0 if not np.isfinite(mi) else mi
        # Non-positive denominators become NaN here; bounds are applied by the
        # screen, which is also where a loss year drops out of the benchmark.
        per.append(mv / ni if np.isfinite(mv) and ni > 0 else np.nan)
        pbr.append(mv / eq if np.isfinite(mv) and eq > 0 else np.nan)
        ev = mv + debt - cash + mi
        evx.append(ev / eb if np.isfinite(ev) and eb > 0 else np.nan)

    if "hist_note" in rec:
        per = pbr = evx = []
    rec["hist_per"] = [round(v, 2) if np.isfinite(v) else None for v in per]
    rec["hist_pbr"] = [round(v, 3) if np.isfinite(v) else None for v in pbr]
    rec["hist_evx"] = [round(v, 2) if np.isfinite(v) else None for v in evx]

    # Latest filing's components, for today's multiples on the same basis.
    fx_last = np.nan
    if same_ccy:
        fx_last = 1.0
    elif fx is not None and not fx.dropna().empty:
        fx_last = float(fx.dropna().iloc[-1])
    rec.update({
        # On the same earnings basis as the history, or today's value would be
        # compared across two definitions - the thing this whole design avoids.
        "lf_ni": _num(ni_row.get(lf)), "lf_equity": _num(rows["equity"].get(lf)),
        "lf_ebitda": _num(eb_row.get(lf)), "lf_debt": _num(rows["debt"].get(lf)),
        "lf_cash": _num(rows["cash"].get(lf)), "lf_mi": _num(rows["mi"].get(lf)),
        # Today's quote-major -> reporting-currency rate, carried so the screen
        # does not have to look it up again.
        "fx_now": fx_last,
    })
    rec.setdefault("hist_note", "")
    return rec


def _json_safe(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, (float, np.floating)):
            out[k] = float(v) if np.isfinite(float(v)) else None
        elif isinstance(v, np.integer):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def fetch_statements(snap: pd.DataFrame, cfg: ScreenConfig,
                     asof: str = "") -> pd.DataFrame:
    """Filed statements for the size-gated survivors. Invariant 7: this is the
    slowest work in the run, so it only ever sees names that already cleared
    every cheap filter.

    Three Yahoo calls per ticker (income statement, balance sheet, seven years
    of daily closes) plus one FX history per foreign reporting currency, paced
    like snapshot() and cached per session date for the same reason: a
    throttled statement call returns an empty frame, not an error, and an
    empty frame reads as "no history" - which quietly removes a name from the
    third screen instead of failing loudly.
    """
    import yfinance as yf

    tickers = snap["ticker"].tolist()
    path = os.path.join(cfg.cache_dir,
                        f"statements_v{STATEMENTS_CACHE_VERSION}_{asof or 'latest'}.json")
    cached: dict = {}
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= cfg.cache_ttl_hours:
            try:
                with open(path, encoding="utf-8") as fh:
                    cached = json.load(fh)
            except Exception as e:
                log.warning("  statements cache unreadable (%s), refetching", e)

    # FX: one history per reporting currency that differs from the quote's.
    pairs = set()
    for _, r in snap.iterrows():
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy")
        if isinstance(f, str) and f and f != q_major:
            pairs.add((q_major, f))
    fxs: dict = {}
    for q, f in sorted(pairs):
        try:
            h = yf.Ticker(f"{q}{f}=X").history(period="7y", interval="1d")
            s = h["Close"].copy()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            fxs[(q, f)] = s
            log.info("  FX history %s->%s: %d days", q, f, len(s))
        except Exception as e:
            log.warning("  FX history %s->%s failed: %s - those reporters get "
                        "no own-history benchmark", q, f, e)

    todo = [t for t in tickers if t not in cached]
    rows = {t: r for t, r in snap.set_index("ticker").iterrows()}

    def one(t: str):
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
        r = rows[t]
        try:
            tk = yf.Ticker(t)
            inc, bs = tk.income_stmt, tk.balance_sheet
            h = tk.history(period="7y", interval="1d", auto_adjust=False)
        except Exception as e:
            log.debug("%s statements: %s", t, e)
            return t, None
        if inc is None or inc.empty:
            return t, None
        px = pd.Series(dtype=float)
        if h is not None and not h.empty:
            px = h["Close"].copy()
            px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy") if isinstance(r.get("fin_ccy"), str) else ""
        rec = build_statement_record(
            inc, bs, px, fxs.get((q_major, f)), r.get("currency"), f,
            _num(r.get("close_local")), _num(r.get("market_cap_local")),
            asof=asof or None,
            splits=(h["Stock Splits"] if h is not None and "Stock Splits" in h else None))
        return t, _json_safe(rec)

    if todo:
        log.info("  statements: fetching %d tickers (%d cached)", len(todo),
                 len(tickers) - len(todo))
        with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
            for n, (t, rec) in enumerate(ex.map(one, todo), 1):
                if rec is not None:
                    cached[t] = rec
                if n % 50 == 0:
                    log.info("  statements %d/%d", n, len(todo))
        try:
            os.makedirs(cfg.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cached, fh)
        except Exception as e:
            log.warning("  could not write statements cache: %s", e)

    got = [t for t in tickers if t in cached]
    log.info("  statements for %d/%d survivors", len(got), len(tickers))
    if len(got) < 0.8 * len(tickers):
        log.warning("  only %.0f%% have statements - Yahoo is probably "
                    "throttling. The two statement features will be thin; "
                    "re-run to fill the gaps from cache.",
                    100.0 * len(got) / max(len(tickers), 1))
    if not got:
        return pd.DataFrame(columns=["ticker"])
    out = pd.DataFrame([{"ticker": t, **cached[t]} for t in got])
    for k in ("hist_per", "hist_pbr", "hist_evx"):
        if k in out.columns:
            out[k] = out[k].map(lambda v: v if isinstance(v, list) else [])
    # fin_ccy also comes from the snapshot, and the snapshot's is the one kept.
    return out.drop(columns=["fin_ccy"], errors="ignore")
