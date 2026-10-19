"use client";

// One pre-registered strategy's evidence (#307): verdict, criteria, in-sample / out-of-sample / stress statistics,
// the equity curve, and the by-year and by-IV-regime tables. Unknown values read "—", never 0.

import { Check, X } from "lucide-react";

import type { Criterion, Setup, StrategyResult, WindowStats } from "@/components/fno/backtest-types";
import { TimeSeriesChart, fmtCompactINR } from "@/components/charts";
import { Badge, Card, Table, type Tone } from "@/components/ui";

import { Parts } from "./fit";

export const rupees = (x: number | null | undefined) =>
  x == null ? "—" : `${x < 0 ? "−" : ""}₹${Math.abs(Math.round(x)).toLocaleString("en-IN")}`;
const pct = (x: number | null | undefined, d = 1) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const num = (x: number | null | undefined, d = 2) => (x == null ? "—" : x.toFixed(d));

/** A y range with room below and above: the starting-capital line sat on the x axis, its label over the first date. */
const padded = (vals: number[]): [number, number] => {
  const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.15 || Math.abs(hi) * 0.01 || 1;
  return [lo - pad, hi + pad];
};

export const STATUS: Record<string, { tone: Tone; label: string }> = {
  candidate: { tone: "gain", label: "candidate" },
  fails: { tone: "loss", label: "fails" },
  overlay_reported: { tone: "info", label: "overlay: reported only" },
  in_sample_only: { tone: "warn", label: "in-sample only" },
  no_data: { tone: "neutral", label: "no data" },
};

const ROWS: [string, (w: WindowStats) => string][] = [
  ["Trades (expiries)", (w) => `${w.trades} (${w.expiries})`],
  ["Expectancy / trade, after costs", (w) => rupees(w.expectancy)],
  ["t-stat (expiry-clustered)", (w) => num(w.t_stat)],
  ["95% bootstrap CI", (w) => (w.ci_low == null ? "—" : `${rupees(w.ci_low)} … ${rupees(w.ci_high)}`)],
  ["Win rate", (w) => pct(w.win_rate, 0)],
  ["Average win / loss", (w) => `${rupees(w.avg_win)} / ${rupees(w.avg_loss)}`],
  ["Worst trade · 5% CVaR", (w) => `${rupees(w.worst_trade)} · ${rupees(w.cvar_5)}`],
  ["Total net · costs paid", (w) => `${rupees(w.total_net)} · ${rupees(w.costs)}`],
  ["CAGR on the capital", (w) => pct(w.cagr)],
  ["Return on margin, a year", (w) => pct(w.return_on_margin_annual)],
  ["Max drawdown (% capital)", (w) => (w.max_drawdown_pct == null ? "—" : `${w.max_drawdown_pct.toFixed(1)}%`)],
  ["Sharpe · Sortino", (w) => `${num(w.sharpe)} · ${num(w.sortino)}`],
  ["Deflated Sharpe", (w) => num(w.deflated_sharpe ?? null, 3)],
];

export function StatsTable({ cols }: { cols: [string, WindowStats | undefined][] }) {
  const live = cols.filter((c): c is [string, WindowStats] => !!c[1]);
  return (
    <Table label="Window statistics" stickyFirst>
      <thead><tr><th>Measure</th>{live.map(([h, w]) => <th key={h}>{h}<span className="block normal-case tracking-normal">{w.from} → {w.to}</span></th>)}</tr></thead>
      <tbody>{ROWS.map(([label, f]) => <tr key={label}><td className="text-muted">{label}</td>{live.map(([h, w]) => <td key={h} className="num">{f(w)}</td>)}</tr>)}</tbody>
    </Table>
  );
}

export function CriteriaList({ items }: { items: Criterion[] }) {
  return (
    <ul className="space-y-1 text-sm">
      {items.map((c) => (
        <li key={c.criterion} className="flex items-start gap-2">
          {c.pass ? <Check className="mt-0.5 size-4 shrink-0 text-gain" aria-label="passes" /> : <X className="mt-0.5 size-4 shrink-0 text-loss" aria-label="fails" />}
          <span>{c.criterion}: <span className="num font-medium">{c.value == null ? "unknown" : c.value.toLocaleString("en-IN")}</span> <span className="text-muted">(needs {c.rule})</span></span>
        </li>
      ))}
    </ul>
  );
}

