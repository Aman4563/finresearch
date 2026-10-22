"use client";

import { ClipboardCheck } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Fig } from "@/components/fno/fit";
import { pnlTone, rs } from "@/components/fno/paper-trade";
import type { FnoReview } from "@/components/fno/review-types";
import { Card, ErrorNote, PageHeader, Skeleton, Stat, cx, inputClass } from "@/components/ui";
import { api } from "@/lib/api";

const pct = (x: number | null, d = 0) => (x == null ? "unknown" : `${(x * 100).toFixed(d)}%`);
const money = (x: number | null) => (x == null ? "unknown" : rs(x));
const CLASS: Record<string, string> = {
  planned_target: "At the target", planned_stop: "At the stop", planned_time: "At the planned time / expiry",
  planned_risk: "At a risk rule (delta, event)", late: "Late (after a rule fired)", discretionary: "Discretionary", no_plan: "No plan (left out)",
};

export default function FnoReviewPage() {
  const [month, setMonth] = useState("");
  const [v, setV] = useState<FnoReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setV(await api<FnoReview>(`/api/fno/review${month ? `?month=${month}` : ""}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [month]);
  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);
  const ad = v?.adherence, sf = v?.shortfall, mx = v?.mix, ex = v?.explain.summary;

  return (
    <div className="space-y-6">
      <PageHeader icon={<ClipboardCheck className="size-5" />} eyebrow="Research" title="F&O monthly review"
        description="How the month's trades were carried out against their plan: shortfall against the card, exits against the exit rules, the exposure mix and the daily P&L explained by greeks. Process control, not a validated model. Local only; no orders are ever placed." />
      <label className="flex items-center gap-2 text-sm">Month
        <input type="month" aria-label="Month" className={cx(inputClass, "w-44 py-1")} value={month || v?.month || ""} onChange={(e) => setMonth(e.target.value)} />
      </label>
      <ErrorNote error={error} onRetry={load} />
      {!v ? (!error && <Skeleton className="h-40 rounded-xl" />) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <Stat label="Plan adherence" display={<Fig>{pct(ad!.rate)}</Fig>} hint={`${ad!.closed} closed paper trades`}
              help={`Exits at the planned target, stop, time or risk rule ÷ trades with a plan. A square-off more than ${ad!.window_hours} h after a rule fired is late.`} />
            <Stat label="Loss beyond the stop" display={<Fig className="text-loss">{money(ad!.mean_beyond_stop)}</Fig>} hint={`${ad!.beyond_stop_count} of ${ad!.with_stop} trades with a stop`} />
            <Stat label="Shortfall per trade" display={<Fig className={pnlTone(sf!.mean_total == null ? null : -sf!.mean_total)}>{money(sf!.mean_total)}</Fig>}
              hint={`known for ${sf!.known} of ${sf!.trades.length} entries${sf!.mean_pct == null ? "" : ` · ${sf!.mean_pct.toFixed(2)}% of premium`}`}
              help="Σ side × (fill − the card's mid) × qty + entry costs. Positive is a cost. Unknown when the trade was not taken from a card with live bid/ask mids." />
            <Stat label="Premium sold" display={<Fig>{pct(mx!.premium_sold_share)}</Fig>} hint={`${mx!.trades} trades opened`} />
            <Stat label="Same-day expiry" display={<Fig>{pct(mx!.same_day_expiry_share)}</Fig>} hint={`held ${pct(mx!.mean_hold_share)} of days to expiry`} />
            <Stat label="P&L explained" display={<Fig>{pct(ex!.share_within)}</Fig>}
              hint={`${ex!.within} of ${ex!.decided} decided position-days · target ${pct(ex!.target_share)}`}
              help="A position-day is explained when |residual| ≤ max(10% of the day's P&L, 0.5% of the position's max loss)." />
          </div>
          <Card title="Exits" subtitle="Closed paper trades this month, by how they were closed">
            {ad!.closed ? <ul className="space-y-1 text-sm">{Object.entries(ad!.counts).map(([k, n]) => <li key={k}><span className="num">{n}</span> · {CLASS[k] ?? k}</li>)}</ul>
              : <p className="text-sm text-muted">No paper trade closed this month.</p>}
          </Card>
          <Card title="Shortfall by trade" subtitle="Paper entries this month against the card they were taken from">
            {sf!.trades.length ? <ul className="space-y-1 text-sm">{sf!.trades.map((t) => <li key={t.paper_id}>Paper #{t.paper_id}: {t.total == null ? `unknown (${t.why})` : <Fig className={pnlTone(-t.total)}>{rs(t.total)}{t.pct == null ? "" : ` · ${t.pct.toFixed(2)}%`}</Fig>}</li>)}</ul>
              : <p className="text-sm text-muted">No paper trade opened this month.</p>}
          </Card>
          <Card title="Daily P&L explain" subtitle={`${ex!.position_days} position-days · ${ex!.unknown} unknown`}>
            {v.explain.days.length ? (
              <div className="overflow-x-auto"><table className="w-full text-sm">
                <thead><tr className="text-left text-muted"><th>Day</th><th>Position</th><th>Actual</th><th>Δ</th><th>Γ</th><th>Vega</th><th>Θ</th><th>Residual</th><th>Within</th></tr></thead>
                <tbody>{v.explain.days.map((d) => (
                  <tr key={`${d.position}:${d.day}`}><td>{d.day}</td><td>{d.position}</td><td className="num">{money(d.actual)}</td>
                    {d.greeks ? <><td className="num">{rs(d.greeks.delta)}</td><td className="num">{rs(d.greeks.gamma)}</td><td className="num">{rs(d.greeks.vega)}</td><td className="num">{rs(d.greeks.theta)}</td><td className="num">{rs(d.greeks.residual)}</td></>
                      : <td colSpan={5} className="text-muted">unknown ({d.why})</td>}
                    <td>{d.within == null ? "unknown" : d.within ? "yes" : "no"}</td></tr>))}</tbody>
              </table></div>
            ) : <p className="text-sm text-muted">No marks yet: the monitor marks open legs each night after NSE&apos;s bhavcopy.</p>}
          </Card>
          <p className="text-xs text-muted">Disposition effect and loss-chasing: {v.deferred.status === "deferred" ? `deferred until ${v.deferred.starts_at} closed trades (${v.deferred.closed_trades} so far)` : "due: enough closed trades"}.</p>
          <ul className="list-disc space-y-1 pl-5 text-[11px] text-muted">{v.notes.map((n) => <li key={n}>{n}</li>)}</ul>
        </>
      )}
      <p className="text-xs text-muted"><Link className="text-brand hover:underline" href="/fno/paper">Paper book →</Link></p>
    </div>
  );
}
