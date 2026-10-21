"use client";

// Holding-level optimiser (#346): robust weights (minimum variance, risk parity, HRP; no expected returns) within
// what cannot be sold and the position limit, shown only after the pre-registered out-of-sample check passes, with a
// tax-aware FIFO trade list. Suggested trades only: nothing is placed. Loaded on request (it reads up to four years
// of each holding's prices). An unknown cost or tax is shown as unknown, never ₹0. The arithmetic is
// portfolio/optimise.py; this card only shows it.

import { ShieldAlert, Sigma } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Callout, Card, ErrorNote, Segmented, Table } from "@/components/ui";
import { useApi } from "@/lib/api";

import { Loading, pct } from "./shared-analytics";
import { inr, units } from "./types";

type Method = "min_variance" | "risk_parity" | "hrp";
type Row = {
  holding_id: number; name: string; current_pct: number; target_pct: number | null; lo_pct: number | null; hi_pct: number | null;
  status: "optimised" | "fixed"; reason: string | null;
};
type Sell = { holding_id: number; name: string; category: string; units: number; gross: number; charges: number; exit_load: number | null;
  gain: number; tax: number | null; costs_unknown: string[]; notes: string[] };
type Buy = { holding_id: number; name: string; units: number; amount: number; stamp: number; costs_unknown: string[] };
type Validation = { passed: boolean; reasons: string[]; windows: number; days: number; min_windows: number; max_turnover: number;
  vol: Record<"method" | "equal_weight" | "current", number | null>; turnover: Record<"method" | "equal_weight" | "current", number | null> };
export type OptimisePlan = {
  status: "no_value" | "too_few" | "infeasible" | "not_validated" | "suggested"; message: string; disclaimer: string; method: Method;
  complete: boolean; excluded: { holding_id: number; name: string; reason: string }[]; holdings: Row[];
  skipped: { holding_id: number; name: string; units: number; reason: string }[]; sells: Sell[]; buys: Buy[];
  validation: Validation | null; risk: { vol_current: number; vol_target: number; shrinkage: number; note: string } | null;
  totals: { sold_gross: number; sell_charges: number; stamp_duty: number; tax: number | null; bought: number; unfunded: number; costs_unknown: string[] } | null;
  tax_complete: boolean; unclassified: { detail: string }; notes: string[];
};

