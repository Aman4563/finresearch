"use client";

// The Performance tab: the daily value history rebuilt from transactions and prices, time-weighted return against
// NIFTYBEES (total return), and "the same cash flows put into NIFTYBEES" (direct index equivalent).

import { LineChart as LineIcon, Scale, TrendingUp, Wallet } from "lucide-react";

import { TimeSeriesChart, fmtCompactINR } from "@/components/charts";
import { Callout, Card, ErrorNote, InfoTip, Stat } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import { AnalyticsFooter, type AnalyticsBase, Loading, type Metric, MetricRow, NotEnough, spct } from "./shared-analytics";
import { inr, signed } from "./types";

type Perf = AnalyticsBase & {
  series: { date: string; portfolio: number; benchmark: number | null; value: number; invested: number; die_value: number | null }[];
  summary: null | {
    days: number; calendar_days: number; value: number; invested: number; gain: number; initial: number;
    twr: Metric; twr_annualised: Metric; xirr: Metric;
    benchmark: null | {
      label: string; note: string | null; symbol: string; twr: Metric; twr_gap_pp: number | null; die_value: number;
      die_xirr: Metric; alpha_inr: number; pme: Metric; clamped: boolean; dividends: number; splits: number;
      verdict: string | null; early: boolean;
    };
  };
};

