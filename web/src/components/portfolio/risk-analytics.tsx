"use client";

// The Risk tab: realised risk of the value history (volatility, beta, drawdown, VaR/CVaR, Sharpe/Sortino with a stated
// risk-free rate), benchmark-relative and rolling views, the drawdown table and the VaR backtest (#349), today's mix over
// the last 12 months (risk contribution by holding and sector), stress scenarios (unmapped holdings never shown as 0 %,
// #423) and the historical stress replay (#345).

import { Activity, Flame, Gauge as GaugeIcon, PieChart, ShieldAlert } from "lucide-react";
import { useState } from "react";

import { BarsChart } from "@/components/charts";
import { Badge, Button, Callout, Card, ErrorNote, Field, Segmented, Table, cx, inputClass } from "@/components/ui";
import { day, useApi } from "@/lib/api";

import { FactorExposures } from "./factor-exposures";
import { JointRiskCard } from "./joint-risk";
import { AnalyticsFooter, type AnalyticsBase, Loading, type Metric, MetricRow, NotEnough, pct, spct } from "./shared-analytics";
import { DrawdownTable, type Episode, RelativeCard, type Relative, RollingCard, type Rolling, VarBacktestLine, type VarBacktest } from "./risk-relative";
import { StressReplay } from "./stress-replay";
import { inr, signed } from "./types";

type Scenario = {
  id: string; label: string; available: boolean; reason?: string; note: string; window?: string[]; peak?: string; trough?: string;
  benchmark_move?: number; portfolio_move?: number | null; inr?: number | null;
  mapped_value?: number; unmapped_value?: number; mapped_share?: number | null; assume_beta1?: boolean;
  // move and ₹ are null for an unmapped holding (#423): unknown, never a 0 % move
  positions?: { key: string; name: string; value?: number; move: number | null; inr: number | null; how: string; assumed?: boolean }[];
};
type Risk = AnalyticsBase & {
  value?: number;
  risk_free: { rate: number; label: string; as_of: string | null; source: string | null; fallback: boolean; user_set: boolean };
  realised: null | (Relative & {
    rolling?: Rolling; drawdowns?: Episode[]; var_backtest?: VarBacktest; var_ledger?: VarBacktest;
    window: { start: string; end: string; returns: number };
    volatility: Metric; beta: Metric; tracking_error: Metric; sharpe: Metric; sortino: Metric;
    var_1d: Metric; cvar_1d: Metric; var_21d: Metric; cvar_21d: Metric;
    max_drawdown: Metric & { peak: string; trough: string; recovered: string | null; days_to_recover: number | null; underwater_days: number; current: number };
  });
  hypothetical: null | {
    note: string; reason?: string; days?: number; from_?: string; to?: string; how?: string; missing?: string[];
    volatility?: Metric; var_1d?: Metric; cvar_1d?: Metric;
    positions: { key: string; name: string; weight: number; risk_share: number; sector: string }[];
    sectors: { sector: string; weight: number; risk_share: number }[];
  };
  stress: Scenario[];
};

const money = (m: Metric | undefined) => (m?.inr != null ? <p className="num text-[11px] font-normal text-muted">≈ {inr(m.inr)}</p> : null);