const METHODS: { value: Method; label: string }[] = [
  { value: "min_variance", label: "Minimum variance" }, { value: "risk_parity", label: "Risk parity" }, { value: "hrp", label: "HRP" },
];
const pp = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(1)}%`);

function Check({ v }: { v: Validation }) {
  const row = (k: "method" | "equal_weight" | "current", label: string) => (
    <tr key={k}><td>{label}</td><td className="num text-right">{pct(v.vol[k])}</td><td className="num text-right">{pct(v.turnover[k], 0)}</td></tr>
  );
  return (
    <div className="space-y-2">
      <p className="text-xs">
        <Badge tone={v.passed ? "gain" : "warn"}>{v.passed ? "passed" : "not passed"}</Badge>{" "}
        Out-of-sample check: {v.windows} monthly windows ({v.days} days), each fitted only on the year before it.
      </p>
      <Table label="Out-of-sample comparison">
        <thead><tr><th>Weights</th><th className="text-right">Realised volatility</th><th className="text-right">Turnover a year</th></tr></thead>
        <tbody>{[row("method", "This method"), row("equal_weight", "Equal weight"), row("current", "Your current weights")]}</tbody>
      </Table>
      {v.reasons.length > 0 && <ul className="list-disc pl-5 text-xs text-warn">{v.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
    </div>
  );
}

function Weights({ rows }: { rows: Row[] }) {
  return (
    <Table label="Target weights">
      <thead><tr><th>Holding</th><th className="text-right">Now</th><th className="text-right">Target</th><th className="text-right">Allowed</th><th>Note</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.holding_id}><td className="max-w-[14rem] truncate">{r.name}</td><td className="num text-right">{pp(r.current_pct)}</td>
          <td className="num text-right font-medium">{pp(r.target_pct)}</td>
          <td className="num text-right text-muted">{r.lo_pct == null ? "fixed" : `${pp(r.lo_pct)}–${pp(r.hi_pct)}`}</td>
          <td className="text-xs text-muted">{r.reason ?? ""}</td></tr>
      ))}</tbody>
    </Table>
  );
}

const Unknown = ({ items }: { items: string[] }) =>
  items.length ? <span className="text-[10px] text-warn">unknown: {items.join("; ")}</span> : null;

function Trades({ p }: { p: OptimisePlan }) {
  if (!p.sells.length && !p.buys.length) return null;
  const t = p.totals;
  return (
    <div className="space-y-2">
      {p.sells.length > 0 && <Table label="Suggested sales">
        <thead><tr><th>Sell</th><th>Lots</th><th className="text-right">Units</th><th className="text-right">Value</th><th className="text-right">Gain</th>
          <th className="text-right">Tax</th><th className="text-right">STT</th><th className="text-right">Exit load</th></tr></thead>
        <tbody>{p.sells.map((s, i) => (
          <tr key={`${s.holding_id}-${i}`}><td className="max-w-[12rem] truncate">{s.name}<br /><Unknown items={s.costs_unknown} /></td>
            <td className="text-xs text-muted">{s.category}</td><td className="num text-right">{units(s.units)}</td>
            <td className="num text-right">{inr(s.gross)}</td><td className="num text-right">{inr(s.gain)}</td>
            <td className="num text-right">{s.tax == null ? "unknown" : inr(s.tax)}</td><td className="num text-right">{inr(s.charges, 2)}</td>
            <td className="num text-right">{s.exit_load == null ? "unknown" : inr(s.exit_load)}</td></tr>
        ))}</tbody>
      </Table>}
      {p.buys.length > 0 && <Table label="Suggested buys">
        <thead><tr><th>Buy</th><th className="text-right">Units</th><th className="text-right">Amount</th><th className="text-right">Stamp duty</th></tr></thead>
        <tbody>{p.buys.map((b) => (
          <tr key={b.holding_id}><td className="max-w-[12rem] truncate">{b.name}<br /><Unknown items={b.costs_unknown} /></td>
            <td className="num text-right">{units(b.units)}</td><td className="num text-right">{inr(b.amount)}</td><td className="num text-right">{inr(b.stamp, 2)}</td></tr>
        ))}</tbody>
      </Table>}
      {t && <p className="text-xs text-muted">
        Sold {inr(t.sold_gross)}, bought {inr(t.bought)}. Tax this year {t.tax == null ? "unknown (a lot's term could not be determined)" : inr(t.tax)}; STT {inr(t.sell_charges, 2)}; stamp duty {inr(t.stamp_duty, 2)}.
        {t.costs_unknown.length > 0 && <> Not included, unknown: {t.costs_unknown.join("; ")}.</>}
      </p>}
    </div>
  );
}

export function OptimiserPanel() {
  const [method, setMethod] = useState<Method>("min_variance");
  const [run, setRun] = useState(false);
  const { data, error, reload } = useApi<OptimisePlan>(run ? `/api/portfolio/optimise?method=${method}` : null);
  return (
    <Card title="Optimiser (suggested trades)" icon={<Sigma className="size-4" />}
      help="Weights per holding that aim for lower risk without forecasting returns, within the units that cannot be sold (ELSS lock-in, exit-load period, unknown cost) and your position limit. A target is shown only if the method beat equal weight and your current weights out of sample.">
      <Callout tone="warn" icon={<ShieldAlert className="size-4" />} title="Suggested trades only, not investment advice">
        {data?.disclaimer ?? "No order is placed or can be placed from the app. FinResearch is not a SEBI-registered investment adviser or research analyst."}
      </Callout>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Segmented<Method> label="Method" value={method} onChange={setMethod} options={METHODS} />
        <Button variant="secondary" onClick={() => (run ? reload() : setRun(true))}>{run ? "Run again" : "Run optimiser"}</Button>
      </div>
      {!run ? <p className="mt-2 text-xs text-muted">Reads up to four years of each holding&apos;s daily prices, which can take a few minutes the first time.</p>
        : error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <div className="mt-3"><Loading what="the optimiser" /></div> : (
          <div className="mt-3 space-y-3">
            <p className="text-sm">{data.message}</p>
            {!data.complete && <p className="text-xs text-warn">Left out (no price): {data.excluded.map((x) => `${x.name} (${x.reason})`).join("; ")}.</p>}
            {!data.tax_complete && <p className="text-xs text-warn">{data.unclassified.detail}</p>}
            {data.validation && <Check v={data.validation} />}
            {data.holdings.length > 0 && <Weights rows={data.holdings} />}
            {data.risk && <p className="text-xs text-muted">Volatility of the optimised holdings: now {pct(data.risk.vol_current)}, target {pct(data.risk.vol_target)} ({data.risk.note}; shrinkage {data.risk.shrinkage.toFixed(2)}).</p>}
            <Trades p={data} />
            {data.skipped.length > 0 && <p className="text-xs text-muted">Not sellable: {data.skipped.map((s) => `${s.name}: ${units(s.units)} units, ${s.reason}`).join("; ")}.</p>}
            <ul className="list-disc pl-5 text-[11px] text-muted">{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>
          </div>
        )}
    </Card>
  );
}