export function PerformanceAnalytics({ refresh }: { refresh: number }) {
  const { data, error, reload } = useApi<Perf>(`/api/portfolio/analytics/performance?r=${refresh}`);
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <Loading what="your value history" />;
  if (!data.available || !data.summary) return <NotEnough data={data} what="value history" />;
  const s = data.summary, b = s.benchmark;
  return (
    <div className="space-y-4">
      <div className="stagger grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Time-weighted return" icon={<TrendingUp className="size-4" />} tone={(s.twr.value ?? 0) >= 0 ? "gain" : "loss"}
          display={<span className="num">{spct(s.twr.value)}</span>}
          hint={s.twr_annualised.value != null ? `${spct(s.twr_annualised.value)} a year` : `since ${day(data.start)}, not annualised`}
          help="The return of your holdings with the timing and size of your purchases and sales removed: the number to compare with an index or a fund." />
        <Stat label="XIRR (this window)" icon={<Wallet className="size-4" />}
          display={s.xirr.value == null ? <span className="text-sm font-normal text-muted">{s.xirr.reason}</span> : <span className="num">{spct(s.xirr.value)}</span>}
          hint="money-weighted, your cash flows"
          help="Annualised return on your own cash flows in this window: the first day's holdings count as money put in, today's value as money back. It can differ from the Holdings tab's XIRR, which starts at each purchase and uses cost." />
        <Stat label={`Same flows into ${b?.symbol ?? "NIFTYBEES"}`} icon={<Scale className="size-4" />}
          display={b ? <span className="num">{inr(b.die_value)}</span> : <span className="text-sm text-muted">unavailable</span>}
          hint={b ? `yours ${inr(s.value)} · ${signed(b.alpha_inr)}` : undefined}
          help="Direct index equivalent: every rupee you put in (or took out) on a day is put into (or taken out of) the Nifty 50 ETF NIFTYBEES on the same day, distributions reinvested. The difference is what your choices added or cost in rupees." />
        <Stat label="Public-market equivalent" icon={<LineIcon className="size-4" />}
          display={b?.pme.value != null ? <span className="num">{b.pme.value.toFixed(3)}</span> : <span className="text-sm text-muted">—</span>}
          hint={b?.die_xirr.value != null ? `index XIRR ${spct(b.die_xirr.value)}` : b?.die_xirr.reason ?? undefined}
          help="Kaplan–Schoar PME: your money out plus today's value, over your money in, each grown at the index's return from its date to today. Above 1 means you did better than the index with the same cash flows; 1 means the same." />
      </div>

      {b && (
        b.early ? (
          <Callout tone="info" title="Early days">
            The series covers {s.calendar_days} days. Under a year the gap to the index is mostly day-to-day noise, so no ahead/behind statement is made yet;
            even over 3-5 years it is weak evidence about skill.
          </Callout>
        ) : b.verdict && <Callout tone="info" title="Against the index">{b.verdict}</Callout>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Growth of ₹100" icon={<LineIcon className="size-4" />}
          help={`Both lines start at 100 on ${day(data.start)}. Yours is the time-weighted index (flows removed); the other is ${b?.label ?? "NIFTYBEES"}. One axis, so the two can be compared directly.`}>
          <TimeSeriesChart data={data.series} area={false} series={[{ key: "portfolio", label: "Your portfolio (TWR)", color: "var(--chart-1)" },
            ...(b ? [{ key: "benchmark", label: b.symbol + " total return", color: "var(--chart-3)", dashed: true }] : [])]}
            format={(v) => v.toFixed(1)} ranges={["3M", "1Y", "3Y", "ALL"]} defaultRange="ALL" showChange={false} />
        </Card>
        <Card title="Your value vs the index with your cash flows" icon={<Wallet className="size-4" />}
          help="Rupees on one axis: your portfolio's value each day, what the same purchases and sales in NIFTYBEES would be worth, and the net money you put in.">
          <TimeSeriesChart data={data.series} area={false} series={[{ key: "value", label: "Your value", color: "var(--chart-1)" },
            ...(b ? [{ key: "die_value", label: "Same flows in " + b.symbol, color: "var(--chart-3)", dashed: true }] : []),
            { key: "invested", label: "Net money in", color: "var(--muted)" }]}
            format={fmtCompactINR} ranges={["3M", "1Y", "3Y", "ALL"]} defaultRange="ALL" showChange={false} />
        </Card>
      </div>

      <Card title="Details" help="Each figure's method is in its info tip. Nothing here is a forecast.">
        <div className="grid gap-x-8 md:grid-cols-2">
          <div>
            <MetricRow label="Time-weighted return" m={s.twr} format={(v) => spct(v, 2)} help="Daily returns chained: (value − that day's net flow) ÷ previous value − 1." />
            <MetricRow label="Annualised" m={s.twr_annualised} format={(v) => spct(v, 2)} help="Only for a year or more: annualising a few months exaggerates." />
            <MetricRow label="XIRR (window)" m={s.xirr} format={(v) => spct(v, 2)} help="Money-weighted return on the window's cash flows." />
            <div className="flex justify-between py-2.5 text-sm"><span className="inline-flex items-center gap-1">Net money in <InfoTip>Value on the first day plus every purchase since, minus sales and dividends paid out.</InfoTip></span><span className="num font-medium">{inr(s.invested)}</span></div>
          </div>
          {b && (
            <div>
              <MetricRow label={`${b.symbol} total return`} m={b.twr} format={(v) => spct(v, 2)}
                help={`${b.label}. It stands in for the Nifty 50 TRI and trails it by the ETF's expense ratio and tracking error. ${b.dividends} distribution(s) and ${b.splits} split(s) applied from NSE's corporate actions.`} />
              <div className="flex justify-between border-b border-border py-2.5 text-sm"><span className="inline-flex items-center gap-1">Gap (TWR) <InfoTip>Your time-weighted return minus the index&apos;s over the same days, in percentage points.</InfoTip></span><span className="num font-medium">{b.twr_gap_pp == null ? "—" : `${b.twr_gap_pp > 0 ? "+" : ""}${b.twr_gap_pp.toFixed(2)} pp`}</span></div>
              <MetricRow label="Index XIRR, same flows" m={b.die_xirr} format={(v) => spct(v, 2)} help="XIRR of your cash flows with the index-equivalent value as today's value." />
              <div className="flex justify-between py-2.5 text-sm"><span className="inline-flex items-center gap-1">Difference in ₹ <InfoTip>Your value today minus the index-equivalent value. Taxes on either side are not modelled (pre-tax both sides).</InfoTip></span><span className={(b.alpha_inr ?? 0) >= 0 ? "num font-medium text-gain" : "num font-medium text-loss"}>{signed(b.alpha_inr)}</span></div>
              {b.clamped && <p className="text-[11px] text-warn">A withdrawal was larger than the index holding at the time; the index units were floored at zero.</p>}
              {b.note && <p className="text-[11px] text-warn">{b.note}</p>}
            </div>
          )}
        </div>
      </Card>
      <AnalyticsFooter data={data} extra={<p>Value history uses daily closes (stocks) and NAVs (funds); today&apos;s intraday moves are not included.</p>} />
    </div>
  );
}
