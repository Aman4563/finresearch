"use client";

import { Activity, Hourglass, Repeat, Scale, Wallet } from "lucide-react";
import { useState } from "react";

import { Callout, Card, ErrorNote, Field, Skeleton, Stat, Table, cx, inputClass } from "@/components/ui";
import { useApi } from "@/lib/api";

import type { Behaviour, Disposition } from "./types";

const pct = (x: number | null | undefined, d = 1) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const inr = (x: number | null | undefined) => (x == null ? "—" : `₹${Math.round(x).toLocaleString("en-IN")}`);
const days = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x)} d`);

/** The current Indian financial year, named by its end year as the API does (2027 = FY 2026-27). */
function currentFy(): number {
  const d = new Date();
  return d.getMonth() >= 3 ? d.getFullYear() + 1 : d.getFullYear();
}

function DispositionCard({ d, title }: { d: Disposition; title: string }) {
  return (
    <Card title={title} icon={<Scale className="size-4" />} subtitle={d.scope}
      help={<>On each day you sold, every stock you held is counted once: sold for a gain or a loss (realised), or kept at a gain or a loss (paper). PGR = realised gains ÷ (realised + paper gains); PLR the same for losses. PGR well above PLR means you sell winners and keep losers. {d.source}</>}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="PGR" value={d.pgr} format={(n) => pct(n)} tone="gain" hint={`${d.realised_gains} realised / ${d.paper_gains} paper gains`} />
        <Stat label="PLR" value={d.plr} format={(n) => pct(n)} tone="loss" hint={`${d.realised_losses} realised / ${d.paper_losses} paper losses`} />
        <Stat label="PGR − PLR" value={d.difference} format={(n) => `${(n * 100).toFixed(1)} pp`} tone="info"
          hint={d.t != null ? `t = ${d.t.toFixed(2)} (SE ${(d.se ?? 0).toFixed(3)})` : "no t-statistic"} />
        <Stat label="Sale days counted" value={d.sale_days} format={(n) => String(Math.round(n))}
          hint={d.single_stock_days ? `${d.single_stock_days} single-stock day(s) skipped` : undefined} />
      </div>
      <p className="mt-3 text-sm">{d.reading}</p>
      <p className="mt-1 text-[11px] text-muted">{d.method}{d.portfolio ? `; ${d.portfolio}` : ""}.</p>
      {d.days.length > 0 && (
        <details className="mt-3 text-xs">
          <summary className="cursor-pointer text-muted">Day by day ({d.days.length})</summary>
          <ul className="mt-2 space-y-1.5">
            {d.days.map((x) => (
              <li key={x.day}>
                <span className="num font-medium">{x.day}</span>
                {x.realised_gains.length > 0 && <span className="text-gain"> · sold up: {x.realised_gains.join(", ")}</span>}
                {x.realised_losses.length > 0 && <span className="text-loss"> · sold down: {x.realised_losses.join(", ")}</span>}
                {x.paper_gains.length > 0 && <span> · kept up: {x.paper_gains.join(", ")}</span>}
                {x.paper_losses.length > 0 && <span> · kept down: {x.paper_losses.join(", ")}</span>}
                {x.not_counted.length > 0 && <span className="text-muted"> · not counted: {x.not_counted.join(", ")}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}

/** The yearly behaviour report from your own trades: turnover, holding periods, disposition effect, cost of churn. */
export function BehaviourReport() {
  const [fy, setFy] = useState(currentFy());
  const { data, error, reload } = useApi<Behaviour>(`/api/portfolio/behaviour?fy=${fy}`);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <Field label="Financial year">
          <select className={cx(inputClass, "w-40")} value={fy} onChange={(e) => setFy(Number(e.target.value))}>
            {Array.from({ length: 6 }, (_, i) => currentFy() - i).map((y) => (
              <option key={y} value={y}>FY {y - 1}-{String(y % 100).padStart(2, "0")}</option>
            ))}
          </select>
        </Field>
        {data && <p className="max-w-md text-[11px] text-muted">{data.privacy} {data.disclaimer}</p>}
      </div>
      <ErrorNote error={error} onRetry={reload} />
      {!data && !error && <Skeleton className="h-64 rounded-xl" />}
      {data && (
        <>
          {data.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
          <div className="stagger grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Decisions" value={data.trades} format={(n) => String(Math.round(n))} icon={<Activity className="size-4" />}
              hint={`${data.trades_by_side.buy} buys · ${data.trades_by_side.sell} sells (one per instrument, side and day)`} />
            <Stat label="Turnover (a year)" value={data.turnover.annual} format={(n) => pct(n, 0)} icon={<Repeat className="size-4" />}
              help={<>{data.turnover.source}. {data.turnover.note}</>}
              hint={data.turnover.annual_sales != null ? `sales alone ${pct(data.turnover.annual_sales, 0)} · ${data.turnover.n_months} months` : "needs beginning-of-month prices"} />
            <Stat label="Median days held" value={data.holding_periods.realised.winners.median_days} format={days} icon={<Hourglass className="size-4" />}
              hint={`winners sold · losers ${days(data.holding_periods.realised.losers.median_days)}`} help={data.holding_periods.weighting} />
            <Stat label="Cost of churn" value={data.churn.total} format={inr} tone="loss" icon={<Wallet className="size-4" />}
              help={data.churn.how}
              hint={`charges ${inr(data.churn.charges)} · tax ${inr(data.churn.tax)}${data.churn.drag_pct_a_year != null ? ` · ${data.churn.drag_pct_a_year.toFixed(2)}%/yr of value` : ""}`} />
          </div>
          <DispositionCard d={data.disposition} title="Disposition effect (stocks)" />
          {(data.disposition_funds.sale_days > 0) && <DispositionCard d={data.disposition_funds} title="Disposition effect (funds, variant)" />}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card title="Winners sold early, losers held?" subtitle="Days held, unit-weighted median">
              <Table>
                <thead><tr><th /><th>Count</th><th>Median days</th></tr></thead>
                <tbody>
                  <tr><td>Winners you sold</td><td className="num">{data.holding_periods.realised.winners.n}</td><td className="num">{days(data.holding_periods.realised.winners.median_days)}</td></tr>
                  <tr><td>Losers you sold</td><td className="num">{data.holding_periods.realised.losers.n}</td><td className="num">{days(data.holding_periods.realised.losers.median_days)}</td></tr>
                  <tr><td>Still held, in profit</td><td className="num">{data.holding_periods.open.in_profit.n}</td><td className="num">{days(data.holding_periods.open.in_profit.median_days)}</td></tr>
                  <tr><td>Still held, at a loss</td><td className="num">{data.holding_periods.open.in_loss.n}</td><td className="num">{days(data.holding_periods.open.in_loss.median_days)}</td></tr>
                </tbody>
              </Table>
              {data.holding_periods.realised.reading && <p className="mt-2 text-sm">{data.holding_periods.realised.reading}</p>}
              {data.after_sale && (
                <p className="mt-2 text-xs text-muted">
                  In the {data.after_sale.horizon_trading_days} trading days after each sale, winners you sold returned {pct(data.after_sale.winners_sold.mean_excess)} (n = {data.after_sale.winners_sold.n})
                  and losers you kept {pct(data.after_sale.losers_kept.mean_excess)} (n = {data.after_sale.losers_kept.n}), over {data.after_sale.excess_over}. {data.after_sale.source}.
                </p>
              )}
            </Card>
            <Card title="Trading frequency vs returns" subtitle="Per financial year; a table, not a fitted relationship">
              <Table>
                <thead><tr><th>Year</th><th>Decisions</th><th>Turnover</th><th>Your TWR</th><th>NIFTYBEES</th></tr></thead>
                <tbody>
                  {data.frequency_vs_returns.map((r) => (
                    <tr key={r.fy}>
                      <td>{r.label}</td><td className="num">{r.trades}</td><td className="num">{pct(r.turnover_annual, 0)}</td>
                      <td className="num">{pct(r.twr)}</td><td className="num">{pct(r.benchmark)}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              {data.churn.near_long_term.length > 0 && (
                <Callout tone="warn" title={`${data.churn.near_long_term.length} sale(s) within 60 days of turning long-term`}>
                  {data.churn.near_long_term.map((x) => `${x.name} (${x.days_short} days short, gain ${inr(x.gain)})`).join("; ")}
                </Callout>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
