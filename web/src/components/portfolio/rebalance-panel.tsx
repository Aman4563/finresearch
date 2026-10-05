"use client";

// Rebalance (feature #8): new money to the underweight classes first, then a tax-aware FIFO sell list only when a
// class is outside its band, then buys by class. Illustrative only, not advice (SEBI IA / RA rules). The arithmetic
// is portfolio/rebalance.py; this card only shows it.

import { Scale, ShieldAlert } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Callout, Card, ErrorNote, Field, SkeletonRows, Stat, Table, cx, inputClass } from "@/components/ui";
import { useApi } from "@/lib/api";

import { inr, signed, units } from "./types";

type AllocRow = {
  label: string; target_pct: number; band_pp: number; before_pct: number; after_cash_pct: number; after_pct: number;
  outside_before: boolean; outside_after_cash: boolean; outside_after: boolean;
};
type ClassMove = { asset_class: string; amount: number; stamp: number; examples: string[] };
type SellStep = {
  step: number; holding_id: number; name: string; account: string; asset_class: string; category: string; units: number;
  price: number; gross: number; charges: number; exit_load: number; net: number; gain: number; short_term: number;
  long_term: number; tax: number; notes: string[];
  lots: { acquired: string | null; units: number; gain: number | null; term: string | null }[];
};
export type RebalancePlan = {
  status: "no_targets" | "no_value" | "within_bands" | "cash_only" | "rebalance"; message: string; disclaimer: string;
  fy_label: string; slab_pct?: number; bands: { abs_pp: number; rel_pct: number }; new_money: number;
  allocation?: AllocRow[]; cash_flow?: ClassMove[]; sells?: SellStep[]; buys?: ClassMove[];
  unfilled?: { asset_class: string; amount: number; why: string[] }[];
  skipped?: { holding_id: number; name: string; asset_class: string; units: number; reason: string }[];
  totals?: { sold_gross: number; sell_charges: number; stamp_duty: number; exit_load: number; gain: number; tax: number;
    bought: number; exemption_before: number; exemption_after: number; tax_so_far: number };
  unpriced: string[]; assumptions: string[]; sources: string[];
};

export function RebalancePanel() {
  const [form, setForm] = useState({ money: "", abs: "5", rel: "25" });
  const [q, setQ] = useState("new_money=0&band_abs_pp=5&band_rel_pct=25");
  const { data, error } = useApi<RebalancePlan>(`/api/portfolio/rebalance?${q}`);
  const apply = () => setQ(new URLSearchParams({
    new_money: String(Number(form.money) || 0), band_abs_pp: String(Number(form.abs) || 5), band_rel_pct: String(Number(form.rel) || 0),
  }).toString());
  return (
    <Card title="Rebalance (illustrative)" icon={<Scale className="size-4" />}
      help="New money goes to the underweight classes first (no sale, no tax). Only if a class is still outside its band are holdings sold, oldest lots first (FIFO), cheapest tax first; the proceeds are then bought into the underweight classes.">
      <Callout tone="warn" icon={<ShieldAlert className="size-4" />} title="Illustrative, not investment advice">
        {data?.disclaimer ?? "FinResearch is not a SEBI-registered investment adviser or research analyst. These steps only show what your own targets imply under the assumptions listed below."}
      </Callout>
      <div className="mt-3 grid gap-3 sm:grid-cols-4">
        <Field label="New money (₹, optional)"><input inputMode="decimal" value={form.money} placeholder="e.g. a SIP instalment" onChange={(e) => setForm({ ...form, money: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Band: absolute (pp)"><input inputMode="decimal" value={form.abs} onChange={(e) => setForm({ ...form, abs: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Band: relative (% of target)"><input inputMode="decimal" value={form.rel} onChange={(e) => setForm({ ...form, rel: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
        <div className="flex items-end"><Button onClick={apply}>Update plan</Button></div>
      </div>
      <ErrorNote error={error} />
      {!data && !error && <SkeletonRows rows={4} />}
      {data && <PlanBody p={data} />}
    </Card>
  );
}

function Bars({ rows }: { rows: AllocRow[] }) {
  // one row per class: today, then after the plan, with the target as a tick and the band as a shaded range
  const bar = (pct: number, cls: string) => <div className={cx("h-2 rounded-full", cls)} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />;
  return (
    <div className="mt-4 space-y-3" role="list" aria-label="Allocation before and after the plan">
      {rows.map((r) => (
        <div key={r.label} role="listitem" className="text-xs">
          <div className="flex flex-wrap justify-between gap-2">
            <span className="font-medium">{r.label}</span>
            <span className="num text-muted">target {r.target_pct.toFixed(1)}% ± {r.band_pp.toFixed(1)} pp · now <span className={cx(r.outside_before && "font-medium text-warn")}>{r.before_pct.toFixed(1)}%{r.outside_before ? " (outside)" : ""}</span> → after <span className={cx(r.outside_after && "font-medium text-warn")}>{r.after_pct.toFixed(1)}%</span></span>
          </div>
          <div className="relative mt-1 space-y-1 rounded bg-background-subtle p-1">
            <div className="absolute inset-y-0 bg-brand/10" style={{ left: `${Math.max(0, r.target_pct - r.band_pp)}%`, width: `${Math.min(100, 2 * r.band_pp)}%` }} aria-hidden />
            <div className="absolute inset-y-0 w-0.5 bg-foreground/60" style={{ left: `${Math.min(100, r.target_pct)}%` }} aria-hidden />
            {bar(r.before_pct, "relative bg-muted/60")}
            {bar(r.after_pct, "relative bg-brand")}
          </div>
        </div>
      ))}
      <p className="text-[11px] text-muted">Grey: today. Coloured: after the plan. Tick: target; shaded: the band.</p>
    </div>
  );
}

function Moves({ title, rows }: { title: string; rows: ClassMove[] }) {
  if (rows.length === 0) return null;
  return (
    <Table label={title} className="mt-4">
      <thead><tr><th>{title}</th><th className="text-right">Amount</th><th className="text-right">Stamp duty</th><th>You already hold in this class</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.asset_class}><td>{r.asset_class}</td><td className="num text-right font-medium">{inr(r.amount)}</td><td className="num text-right text-muted">{inr(r.stamp, 2)}</td>
          <td className="text-xs text-muted">{r.examples.length ? r.examples.join(", ") : "nothing yet"}</td></tr>
      ))}</tbody>
    </Table>
  );
}

function Sells({ rows }: { rows: SellStep[] }) {
  if (rows.length === 0) return null;
  return (
    <Table label="Sell steps" className="mt-4">
      <thead><tr><th>#</th><th>Sell</th><th className="text-right">Units</th><th className="text-right">Proceeds</th><th className="text-right">Gain</th><th className="text-right">Tax</th><th className="text-right">Charges</th><th className="text-right">Exit load</th></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.step}>
          <td className="num text-muted">{s.step}</td>
          <td className="max-w-[18rem]"><span className="block truncate font-medium">{s.name}</span>
            <span className="text-[11px] text-muted">{s.account} · {s.asset_class} · <Badge tone={s.gain < 0 ? "gain" : s.category.includes("within") ? "info" : "warn"}>{s.category}</Badge> · oldest lots first: {s.lots.map((l) => `${units(l.units)} from ${l.acquired ?? "?"}`).join(", ")}{s.notes.length ? ` · ${s.notes.join("; ")}` : ""}</span></td>
          <td className="num text-right">{units(s.units)}</td>
          <td className="num text-right">{inr(s.gross)}</td>
          <td className={cx("num text-right", s.gain < 0 ? "text-loss" : "text-gain")}>{signed(s.gain)}</td>
          <td className="num text-right font-medium">{inr(s.tax)}</td>
          <td className="num text-right text-muted">{inr(s.charges, 2)}</td>
          <td className="num text-right text-muted">{inr(s.exit_load)}</td>
        </tr>
      ))}</tbody>
    </Table>
  );
}

