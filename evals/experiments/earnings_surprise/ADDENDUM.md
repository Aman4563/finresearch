# Addendum 1: unreliable broadcast dates (5-Oct-2026, before any outcome was computed)

Found while checking the harvest on one stock (INFY), before any SUE, return or test statistic was computed for any
stock. Recorded here because it changes how t0 is found; PREREG.md is unchanged.

**What was found.** In NSE's older Financial Results index some rows carry a broadcast date long after the
results came out: every INFY row for the Mar-2019 quarter says 4-Sep-2019 (157 days after the quarter end), while the
other 31 INFY quarters were broadcast 10-24 days after the quarter end. These look like later re-uploads. NSE's board
meetings API returns only the last ~20 meetings, so it cannot supply the true date for old quarters.

**Rule added.** A quarter's broadcast is accepted as its announcement only when it falls within the legal deadline
for that quarter plus 7 days of slack (for a late filer):
- SEBI LODR Regulation 33(3)(a): quarterly results within 45 days of the quarter end; 33(3)(d): annual audited
  results (the March quarter) within 60 days.
- COVID relaxations [U: from memory, circulars not re-read in this session]: the Mar-2020 quarter by 31-Jul-2020,
  the Jun-2020 quarter by 15-Sep-2020, the Mar-2021 quarter by 30-Jun-2021.
A quarter outside its deadline is not an event (no t0), and the count is reported. Its EPS still enters later
quarters' SUE history, as known from the deadline date (the latest legal date).

**Not detectable.** A re-upload that still falls inside the deadline would move t0 late. That makes the [0,+1]
reaction miss the news and starts the drift window late; it cannot create look-ahead in SUE (SUE uses only filings
dated at or before its own). It is a limitation of the source and is listed in RESULTS.md.

# Addendum 2: the benchmark series (5-Oct-2026, before any outcome was computed)

**What was found.** NSE's `indicesHistory` answer for the NIFTY 50 price index, harvested from Jun-2021, has 1,025
days where INFY alone has 1,326 sessions: 301 sessions are missing, in runs of up to four months (e.g. 9-Apr-2025 to
19-Aug-2025, 13-Oct-2025 to 14-Jan-2026). The PIT Nifty 50 experiment met the same holes and used NIFTYBEES instead.
Dropping every event whose window touches a hole (as PREREG.md allows) would discard roughly half of the sample.

**Change.** The benchmark is the NIFTYBEES ETF's NSE EQ-series closes (Nippon India ETF Nifty 50 BeES, which tracks
the NIFTY 50), harvested by the same `history` walk as the stocks and split-adjusted the same way. Every other rule is
unchanged. Caveat: NIFTYBEES's price tracks the index's total return less its expense ratio if the scheme pays no
distribution in the window, while the stocks' returns exclude dividends; that biases every abnormal return down by
roughly the index dividend yield (≈ 1.2-1.4 % a year, ≈ 0.3 pp over 60 sessions). It cancels in D10 - D1 and makes
the long-only tradable test slightly conservative. The NIFTY 50 index series is kept on disk but not used.
