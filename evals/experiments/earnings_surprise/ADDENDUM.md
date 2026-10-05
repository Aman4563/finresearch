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
