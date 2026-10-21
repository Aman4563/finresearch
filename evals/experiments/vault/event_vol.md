# Event volatility on the vault (studies `event_vol_fairness`, `event_skip_variant`; P3.8)

Part of the vault pack (PREREG.md, #364). Needs P1.5's point-in-time event calendar for 2008-2021 before the read.

## Events

Tier-1 types: Union Budget, general-election results (2009, 2014, 2019), RBI MPC decisions (and RBI policy
announcements before the MPC existed, Oct-2016, as their own type). FOMC, CPI and GDP are context, not tested.

## 1. `event_vol_fairness` (one read)

- Implied event variance J_e^2 from the expiries spanning the event (business time): two expiries both after the
  event: J^2 = (sigma1^2 - sigma2^2) / (1/T1 - 1/T2); one before, one after: J^2 = tau2 (sigma2^2 - sigma1^2); with 3
  or more expiries, least squares with J_e^2 >= 0. J^2 used only when positive; otherwise the event is "not
  identified" and excluded (reported with its count).
- r_e = the log return from the last close before the announcement time to the first close after it.
- **Test (1), fairness in variance units:** mean of z_e^2 = r_e^2 / J_e^2 equals 1; 95 % bootstrap CI over events
  (5,000 draws, seed 391), Holm across types. Plus binomial coverage of the +/-1 sigma (68.3 %) and +/-2 sigma (95.4 %)
  bands. Mean |r_e| / J_e is reported, not tested.
- **Test (2):** event days vs matched non-event days (same weekday, nearest 4 non-event sessions of the same expiry
  cycle), Mann-Whitney on |r|, Holm across types. The event exit rule is "material" only for types whose mean |move|
  is >= 1.5x the non-event mean.
- Types with fewer than 20 identified events are reported descriptively (Budget, elections), never flagged.

## 2. `event_skip_variant` (conditional, one read; VARIANTS_TRIED +1 when added to a specs file)

Runs only if `vrp.md`'s prerequisite holds for `nifty_ic_w` (T22), else never.

- Base: `nifty_ic_w` exactly as registered. Variant: do not enter when a tier-1 event's announcement falls between
  the entry session and the expiry session inclusive.
- Expected share of entries skipped, stated up front: about 11 % from RBI MPC alone (a 7-day window spans an MPC about
  11 % of the time) plus Budget and election weeks: about 12-15 %.
- Judged by the paired per-expiry P&L difference (variant - original), expiry-cluster bootstrap (5,000 draws, seed
  391), pass iff t >= 2 and the 95 % CI is above 0, on the vault weeklies (Feb-2019 [verify] to 31-Dec-2021) AND on
  forward data. 2022-26 is in-sample (the 4-Jun-2024 loss motivated it) and reported only.
- Power, stated up front: about 150 vault weekly expiries with about 15-25 skipped entries: low; a pass is unlikely
  even if the effect is real.
