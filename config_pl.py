"""Configuration for the Poland (GPW, Warsaw) valuation screener.

Fifth market after Korea, the UK, Japan and Germany. The quality floor, the
discount test and the history screen are the user's screening preferences and
are carried over unchanged. What is local:

  * universe hygiene - Poland's structural traps are listed investment
    vehicles, IAS 40 landlords, and foreign companies whose Warsaw line is a
    secondary listing (Banco Santander, UniCredit, CEZ, MOL, Krka)
  * classification - GPW's own sector codes, read off the exchange's company
    search, with its eight macro-sectors as the fallback, like Germany's
    subsector -> sector
  * the currency trap - see repair_foreign_reporters in pl_filters: Yahoo
    divides a PLN price by a book value in EUR or USD for the companies that
    report in a foreign currency
  * two flags read from GPW's shareholder register - state control and a
    majority holder - because those describe most of Warsaw's large caps
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VALUATION_METRICS = ("trailing_pe", "price_to_book", "ev_to_ebitda")

# Polish convention is C/Z and C/WK, but the dashboard is read in English and
# the other European pages say P/E and P/B.
METRIC_LABELS = {
    "trailing_pe": "P/E",
    "price_to_book": "P/B",
    "ev_to_ebitda": "EV/EBITDA",
}

# Same reasoning as Korea: a zero or negative multiple means "no earnings" or
# "negative equity", never "cheap". The upper bounds also catch what the
# currency trap leaves behind when it cannot be repaired: Pepco's P/B reads
# 119.5 on Yahoo because a PLN price is divided by a EUR book.
METRIC_BOUNDS = {
    "trailing_pe": (1.0, 200.0),
    "price_to_book": (0.05, 30.0),
    "ev_to_ebitda": (0.5, 100.0),
}

# ---------------------------------------------------------------------------
# GPW classification
# ---------------------------------------------------------------------------
# The company search files every Main Market company under one of ~44 sector
# codes. The hundreds digit is GPW's macro-sector, so the two-level hierarchy
# is in the code itself. The search page names the sectors in Polish only;
# these are the English labels the dashboard shows. A code GPW adds later and
# this table does not know falls back to the Polish label - it still forms its
# own cohort, it just reads in Polish.
SECTORS = {
    "110": "Banks",
    "120": "Insurance",
    "130": "Capital Markets",
    "140": "Real Estate",
    "150": "Leasing & Factoring",
    "160": "Debt Collection",
    "170": "Financial Intermediation",
    "180": "Investment Activities",
    "185": "Raw Materials",
    "210": "Oil & Gas",
    "220": "Energy",
    "310": "Chemicals",
    "320": "Mining",
    "330": "Metallurgy",
    "350": "Rubber & Plastics",
    "360": "Wood & Paper",
    "370": "Recycling",
    "399": "Other Services",
    "410": "Construction",
    "420": "Electrical Engineering",
    "430": "Transport & Logistics",
    "440": "Business Supplies",
    "450": "Business Services",
    "510": "Food & Beverages",
    "520": "Clothing & Cosmetics",
    "530": "Home Furnishings",
    "540": "Automotive",
    "590": "Other Consumer Goods",
    "610": "Wholesale",
    "620": "Retail Chains",
    "630": "Leisure",
    "640": "Media",
    "650": "Video Games",
    "660": "E-commerce",
    "690": "Other Trade & Services",
    "710": "Hospitals & Clinics",
    "720": "Medical Equipment",
    "730": "Pharmaceuticals",
    "740": "Pharma Distribution",
    "750": "Biotechnology",
    "790": "Other Health Care",
    "810": "Telecoms",
    "820": "IT",
    "830": "New Technologies",
}
MACRO_SECTORS = {
    "1": "Finance",
    "2": "Fuels & Energy",
    "3": "Chemicals & Raw Materials",
    "4": "Industrials",
    "5": "Consumer Goods",
    "6": "Trade & Services",
    "7": "Health Care",
    "8": "Technology",
}

# Financials, by GPW sector, so invariant 6 is exact. Real estate is NOT one -
# GPW files it under the Finance macro-sector (code 140), but a developer or a
# landlord has a meaningful enterprise value. Debt collectors (KRUK) are: their
# "EBITDA" is collections on purchased portfolios and their debt funds the
# portfolio, which is a balance-sheet business like a lender's.
FINANCIAL_INDUSTRIES = ("Banks", "Insurance", "Capital Markets", "Leasing & Factoring",
                        "Debt Collection", "Financial Intermediation",
                        "Investment Activities")

# Listed investment vehicles - Warsaw's version of London's investment trusts
# and Frankfurt's PE holdings: a book of stakes valued at a standing discount
# to NAV. GPW names the class outright (code 180), so this is a lookup.
INVESTMENT_INDUSTRIES = ("Investment Activities",)

# Listed landlords, by GPW's own subsector. GPW splits its Real Estate sector
# into "real estate rent" (landlords: GTC, CPI Europe, Echo, PHN) and "real
# estate sales" (developers: Dom Development, Atal, Murapol, Develia). Only the
# landlords carry the IAS 40 trap Germany excludes property for - their
# buildings are revalued through the income statement every year, so P/E is
# the appraiser's opinion. A developer's flats are inventory at cost; its P/E
# is an earnings multiple, so developers STAY, which Germany could not do
# because Deutsche Börse does not separate the two.
LANDLORD_SUBSECTORS = ("real estate rent",)

# The three size indices. A company in one of them is, in GPW's own judgement,
# a company whose market is here - so a foreign-domiciled member (Allegro,
# Pepco, Zabka, AmRest, Asbis) stays, and a foreign issuer outside all three
# is a secondary line (Banco Santander, UniCredit, CEZ, MOL, Krka) whose price
# is set in Madrid, Milan, Prague, Budapest or Ljubljana. All of those ARE in
# the broad WIG, so WIG membership would not separate them.
SIZE_INDICES = ("WIG20", "mWIG40", "sWIG80")

# ---------------------------------------------------------------------------
# Ownership flags - flags, never gates
# ---------------------------------------------------------------------------
# Read from GPW's own register: every company factsheet has a shareholders tab
# listing holders of 5% or more, by name, shares and votes. Germany and Japan
# had to list "no free shareholder data" as a known gap; Warsaw publishes it.
#
# State control: the Treasury directly, a state fund or agency, or another
# state-controlled listed company, together holding at least STATE_MIN_VOTES.
# The "SOE discount" is real and has causes - windfall levies, dividend policy
# set in the budget, investment chosen by ministers. A state-tagged name that
# screens cheap may be priced for exactly that. 25% because that is roughly
# where Warsaw's state stakes sit (PKO 31%, KGHM 32%, Tauron 30%) and above it
# the Treasury in practice appoints the board; no non-state investor in a
# Polish large cap holds that much without controlling it either.
STATE_MIN_VOTES = 25.0
# Holder names that ARE the state. Matched on upper-cased, accent-stripped
# names. "PANSTWOW" catches Polskie Koleje Państwowe and other state-named
# entities; the Treasury itself is "Skarb Państwa".
STATE_HOLDER_TOKENS = ("SKARB PANSTWA", "PANSTWOW", "BANK GOSPODARSTWA KRAJOWEGO",
                       "POLSKI FUNDUSZ ROZWOJU", "NARODOWY FUNDUSZ OCHRONY",
                       "AGENCJA ROZWOJU PRZEMYSLU")
# Whole names (legal form removed) that are the state but carry no state word.
STATE_HOLDER_STEMS = ("PKP",)
# Portfolio investors - pension funds (PTE / OFE), investment-fund managers
# (TFI) and index managers. Never a controller, and never a link in a state
# chain: "PTE PZU" manages pension money, it is not PZU's own stake.
PORTFOLIO_HOLDER_TOKENS = ("EMERYTALN", "OFE", "PTE", "TFI", "TOWARZYSTWO FUNDUSZY",
                           "BLACKROCK", "VANGUARD", "NORGES", "INVESCO", "FIDELITY")
# A single holder, or a declared concert party ("Porozumienie"), with a
# majority of votes: the company is controlled, and its minorities own a
# slice of something whose capital, dividend and strategy are set elsewhere.
# This covers Japan's 親子上場 problem - most of the Polish banking sector is a
# listed subsidiary of a foreign group (ING, Commerzbank, BCP, Citi, BNP) -
# and founders who never gave up control (Dino, LPP via its foundation).
CONTROL_MIN_VOTES = 50.0
# Legal-form words removed before a holder is matched to a listed company.
LEGAL_FORM_WORDS = {"SPOLKA", "AKCYJNA", "SA", "S", "A", "SE", "NV", "N", "V", "AG",
                    "PLC", "LTD", "LIMITED", "SP", "Z", "O", "OO", "BV", "B", "GMBH",
                    "INC", "CORP", "W", "RESTRUKTURYZACJI", "LIKWIDACJI"}


def plain(s: str) -> str:
    """Upper-case, accents and ł stripped, punctuation to spaces."""
    import unicodedata
    s = str(s or "").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s.upper()).split())


def name_stem(s: str) -> str:
    """A company or holder name without its legal form: 'POWSZECHNY ZAKLAD
    UBEZPIECZEN' for both 'POWSZECHNY ZAKŁAD UBEZPIECZEŃ SPÓŁKA AKCYJNA' and
    'Powszechny Zakład Ubezpieczeń S.A.'."""
    return " ".join(t for t in plain(s).split() if t not in LEGAL_FORM_WORDS)


def is_state_holder(name: str) -> bool:
    p = plain(name)
    return any(t in p for t in STATE_HOLDER_TOKENS) or name_stem(name) in STATE_HOLDER_STEMS


def is_portfolio_holder(name: str) -> bool:
    toks = set(plain(name).split())
    p = plain(name)
    return any((t in toks) if " " not in t and len(t) <= 4 else (t in p)
               for t in PORTFOLIO_HOLDER_TOKENS)


@dataclass
class ScreenConfig:
    # --- Size / liquidity, specified in USD then converted at live FX ---
    min_market_cap_usd: float = 600_000_000
    # Measured, not inherited - see CLAUDE.md "The liquidity gate". At the
    # cross-market USD 4m Warsaw is WIG20 plus a handful, and no GPW sector
    # keeps five peers.
    min_adv_usd: float = 1_000_000
    adv_lookback_days: int = 60                 # trading days

    # --- Valuation test ---
    discount_threshold: float = 0.20
    metrics: tuple[str, ...] = VALUATION_METRICS
    min_metrics_passing: int = 2
    min_valid_metrics: int = 2

    # --- Quality floor ---
    min_roe_pct: float = 5.0
    roe_good_pct: float = 10.0

    # --- Absolute value screen ---
    abs_max_pbr: float = 1.0
    abs_max_ev_ebitda: float = 8.0
    abs_require_roe: bool = True
    abs_financials_pbr_only: bool = True
    abs_require_pbr_vs_roe: bool = True
    abs_cost_of_equity_pct: float = 10.0
    abs_min_div_yield: float = 2.0

    # --- Own-history screen ---
    # Same parameters as every other market. Yahoo holds four filed years for
    # Polish companies, as for German and UK ones.
    hist_min_discount: float = 0.30
    hist_min_metrics: int = 2       # of P/E, P/B, EV/EBITDA; financials have no EV/EBITDA
    hist_min_years: int = 3
    hist_require_roe: bool = True

    # --- Peer groups ---
    # industry = GPW sector (e.g. Banks), sector = GPW macro-sector (Finance).
    peer_keys: tuple[str, ...] = ("industry",)
    fallback_peer_keys: tuple[str, ...] = ("sector",)
    min_peers: int = 5
    winsor_pct: float = 0.05

    # --- Poland-specific universe hygiene ---
    exclude_investment_companies: bool = True
    exclude_reits: bool = True
    exclude_property: bool = True           # landlords only - see LANDLORD_SUBSECTORS
    flag_holdcos: bool = True
    exclude_holdcos: bool = False           # flagged by default, not dropped
    exclude_state: bool = False             # flagged by default, not dropped

    exclude_sectors: tuple[str, ...] = ()

    # --- Fetching ---
    # Yahoo's .info is the call that throttles, globally and silently - the
    # UK build's measured setting.
    max_workers: int = 2
    request_delay: float = 0.25
    cache_dir: str = ".pl_cache"
    cache_ttl_hours: int = 20


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
REIT_TOKENS = ("REIT",)
HOLDCO_TOKENS = ("HOLDING", "HLDG")


# GPW files Real Estate (140) under its Finance macro-sector. For the peer
# fallback that is wrong: a developer short of sector peers would be
# benchmarked against banks and insurers - Atal "passed" the first run that
# way, 31% cheaper than PKO and PZU. Real estate is its own macro bucket here,
# which with Warsaw's handful of large developers means no fallback at all:
# unscored, rather than scored against the wrong thing.
MACRO_OVERRIDES = {"140": "Real Estate"}


def sector_names(code: str, polish: str = "") -> tuple[str, str]:
    """(industry, sector) in English for a GPW sector code."""
    c = str(code or "").strip()
    industry = SECTORS.get(c) or str(polish or "").strip()
    sector = (MACRO_OVERRIDES.get(c) or MACRO_SECTORS.get(c[:1], "")) if c else ""
    return industry, sector


def is_financial(sector: str, industry: str = "") -> bool:
    """Same signature as Germany's (sector, industry) so the dashboard calls it
    the same way. GPW's macro-sector "Finance" includes real estate, so the
    test is on the sector code's own name, not the macro-sector."""
    return str(industry).strip() in FINANCIAL_INDUSTRIES


def is_investment_company(industry: str) -> bool:
    return str(industry).strip() in INVESTMENT_INDUSTRIES


def is_landlord(industry: str, subsector: str) -> bool:
    return (str(industry).strip() == "Real Estate"
            and str(subsector).strip().lower() in LANDLORD_SUBSECTORS)


def is_reit(name: str) -> bool:
    n = str(name).upper()
    return any(t in n for t in REIT_TOKENS)


def is_holdco(name: str) -> bool:
    n = str(name).upper()
    return any(t in n for t in HOLDCO_TOKENS)


def size_tier(indices: str) -> str:
    """The largest GPW size index a company sits in, or ''."""
    members = {t.strip() for t in str(indices or "").split(",")}
    return next((i for i in SIZE_INDICES if i in members), "")
