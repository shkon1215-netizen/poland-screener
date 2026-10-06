"""Re-derive the Poland dashboard from the Korea build's current dashboard.py.

    python sync_dashboard_from_korea.py ../Korea/dashboard.py

The Poland dashboard is not a fork that drifts: it is Korea's dashboard.py
with a fixed list of Polish substitutions applied. Most are Germany's (P/E and
P/B labels, same-basis per_now/pbr_now for the own-history screen, the
reporting currency in growth tooltips, four filed years instead of five, the
undefined guard in cell(), one page rather than one per board); the rest are
Polish (GPW symbol, financials by GPW sector code, an Index filter in place of
the board filter, state / parent / currency-repair tags, a hide-state toggle,
Polish notes). When Korea gains a feature, run this.

Every substitution must match exactly once. Anything that no longer matches is
printed and the exit code is 1 - Korea's text moved, and that substitution
needs updating by hand rather than being silently skipped. Afterwards, grep
the result for Korean text, KOSPI, PER/PBR and "ticker" to catch anything new
Korea added that this list does not know about yet, and re-check that the
browser's verdict matches the Python funnel at default thresholds.
"""
import io
import sys

if len(sys.argv) != 2:
    sys.exit("usage: python sync_dashboard_from_korea.py <path to Korea dashboard.py>")
SRC = sys.argv[1]
P = "dashboard.py"
s = io.open(SRC, encoding="utf-8").read()


