"""Checks on RBI's policy rates read from RBI's press-release RSS feed (#492, refseries.policy_obs), dataset
`policy_rate`, run in every data-quality pass (dq.run). None is critical: no page is marked 'data unverified' by
them, and the risk-free answer carries its own "possibly stale" flag (fincalc.policy_feed.stale).

* mpc_decision_read: a scheduled MPC decision day (the event calendar's rbi_mpc days after the typed table's review,
  or the table's own next meeting while the calendar does not cover it: fincalc.policy_feed.schedule) more than
  GRACE past with no decision read from the feed with its repo rate dated from that day to the next scheduled one:
  warn, and answers from that day stay "possibly stale" until a decision is read. Unknown when every known meeting
  was read but the known schedule itself ended more than GRACE ago.
* table_agreement: an observation of a day the typed table covers (fincalc.policy_rates.covered_through(), normally
  REVIEWED) that contradicts it: warn; the table is kept.
* feed_conflicts: another item giving other rates for a decision already read: warn; the first one seen is used.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from finresearch.dq.base import Check
from finresearch.fincalc import policy_feed as F
from finresearch.fincalc.dates import to_ist

DATASET = "policy_rate"
GRACE = timedelta(days=2)  # the owner's rule (#492): a scheduled decision not read within 2 days is a warning


def _mpc(observed: F.Observed, merged: F.Merged, today: date) -> Check:
    days, through = F.schedule(observed)
    read = [r.effective for r in merged.rows if r.seen is not None]
    # a meeting is read when a decision dated from its day to the next scheduled one was (one that moved a day or two
    # later is still that meeting's); a later meeting's decision does not stand for it
    late = [d for d, nxt in zip(days, [*days[1:], None], strict=True)
            if today > d + GRACE and not any(d <= r and (nxt is None or r < nxt) for r in read)]  # fmt: skip
    if late:
        blind = {o.decided: o for o in observed.observations if o.repo is None}
        parts = [f"{d:%d-%b-%Y}" + (f" (read from {blind[d].url}, seen {F.when(blind[d].seen)}, but no repo rate "
                                    "could be read in it)" if d in blind else "") for d in late]  # fmt: skip
        return Check(DATASET, "mpc_decision_read", "warn", len(late), 0,
                     f"no decision read from RBI's RSS feed within {GRACE.days} days of the scheduled MPC decision of "
                     f"{', '.join(parts)}: rates from then are possibly stale", critical=False)  # fmt: skip
    if today > through + GRACE:
        return Check(DATASET, "mpc_decision_read", "unknown", None, 0,
                     f"no MPC schedule is known after {through:%d-%b-%Y} (the event calendar lists no later meeting): "
                     "a decision since then cannot be checked", critical=False)  # fmt: skip
    return Check(DATASET, "mpc_decision_read", "ok", 0, 0, critical=False)


def checks(s: Session, now: datetime) -> list[Check]:
    from finresearch.refseries import policy_obs

    observed = policy_obs.observed(s)
    merged = F.merge(observed.observations)
    dis, con = merged.disagreements, [n for _, n in merged.conflicts]
    return [_mpc(observed, merged, to_ist(now).date()),
            Check(DATASET, "table_agreement", "warn" if dis else "ok", len(dis), 0, "; ".join(dis) or None,
                  critical=False),
            Check(DATASET, "feed_conflicts", "warn" if con else "ok", len(con), 0, "; ".join(con) or None,
                  critical=False)]  # fmt: skip