function Stress({ items, beta1, setBeta1 }: { items: Scenario[]; beta1: boolean; setBeta1: (v: boolean) => void }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Card title="Stress scenarios" icon={<Flame className="size-4" />}
      help="Past episodes applied to today's holdings: instructive, not a probability. Historical VaR only knows the window it was measured in, so it is shown next to these. A holding that cannot be mapped is listed with its value and left out of the figure, never counted as a 0 % move.">
      <label className="mb-2 flex items-center gap-2 text-xs text-muted">
        <input type="checkbox" checked={beta1} onChange={(e) => setBeta1(e.target.checked)} />
        Hypothetical fall: assume beta 1 for equity with under 120 days of prices (an assumption, labelled on each row)
      </label>
      <div className="space-y-2">
        {items.map((s) => (
          <div key={s.id} className="rounded-lg border border-border">
            <button type="button" onClick={() => setOpen(open === s.id ? null : s.id)} className="flex w-full flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-left">
              <span className="text-sm font-medium">{s.label}</span>
              {s.available ? (
                <span className="flex items-center gap-3 text-xs">
                  <span className="text-muted">index {spct(s.benchmark_move)}</span>
                  <span className={cx("num font-semibold", (s.inr ?? 0) < 0 ? "text-loss" : "text-gain")}>{spct(s.portfolio_move)}{s.inr != null ? ` · ${signed(s.inr)}` : ""}</span>
                  {(s.unmapped_value ?? 0) > 0 && <Badge tone="warn">{s.positions?.filter((p) => p.move == null).length} unmapped</Badge>}
                </span>
              ) : <Badge tone="neutral">not available</Badge>}
            </button>
            {open === s.id && (
              <div className="border-t border-border px-3 py-2.5 text-xs">
                {s.available ? (
                  <>
                    {s.peak && <p className="text-muted">NIFTYBEES peak {day(s.peak)} → trough {day(s.trough)} (found inside {day(s.window?.[0])} to {day(s.window?.[1])}).</p>}
                    <p className="mt-1 text-muted">{s.note}</p>
                    {s.mapped_value != null && <p className="mt-1 text-muted">The figure is on the {inr(s.mapped_value)} that could be mapped{s.mapped_share != null ? `, ${pct(s.mapped_share)} of today's holdings` : ""}{(s.unmapped_value ?? 0) > 0 ? `; ${inr(s.unmapped_value)} is unmapped and not in it` : ""}.</p>}
                    <Table label={`${s.label}: holdings`} className="mt-2">
                      <thead><tr><th>Holding</th><th className="text-right">Move</th><th className="text-right">₹</th><th>How</th></tr></thead>
                      <tbody>{s.positions?.map((p) => (
                        <tr key={p.key}><td className="max-w-[14rem] truncate">{p.name}</td>
                          <td className="num text-right">{p.move == null ? <Badge tone="warn">unmapped</Badge> : spct(p.move)}</td>
                          <td className={cx("num text-right", (p.inr ?? 0) < 0 ? "text-loss" : "text-muted")}>{p.inr == null ? "—" : signed(p.inr)}</td>
                          <td className={cx(p.assumed ? "text-warn" : "text-muted")}>{p.how}</td></tr>
                      ))}</tbody>
                    </Table>
                  </>
                ) : <p className="text-muted">{s.reason}. {s.note}</p>}
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

export function RiskAnalytics({ refresh }: { refresh: number }) {
  const [rfInput, setRfInput] = useState("");
  const [rf, setRf] = useState<string | null>(null);
  const [view, setView] = useState<"holding" | "sector">("holding");
  const [beta1, setBeta1] = useState(false);
  const { data, error, reload } = useApi<Risk>(`/api/portfolio/analytics/risk?r=${refresh}${rf ? `&rf=${encodeURIComponent(rf)}` : ""}${beta1 ? "&assume_beta1=true" : ""}`);
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <Loading what="risk figures" />;
  if (!data.available || !data.realised) return <NotEnough data={data} what="risk figures" />;
  const r = data.realised, h = data.hypothetical, dd = r.max_drawdown;
  const rows = view === "holding"
    ? (h?.positions ?? []).map((p) => ({ label: p.name.length > 22 ? p.name.slice(0, 21) + "…" : p.name, weight: p.weight * 100, risk: p.risk_share * 100 }))
    : (h?.sectors ?? []).map((s) => ({ label: s.sector.length > 22 ? s.sector.slice(0, 21) + "…" : s.sector, weight: s.weight * 100, risk: s.risk_share * 100 }));
  return (
    <div className="space-y-4">
      <Callout tone="info" icon={<ShieldAlert className="size-4" />} title="What these numbers can and cannot say">
        They describe the past {r.window.returns} trading days of your time-weighted returns ({day(r.window.start)} to {day(r.window.end)}). Short histories give noisy estimates, and a window without a crisis understates the tails: read the stress scenarios alongside.
      </Callout>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Volatility and market sensitivity" icon={<Activity className="size-4" />}>
          <MetricRow label="Volatility (annualised)" m={r.volatility} format={(v) => pct(v)} help="How much daily returns swing, scaled to a year. About two years in three land within ± this much of the average (if returns were normal, which they are not quite)." />
          <MetricRow label="Beta vs NIFTYBEES" m={r.beta} format={(v) => v.toFixed(2)} help="How much your portfolio moved per 1 % move of the Nifty 50 ETF: 1.2 means about 1.2 % on average. Needs 120 trading days." />
          <MetricRow label="Tracking error" m={r.tracking_error} format={(v) => pct(v)} help="How far your returns wander from the index's, annualised. Low: index-like; high: very different from the index." />
          <MetricRow label="Sharpe ratio" m={r.sharpe} format={(v) => v.toFixed(2)} help="Return above the risk-free rate per unit of volatility. Needs about a year of data." />
          <MetricRow label="Sortino ratio" m={r.sortino} format={(v) => v.toFixed(2)} help="Like Sharpe, but only falls below the risk-free rate count as risk." />
          <div className="mt-3 flex flex-wrap items-end gap-2 border-t border-border pt-3">
            <p className="w-full text-[11px] text-muted">Risk-free rate: {(data.risk_free.rate * 100).toFixed(2)} % a year — {data.risk_free.label}{data.risk_free.as_of ? `, as of ${day(data.risk_free.as_of)}` : ""}{data.risk_free.fallback ? " (FBIL unreachable: stored curve)" : ""}.</p>
            <Field label="Use your own rate (% a year)"><input inputMode="decimal" value={rfInput} onChange={(e) => setRfInput(e.target.value)} placeholder="e.g. 6.5" className={cx(inputClass, "w-28 num")} /></Field>
            <Button variant="secondary" disabled={!rfInput || Number.isNaN(Number(rfInput))} onClick={() => setRf(rfInput)}>Apply</Button>
            {rf && <Button variant="ghost" onClick={() => { setRf(null); setRfInput(""); }}>Reset</Button>}
          </div>
        </Card>
        <Card title="Losses" icon={<GaugeIcon className="size-4" />}>
          <MetricRow label="Max drawdown" m={dd} format={(v) => pct(v)}
            help="The largest fall of your time-weighted index from a previous high, and whether it has been recovered."
            extra={dd.value != null && dd.value < 0 ? <p className="text-[10px] font-normal text-muted">{day(dd.peak)} → {day(dd.trough)} · {dd.recovered ? `recovered ${day(dd.recovered)} (${dd.days_to_recover} days)` : `not recovered · ${dd.underwater_days} days below the peak`}</p> : null} />
          <div className="flex justify-between border-b border-border py-2.5 text-sm"><span>Below the last peak now</span><span className="num font-medium">{pct(dd.current)}</span></div>
          <MetricRow label="1-day VaR 95 %" m={r.var_1d} format={(v) => pct(v, 2)} extra={money(r.var_1d)}
            help="Historical value at risk: on about 1 day in 20 of this window, the loss was at least this much (true by construction). Whether that held on new days is what the backtests below check." />
          <MetricRow label="1-day CVaR 95 %" m={r.cvar_1d} format={(v) => pct(v, 2)} extra={money(r.cvar_1d)} help="Expected shortfall: the average loss on those worst 1-in-20 days." />
          <MetricRow label="1-month VaR 95 %" m={r.var_21d} format={(v) => pct(v, 2)} extra={money(r.var_21d)} help="The same over 21 trading days, from overlapping windows: far fewer independent observations, so treat it as rough." />
          <MetricRow label="1-month CVaR 95 %" m={r.cvar_21d} format={(v) => pct(v, 2)} extra={money(r.cvar_21d)} help="The average of the worst 5 % of 21-day returns." />
          <VarBacktestLine b={r.var_backtest} title="VaR backtest (walk-forward)" />
          <VarBacktestLine b={r.var_ledger} title="Logged VaR backtest" />
        </Card>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <RelativeCard r={r} />
        <DrawdownTable items={r.drawdowns} />
      </div>
      <RollingCard rolling={r.rolling} />

      <Card title="Where today's risk comes from" icon={<PieChart className="size-4" />}
        help={`${h?.note ?? ""} ${h?.how ?? ""}`}
        actions={h && !h.reason ? <Segmented value={view} onChange={setView} options={[{ value: "holding", label: "Holdings" }, { value: "sector", label: "Sectors" }]} /> : undefined}>
        {!h || h.reason ? <p className="text-sm text-muted">{h?.reason ?? "Not available."}</p> : (
          <>
            <p className="mb-3 text-xs text-muted">Today&apos;s mix over {h.days} trading days ({day(h.from_)} to {day(h.to)}): volatility {pct(h.volatility?.value)}, 1-day VaR 95 % {pct(h.var_1d?.value, 2)} (≈ {inr(h.var_1d?.inr)}). A holding whose share of risk is larger than its share of value adds more than its weight to the swings.</p>
            <BarsChart data={rows} x="label" layout="vertical" labelWidth={150} height={Math.max(160, rows.length * 34 + 40)} format={(v) => `${v.toFixed(1)}%`}
              series={[{ key: "weight", label: "Share of value", color: "var(--chart-2)" }, { key: "risk", label: "Share of risk", color: "var(--chart-1)" }]} />
            {h.missing && h.missing.length > 0 && <p className="mt-2 text-[11px] text-warn">Not in this view (no common price history): {h.missing.join(", ")}.</p>}
          </>
        )}
      </Card>
      {data.stress.length > 0 && <Stress items={data.stress} beta1={beta1} setBeta1={setBeta1} />}
      <StressReplay refresh={refresh} />
      <FactorExposures refresh={refresh} />
      <JointRiskCard refresh={refresh} />
      <AnalyticsFooter data={data} />
    </div>
  );
}