PAIRS = [
    # ---- module docstring and imports --------------------------------------
    ("Reads the CSV that main_kr.py writes", "Reads the CSV that main_pl.py writes"),
    ("re-run main_kr.py to refresh it in place.", "re-run main_pl.py to refresh it in place."),
    ("import numpy as np\nimport pandas as pd\n",
     "import numpy as np\nimport pandas as pd\n\n"
     "# Financials by GPW sector code, the same test the screen used - the page's\n"
     "# carve-out must agree with Python's or its verdict drifts.\n"
     "from config_pl import is_financial\n"),

    # GPW symbols include all-digit-led ones (11B); read them as text.
    ('dtype={"ticker": str})', 'dtype={"ticker": str, "symbol": str, "isin": str})'),

    # ---- funnel + table ----------------------------------------------------
    ('("listings", "Corporate lines", "ETFs, ETNs and funds already removed"),',
     '("listings", "Listed companies", "GPW Main Market"),'),
    ('("after_korea_filters", "After share-class hygiene", None),',
     '("after_polish_filters", "After universe hygiene", None),'),
    ('("cleared_size_liquidity", "Cleared size gate", None),',
     '("cleared_size_liquidity", "Cleared size + liquidity", None),'),
    ('("ticker", "Code", "l"), ("name", "Name", "l"), ("industry", "업종 industry", "l"),',
     '("symbol", "Code", "l"), ("name", "Name", "l"), ("industry", "Sector", "l"),'),
    ('("mcap_musd", "Cap $m", ""), ("trailing_pe", "PER", ""), ("price_to_book", "PBR", ""),',
     '("mcap_musd", "Cap $m", ""), ("trailing_pe", "P/E", ""), ("price_to_book", "P/B", ""),'),
    ('("hist_avg_disc", "vs own 5y", ""),', '("hist_avg_disc", "vs own history", ""),'),
    ('("abs_pbr_ok", "PBR below {abs_max_pbr:g}"),', '("abs_pbr_ok", "P/B below {abs_max_pbr:g}"),'),
    ('("abs_pbr_vs_roe_ok", "PBR below fair value (ROE ÷ {abs_cost_of_equity_pct:g}% CoE)"),',
     '("abs_pbr_vs_roe_ok", "P/B below fair value (ROE ÷ {abs_cost_of_equity_pct:g}% CoE)"),'),

    # ---- row payload -------------------------------------------------------
    ('            "ticker": str(r.get("ticker", "")),\n',
     '            # The GPW symbol, not the Yahoo one: PKO is what a Polish reader\n'
     '            # looks up, PKO.WA is a vendor detail. This key must stay in step\n'
     '            # with TABLE_COLS - see the guard in cell().\n'
     '            "symbol": str(r.get("symbol", "") or r.get("ticker", "")),\n'),
    # One board on GPW; the filter that slot carries is the size index.
    ('            "board": str(r.get("board", "")),',
     '            # The size index (WIG20 / mWIG40 / sWIG80), shown in the filter\n'
     '            # Korea uses for its boards. Everything here is Main Market.\n'
     '            "board": str(r.get("tier", "") or "") if pd.notna(r.get("tier")) '
     'and str(r.get("tier")) else "no index",'),
    ('            "fin": bool(str(r.get("sector", "") or "").lower().find("financial") >= 0),',
     '            "fin": bool(is_financial(r.get("sector", "") or "", r.get("industry", "") or "")),\n'
     '            # Ownership flags read from GPW\'s shareholder register - see\n'
     '            # pl_filters.classify_ownership. Never gates; the holders travel\n'
     '            # so the tag can say who.\n'
     '            "state": "" if pd.isna(r.get("state_holder")) else str(r.get("state_holder") or ""),\n'
     '            "ctrl": "" if pd.isna(r.get("ctrl_holder")) or not r.get("ctrl_holder") else\n'
     '                    f"{r.get(\'ctrl_holder\')} {float(r.get(\'ctrl_pct\')):.1f}%",\n'
     '            # What the currency repair did to this row, if anything - see\n'
     '            # pl_filters.repair_foreign_reporters.\n'
     '            "fxn": "" if pd.isna(r.get("fx_note")) else str(r.get("fx_note") or ""),\n'
     '            # Trailing profit against a filed loss - see pl_filters.flag_ttm_vs_filed.\n'
     '            "ttmx": bool(r.get("ttm_vs_filed_loss") == True),  # noqa: E712'),
    ('            "evx_now": _f(r.get("evx_now"), 6),\n',
     '            "evx_now": _f(r.get("evx_now"), 6),\n'
     '            # Today\'s P/E and P/B on the SAME basis as the history (market\n'
     '            # value over the latest filing), which is not the basis of the\n'
     '            # trailing_pe/price_to_book columns - see add_history_now.\n'
     '            # These are what the history screen thresholds against, so\n'
     '            # they carry full precision like the multiples above.\n'
     '            "per_now": _f(r.get("per_now"), 6),\n'
     '            "pbr_now": _f(r.get("pbr_now"), 6),\n'
     '            # The reporting currency the 3-year figures are in. Not the quote\n'
     '            # currency: Pepco trades in zloty and reports in euros.\n'
     '            "fin_ccy": str(r.get("fin_ccy", "") or ""),\n'),
    ('            # Three-year history, oldest first, in 억원. The yearly values',
     '            # Three-year history, oldest first, in millions of the REPORTING\n'
     '            # currency (fin_ccy). The yearly values'),
    ('            # Own five-year history. The medians and today\'s values let the',
     '            # Own filed history. The medians and today\'s values let the'),

    # ---- drop labels -------------------------------------------------------
    ('''    for key, label in [("dropped_preferred", "우선주 preferred"),
                       ("dropped_reit", "리츠 REIT"),
                       ("dropped_spac", "스팩 SPAC")]:''',
     '''    for key, label in [("dropped_investment_co", "investment vehicles"),
                       ("dropped_property", "landlords"),
                       ("dropped_reit", "REITs"),
                       ("dropped_foreign_secondary", "foreign secondary lines"),
                       ("dropped_state", "state-controlled")]:'''),

    # ---- boards: one page ----------------------------------------------------
    ('''BOARD_FILES = {"KOSPI": "kr_dashboard.html",
               "KOSDAQ": "kq_dashboard.html",
               "BOTH": "krkq_dashboard.html"}''',
     '''# Poland runs as ONE screen of GPW's Main Market. NewConnect, GPW's
# alternative market, is not read: nothing on it clears the USD 600m floor.
BOARD_FILES = {"MAIN": "pl_dashboard.html"}'''),
    ('''# its own. KOSPI keeps the original name - renaming a published artifact makes
# it unrecognisable to anyone who bookmarked it.
BOARD_TITLES = {"KOSDAQ": "KOSDAQ Discount Screen"}''',
     '''# its own. One page here, so nothing to retitle.
BOARD_TITLES = {}'''),

    # ---- FX field ----------------------------------------------------------
    ('"krw_per_usd": meta.get("krw_per_usd"),', '"usd_per_pln": meta.get("usd_per_pln"),\n'
     '            "roster_asof": meta.get("roster_asof", ""),'),

    # ---- titles ------------------------------------------------------------
    ('head.replace("<title>Korea Discount Screen</title>",',
     'head.replace("<title>Poland Discount Screen</title>",'),
    ('body.replace("<h1>Korea Discount Screen</h1>",', 'body.replace("<h1>Poland Discount Screen</h1>",'),
    ('_HEAD = """<title>Korea Discount Screen</title>', '_HEAD = """<title>Poland Discount Screen</title>'),
    ('<p class="eyebrow">KRX relative valuation</p>', '<p class="eyebrow">GPW relative valuation</p>'),
    ('<h1>Korea Discount Screen</h1>', '<h1>Poland Discount Screen</h1>'),

    # ---- fonts and line breaking -------------------------------------------
    # IBM Plex Sans covers Latin Extended-A, so ł, ż, ę render in the face.
    ('family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@300;400;500;600;700&display=swap',
     'family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap'),
    ('--sans:"IBM Plex Sans KR", system-ui, -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;',
     '--sans:"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;'),
    ('''/* Korean breaks at any character by default, so 현대지에프홀딩스 shatters across
   four lines in a narrow column. keep-all breaks at word boundaries instead. */''',
     '''/* Polish names break at spaces first and mid-word only when a single word
   is wider than its column - keep-all would push it out of the table. */'''),
    ("  word-break:keep-all}", "  word-break:normal;overflow-wrap:anywhere}"),

    # ---- commands / search / filters ---------------------------------------
    ('<code id="cmd">python main_kr.py</code>', '<code id="cmd">python main_pl.py</code>'),
    ('placeholder="name, code or 업종"', 'placeholder="name, code or sector"'),
    ('$("cmd").textContent = M.cmd || ("python main_kr.py --board "',
     '$("cmd").textContent = M.cmd || ("python main_pl.py --board "'),
    ('      <label for="brd">Board</label>', '      <label for="brd">Index</label>'),
    ('    <label class="toggle"><input type="checkbox" id="nohold"> Hide holdcos</label>',
     '    <label class="toggle"><input type="checkbox" id="nohold"> Hide holdcos</label>\n'
     '    <label class="toggle"><input type="checkbox" id="nostate"> Hide state-controlled</label>'),
    ('  const onlyPass = $("onlypass").checked, noHold = $("nohold").checked;',
     '  const onlyPass = $("onlypass").checked, noHold = $("nohold").checked;\n'
     '  const noState = $("nostate").checked;'),
    ('    if (noHold && r.holdco) return false;',
     '    if (noHold && r.holdco) return false;\n'
     '    if (noState && r.state) return false;'),
    ('["q","brd","scr","gmode","onlypass","nohold"].forEach(id =>',
     '["q","brd","scr","gmode","onlypass","nohold","nostate"].forEach(id =>'),

    # ---- history panel, selector and control labels ------------------------
    ('<h2>Cheap vs its own 5 years</h2>', '<h2>Cheap vs its own history</h2>'),
    ('<span class="hint">median of filed years</span>',
     '<span class="hint">median of 4 filed years</span>'),
    ('<option value="history">Cheap vs own 5y</option>',
     '<option value="history">Cheap vs own history</option>'),
    ('<div class="tg"><label for="t_hdisc">Below own 5y by at least %</label>',
     '<div class="tg"><label for="t_hdisc">Below own history by at least %</label>'),

    # ---- threshold controls ------------------------------------------------
    ('<div class="tg"><label for="t_pbr">PBR below</label>',
     '<div class="tg"><label for="t_pbr">P/B below</label>'),
    ('<div class="tg"><label for="t_per">PER below</label>',
     '<div class="tg"><label for="t_per">P/E below</label>'),
    ('id="t_fair"> Require PBR below fair value', 'id="t_fair"> Require P/B below fair value'),
    ('id="t_carve"> Financials qualify on PBR + ROE', 'id="t_carve"> Financials qualify on P/B + ROE'),
    ('tests.push(["PBR below " + T.pbr,', 'tests.push(["P/B below " + T.pbr,'),
    ('tests.push(["PER below " + T.per,', 'tests.push(["P/E below " + T.per,'),
    ('tests.push([`PBR below fair value (ROE ÷ ${T.coe}% CoE)`,',
     'tests.push([`P/B below fair value (ROE ÷ ${T.coe}% CoE)`,'),
    ('financials qualified on PBR and ROE ', 'financials qualified on P/B and ROE '),
    ('${hist.length} vs own 5y`},', '${hist.length} vs own history`},'),

    # ---- chips -------------------------------------------------------------
    ('["Source", M.source === "naver" ? "KIND + Naver" : "KRX"],',
     '["Source", "GPW + Yahoo"],\n  ["Roster as at", M.roster_asof || "?"],'),
    ('["USD/KRW", M.krw_per_usd],', '["PLN/USD", M.usd_per_pln],'),

    # ---- interpretation copy -----------------------------------------------
    ('''    <p><b>Cheap vs its own five years</b> compares today's PER, PBR and EV/EBITDA
    with the median of the company's last five filed years. It catches what the
    other two miss - a company that always traded at a premium and has just
    de-rated. Loss years drop out of the benchmark rather than dragging it, and
    fewer than three usable years means no benchmark at all. <b>One caution:</b>
    these are trailing multiples, so when earnings are surging the latest filing
    lags the price and a stock reads <i>expensive</i> against its history until
    the next filing catches up. A one-off gain does the opposite.</p>''',
     '''    <p><b>Cheap vs its own history</b> compares today's P/E, P/B and EV/EBITDA
    with the median of the company's last four filed years - four, not Korea's
    five, because that is all Yahoo holds for Polish companies. It catches what
    the other two miss: a company that always traded at a premium and has just
    de-rated. Loss years drop out of the benchmark rather than dragging it, and
    fewer than three usable years means no benchmark at all. For a bank, a
    2022-23 year of Swiss-franc mortgage provisions is such a loss year.</p>
    <p><b>Today's multiples in that column are not the ones in the P/E and P/B
    columns.</b> Each past year is that year-end market value over that year's
    filed accounts, so today is measured the same way: today's market value over
    the latest filed year, on Yahoo's normalized earnings. The P/E column uses
    the last twelve months instead; comparing across the two would report the
    gap between two definitions as a discount. <b>One caution:</b> when earnings
    are rising, the latest filing lags the price and a stock reads
    <i>expensive</i> against its history until the next filing catches up.</p>'''),
    ('''    own 5 years</b> asks whether it is cheap against itself. Korea needs all
    three:''',
     '''    own history</b> asks whether it is cheap against itself. Poland needs all
    three:'''),
    ('''how far below book it traded. Those names clear the absolute screen on PBR and
    ROE alone and are marked <span class="tag">pbr+roe</span>.</p>''',
     '''how far below book it traded. Those names clear the absolute screen on P/B and
    ROE alone and are marked <span class="tag">pbr+roe</span>.</p>'''),
    ('''<p><b>Low PBR with low ROE is not a discount.</b> It is a company not earning
    its cost of capital, priced accordingly. Much of what gets called the Korea
    Discount is this.''',
     '''<p><b>Low P/B with low ROE is not a discount.</b> It is a company not earning
    its cost of capital, priced accordingly. Warsaw's discount to Western
    Europe is partly this and partly the next paragraph.'''),
    ('''<p><b>업종 files holding companies under 기타 금융업.</b> That pools operating
    holdcos with bank holdcos in one peer group, and suppresses EV/EBITDA for
    both — enterprise value is meaningless for a bank, but not for an operating
    company. Holdcos are tagged so you can see which rows this touches.</p>''',
     '''<p><b>Who controls the company matters more in Warsaw than anywhere else
    this screen runs.</b> Ownership is read from GPW's own shareholder register.
    Where the state - the Treasury, a state fund, or a state-controlled listed
    company - holds 25% or more of the votes, the row is tagged
    <span class="tag">state</span>: PKO BP, PZU, Pekao, Orlen, PGE and KGHM among
    them. Those discounts have causes: windfall levies, dividends set by the
    budget, investment chosen by ministers. A single holder with a majority of
    the votes is tagged <span class="tag">ctrl</span>: most of the rest of the
    banking sector is a listed subsidiary of a foreign group (ING, mBank,
    Millennium, Citi Handlowy, BNP Paribas), and some founders never gave up
    control. Both are flags, not gates; hover a tag for the holder.</p>
    <p><b>Some companies report in euros or dollars.</b> Pepco and Asbis among
    the large ones quote in zloty and file in another currency, and Yahoo divides
    one by the other - a zloty price over a euro book reads 4.3 times too
    expensive. Those rows are repaired, each test checked per company rather
    than assumed, and tagged <span class="tag">fx</span>; hover it for what was
    repaired and what was refused.</p>
    <p><b>Excluded by default:</b> listed investment vehicles (GPW's own
    "investment activities" sector), which trade at a standing discount to NAV;
    landlords (GTC, CPI Europe), whose IAS 40 revaluations make their P/E an
    appraisal - but <i>not</i> developers (Dom Development, Atal, Murapol), whose
    flats are inventory and whose P/E is an earnings multiple; and foreign
    companies whose main market is elsewhere (Banco Santander, UniCredit, CEZ,
    MOL, Krka). Peer groups are GPW's sectors, falling back to its eight
    macro-sectors.</p>
    <p><b>A <span class="tag">ttm vs fy</span> tag means read the P/E twice.</b>
    The P/E and ROE columns are Yahoo's trailing twelve months, which sum the
    last four quarters one-offs and all. A tagged name was profitable on that
    basis but lost money in its last filed year. The screens still run on the
    trailing figure, as in every other market; the tag says where it
    misleads.</p>'''),

    # ---- JS: history reads same-basis values -------------------------------
    ('  const cur = {per: r.trailing_pe, pbr: r.price_to_book, evx: r.evx_now};',
     '  const cur = {per: r.per_now, pbr: r.pbr_now, evx: r.evx_now};'),
    ('  const now = {per: r.trailing_pe, pbr: r.price_to_book, evx: r.evx_now};',
     '  const now = {per: r.per_now, pbr: r.pbr_now, evx: r.evx_now};'),
    ('''  const passTag = r.ev.hist ? ' <span class="tag">5y low</span>' : "";''',
     '''  const passTag = r.ev.hist ? ' <span class="tag">hist low</span>' : "";'''),
    # Yahoo has four filed years for Polish names, so the fifth history slot is
    # null with no year label. Show only the years that exist.
    ('    const ser = (r.h_ser && r.h_ser[k] || []).map((v, i) =>',
     '    const ser = (r.h_ser && r.h_ser[k] || []).slice(0, yrs.length).map((v, i) =>'),
    ('  // Own five-year history. Mirrors korea_filters.apply_history_screen: the',
     '  // Own filed history. Mirrors pl_filters.apply_history_screen: the'),
    ('/* Today\'s value against the five-year median, per metric.',
     '/* Today\'s value against the filed-year median, per metric.'),
    ('/* Average discount to the company\'s own five-year median, across every metric',
     '/* Average discount to the company\'s own filed-year median, across every metric'),

    # ---- JS: growth tooltip names the reporting currency -------------------
    ('  const tip = GROWTH_LABEL[key] + " (억원)\\\\n"',
     '  const tip = GROWTH_LABEL[key] + " (" + (r.fin_ccy || "reporting ccy") + " m)\\\\n"'),

    # ---- JS: symbol, tags, and the guard against one bad key ---------------
    ('''function cell(r, k) {
  const v = r[k];
  if (k === "ticker") return `<span style="color:var(--ink-3)">${esc(v)}</span>`;''',
     '''function cell(r, k) {
  const v = r[k];
  /* A key present in TABLE_COLS but absent from the row payload arrives as
     undefined, which slipped past the null guard and hit .toFixed() in the UK
     build - one renamed column left the whole table empty. Missing reads as
     missing. Computed columns read other fields and are exempt. */
  if (v === undefined && !(k in GROWTH) && k !== "hist_avg_disc"
      && k !== "screen" && k !== "name") return '<span class="na">—</span>';
  if (k === "symbol") return `<span style="color:var(--ink-3)">${esc(v)}</span>`;'''),
    ('''    const tags = (r.holdco ? '<span class="tag">holdco</span>' : "")''',
     '''    const tags = (r.state ? `<span class="tag" title="State-controlled: ${esc(r.state)}">state</span>` : "")
               + (r.ctrl ? `<span class="tag" title="Majority of votes: ${esc(r.ctrl)}">ctrl</span>` : "")
               + (r.fxn ? `<span class="tag" title="Reports in ${esc(r.fin_ccy)}, quotes in PLN: ${esc(r.fxn)}">fx</span>` : "")
               + (r.ttmx ? '<span class="tag" title="Profitable over the trailing twelve months, loss-making in the last filed year: the trailing P/E and ROE likely carry one-offs">ttm vs fy</span>' : "")
               + (r.holdco ? '<span class="tag">holdco</span>' : "")'''),
    ('r.name.toLowerCase().includes(q) || r.ticker.includes(q)',
     'r.name.toLowerCase().includes(q) || (r.symbol || "").toLowerCase().includes(q)'),

    # ---- storage key and comments ------------------------------------------
    ('const STORE = "kr-thresholds-" + (M.board || "x");',
     'const STORE = "pl-thresholds-" + (M.board || "x");'),
    ('/* One row against the current thresholds. Mirrors korea_filters.apply_roe_gate',
     '/* One row against the current thresholds. Mirrors pl_filters.apply_roe_gate'),
    ('''   Only serve.py can actually re-run the screen: it shells out to main_kr.py,
   which scrapes KIND and Naver. A page opened straight off disk, or published''',
     '''   Only serve.py can actually re-run the screen: it shells out to main_pl.py,
   which reads GPW's company search and Yahoo. A page opened straight off
   disk, or published'''),
]

# Labels that appear more than once, replaced everywhere.
ALL = [
    ('const lab = {per: "PER", pbr: "PBR", evx: "EV/EBITDA"};',
     'const lab = {per: "P/E", pbr: "P/B", evx: "EV/EBITDA"};'),
]

missed = []
for old, new in PAIRS:
    if old in s:
        s = s.replace(old, new, 1)
    else:
        missed.append(old[:72])
for old, new in ALL:
    n = s.count(old)
    if not n:
        missed.append(old[:72])
    s = s.replace(old, new)

io.open(P, "w", encoding="utf-8").write(s)
print(f"applied {len(PAIRS) + len(ALL) - len(missed)}/{len(PAIRS) + len(ALL)}")
for m in missed:
    print("  MISS:", m)
sys.exit(1 if missed else 0)