function Breakdown({ title, rows }: { title: string; rows: Record<string, WindowStats> }) {
  return (
    <div className="min-w-0">
      <p className="mb-1 text-xs font-medium text-muted">{title}</p>
      {/* inside a grid cell: no bleed to the card's edge (it ran over the neighbouring table), scrolls in its own box */}
      <Table label={title} bleed={false} stickyFirst>
        <thead><tr><th>Group</th><th>Trades</th><th>Expectancy</th><th>Win rate</th><th>Worst</th><th>Total net</th></tr></thead>
        <tbody>{Object.entries(rows).map(([k, w]) => (
          <tr key={k}><td>{k}</td><td className="num">{w.trades}</td><td className="num">{rupees(w.expectancy)}</td>
            <td className="num">{pct(w.win_rate, 0)}</td><td className="num">{rupees(w.worst_trade)}</td><td className="num">{rupees(w.total_net)}</td></tr>
        ))}</tbody>
      </Table>
    </div>
  );
}

export function StrategyCard({ r, capital = 1_000_000 }: { r: StrategyResult; capital?: number }) {
  const st = STATUS[r.status] ?? { tone: "neutral" as Tone, label: r.status };
  const w = r.windows;
  return (
    <Card title={<span className="flex min-w-0 flex-wrap items-center gap-2"><span className="line-clamp-2 min-w-0 break-words" title={r.name}>{r.name}</span><Badge tone={st.tone}>{st.label}</Badge><Badge tone="neutral">{r.role}</Badge></span>}
      subtitle={`${r.key} · ${r.underlying ?? "no underlying"} · spec ${r.spec_hash.slice(0, 10)}`}>
      <div id={r.key} className="min-w-0 space-y-4">
        <ul className="list-disc pl-5 text-sm">{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
        {r.criteria && <CriteriaList items={r.criteria} />}
        {w && <StatsTable cols={[["In-sample", w.is], ["Out-of-sample", w.oos], ["OOS, slippage ×2", w.oos_stress]]} />}
        {r.equity && r.equity.length > 1 && (
          <TimeSeriesChart data={r.equity.map(([d, v]) => ({ date: d, equity: v }))} series={[{ key: "equity", label: "Equity (₹)" }]}
            format={fmtCompactINR} height={220} references={[{ y: capital, label: `${fmtCompactINR(capital)} start`, tone: "muted" }]} showChange={false}
            yDomain={padded([capital, ...r.equity.map(([, v]) => v)])} />
        )}
        {r.overlay && (
          <p className="text-sm">Held stock alone {rupees(r.overlay.stock_total)} (worst period {rupees(r.overlay.stock_worst)}); with the overlay {rupees(r.overlay.with_overlay_total)} (worst {rupees(r.overlay.with_overlay_worst)}) over {r.overlay.periods} periods. <span className="text-muted">{r.overlay.note}</span></p>
        )}
        <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
          {r.by_year && Object.keys(r.by_year).length > 0 && <Breakdown title="By year of entry (walk-forward, frozen spec)" rows={r.by_year} />}
          {r.by_iv_regime && Object.keys(r.by_iv_regime).length > 0 && <Breakdown title="By IV percentile at entry (low < ⅓ ≤ mid < ⅔ ≤ high)" rows={r.by_iv_regime} />}
        </div>
        {r.skips && Object.keys(r.skips).length > 0 && (
          <p className="text-xs text-muted">Entries refused: {Object.entries(r.skips).map(([k, n]) => `${k} (${n})`).join(" · ")}</p>
        )}
      </div>
    </Card>
  );
}

export function SetupView({ s }: { s: Setup | null }) {
  if (!s) return <p className="text-sm text-muted">Setup not computed.</p>;
  if (s.status !== "entry") return <p className="text-sm">No entry at the last close{s.as_of ? ` (${s.as_of})` : ""}: {s.reason ?? "unknown"}.</p>;
  return (
    <div className="space-y-1 text-sm">
      <p>At the {s.as_of} close: {s.symbol} {s.expiry} ({s.dte} days), {s.lots} × {s.lot}: {s.legs?.map((l) => `${l.side} ${l.strike} ${l.right} @ ~${l.ref_price}`).join(", ")}.</p>
      <p className="text-muted"><Parts parts={[`Premium ${rupees(s.premium)}`, `max loss ${rupees(s.max_loss)}`, `margin ≈ ${rupees(s.margin_approx)}`, `entry costs ${rupees(s.entry_costs)}.`]} /> {s.note}</p>
    </div>
  );
}