function PlanBody({ p }: { p: RebalancePlan }) {
  const t = p.totals;
  return (
    <div className="mt-4">
      <p className="text-sm">{p.message}</p>
      {p.unpriced.length > 0 && <p className="mt-1 text-xs text-warn">Not in the allocation (no price): {p.unpriced.join(", ")}.</p>}
      {t && (p.sells?.length || p.cash_flow?.length) ? (
        <div className="mt-3 grid gap-3 sm:grid-cols-4">
          <Stat label="Sold" value={t.sold_gross} format={(n) => inr(n)} />
          <Stat label={`Tax this year (${p.fy_label})`} value={t.tax} format={(n) => inr(n)} hint={p.slab_pct != null ? `incl. 4 % cess; slab ${p.slab_pct} %` : "incl. 4 % cess"} />
          <Stat label="Charges" value={t.sell_charges + t.stamp_duty} format={(n) => inr(n, 2)} hint="STT + stamp duty" />
          <Stat label="LTCG exemption left" value={t.exemption_after} format={(n) => inr(n)} hint={`${inr(t.exemption_before)} before`} />
        </div>
      ) : null}
      {p.allocation && <Bars rows={p.allocation} />}
      <Moves title="1. New money to" rows={p.cash_flow ?? []} />
      <Sells rows={p.sells ?? []} />
      <Moves title={p.sells?.length ? "3. Then buy (by class)" : "Buy (by class)"} rows={p.buys ?? []} />
      {(p.unfilled ?? []).map((u) => (
        <p key={u.asset_class} className="mt-2 text-xs text-warn">{u.asset_class}: {inr(u.amount)} more could not be sold ({u.why.join("; ")}).</p>
      ))}
      {(p.skipped ?? []).length > 0 && (
        <details className="mt-3 text-xs"><summary className="cursor-pointer text-muted">Not sold ({p.skipped!.length})</summary>
          <ul className="mt-1 list-disc pl-5 text-muted">{p.skipped!.map((s) => <li key={s.holding_id}>{s.name}: {units(s.units)} units, {s.reason}</li>)}</ul>
        </details>
      )}
      <details className="mt-3 text-xs"><summary className="cursor-pointer text-muted">Assumptions and sources</summary>
        <ul className="mt-1 list-disc pl-5 text-muted">{p.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
        <ul className="mt-2 list-disc pl-5 text-muted">{p.sources.map((a) => <li key={a}>{a}</li>)}</ul>
      </details>
    </div>
  );
}
