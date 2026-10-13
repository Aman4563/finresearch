"use client";

// The Strategy Lab's builder (#318): a strategy from building blocks. The server validates every combination and
// answers in words (shown as is); this form only shapes the inputs.

import { useState } from "react";

import { Button, Field, cx, inputClass } from "@/components/ui";

import type { LabResponse, LabSpec, Structure, Trend } from "./lab-types";

export const DEFAULT_SPEC: LabSpec = {
  underlying: "NIFTY", structure: "put_credit", expiry: "monthly", dte_min: 30, dte_max: 45, weekdays: [0, 1, 2, 3, 4],
  iv_measure: "percentile", iv_min: null, iv_max: null, trend: "none", strike_by: "delta", delta: 0.2, otm_pct: null,
  width_pct: 2, max_loss_pct: 5, capital: 1_000_000, max_lots: 20, max_concurrent: 2, max_per_expiry: 2, min_days_between: 5,
  profit_take_pct: 50, stop_mult: 2, exit_dte: 7, exit_before_expiry_day: true, exit_before_results: false,
};

const n = (v: string): number | null => (v.trim() === "" ? null : Number(v));
const s = (v: number | null) => (v == null ? "" : String(v));

function Num({ label, value, onChange, step = "1", hint, name }: {
  label: string; value: number | null; onChange: (v: number | null) => void; step?: string; hint?: string; name: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <input aria-label={label} name={name} type="number" step={step} className={cx(inputClass, "num w-full")} value={s(value)} onChange={(e) => onChange(n(e.target.value))} />
    </Field>
  );
}

export function StrategyForm({ blocks, underlyings, initial, submitLabel, onSubmit, busy, withName }: {
  blocks: LabResponse["blocks"]; underlyings: string[]; initial: LabSpec; submitLabel: string; busy: boolean; withName?: boolean;
  onSubmit: (spec: LabSpec, name: string) => void;
}) {
  const [f, setF] = useState<LabSpec>(initial);
  const [name, setName] = useState("");
  const set = <K extends keyof LabSpec>(k: K, v: LabSpec[K]) => setF((x) => ({ ...x, [k]: v }));
  const credit = f.structure.endsWith("credit") || f.structure === "iron_condor";
  const day = (d: number) => set("weekdays", f.weekdays.includes(d) ? f.weekdays.filter((x) => x !== d) : [...f.weekdays, d].sort());

  return (
    <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); onSubmit(f, name); }}>
      {withName && (
        <Field label="Name (optional)"><input aria-label="Name" maxLength={120} className={cx(inputClass, "w-full")} value={name} onChange={(e) => setName(e.target.value)} /></Field>
      )}
      <fieldset className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <legend className="col-span-full mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Underlying and structure</legend>
        <Field label="Underlying">
          <input aria-label="Underlying" list="lab-underlyings" className={cx(inputClass, "w-full uppercase")} value={f.underlying} onChange={(e) => set("underlying", e.target.value.toUpperCase())} />
          <datalist id="lab-underlyings">{underlyings.map((u) => <option key={u} value={u} />)}</datalist>
        </Field>
        <Field label="Structure">
          <select aria-label="Structure" className={cx(inputClass, "w-full")} value={f.structure} onChange={(e) => set("structure", e.target.value as Structure)}>
            {Object.entries(blocks.structures).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
        <Field label="Expiry">
          <select aria-label="Expiry" className={cx(inputClass, "w-full")} value={f.expiry} onChange={(e) => set("expiry", e.target.value as LabSpec["expiry"])}>
            <option value="monthly">monthly</option><option value="any">nearest (weekly where listed)</option>
          </select>
        </Field>
        <Num name="dte" label="Days to expiry: min" value={f.dte_min} onChange={(v) => set("dte_min", v ?? 0)} />
        <Num name="dtemax" label="Days to expiry: max" value={f.dte_max} onChange={(v) => set("dte_max", v ?? 0)} hint={`at most ${blocks.max_dte}`} />
      </fieldset>

      <fieldset className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <legend className="col-span-full mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Entry filters (each one is a knob that can overfit)</legend>
        <div className="col-span-full flex flex-wrap items-center gap-3 text-sm" role="group" aria-label="Entry days">
          {blocks.weekdays.map((w, i) => (
            <label key={w} className="flex items-center gap-1"><input type="checkbox" checked={f.weekdays.includes(i)} onChange={() => day(i)} />{w}</label>
          ))}
        </div>
        <Field label="IV measure">
          <select aria-label="IV measure" className={cx(inputClass, "w-full")} value={f.iv_measure} onChange={(e) => set("iv_measure", e.target.value as LabSpec["iv_measure"])}>
            <option value="percentile">IV percentile (1 year)</option><option value="rank">IV rank (1 year)</option>
          </select>
        </Field>
        <Num name="ivmin" label="IV from (0-100, blank = any)" value={f.iv_min} onChange={(v) => set("iv_min", v)} />
        <Num name="ivmax" label="IV to (0-100, blank = any)" value={f.iv_max} onChange={(v) => set("iv_max", v)} />
        <Field label="Trend">
          <select aria-label="Trend" className={cx(inputClass, "w-full")} value={f.trend} onChange={(e) => set("trend", e.target.value as Trend)}>
            {Object.entries(blocks.trends).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
      </fieldset>

      <fieldset className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <legend className="col-span-full mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Legs and sizing</legend>
        <Field label={credit ? "Short leg by" : "Bought leg by"}>
          <select aria-label="Leg by" className={cx(inputClass, "w-full")} value={f.strike_by}
            onChange={(e) => setF((x) => (e.target.value === "delta" ? { ...x, strike_by: "delta", delta: 0.2, otm_pct: null } : { ...x, strike_by: "otm_pct", delta: null, otm_pct: 3 }))}>
            <option value="delta">delta</option><option value="otm_pct">% out of the money</option>
          </select>
        </Field>
        {f.strike_by === "delta"
          ? <Num name="delta" label="|Delta|" step="0.01" value={f.delta} onChange={(v) => set("delta", v)} />
          : <Num name="otm" label="% out of the money" step="0.1" value={f.otm_pct} onChange={(v) => set("otm_pct", v)} />}
        <Num name="width" label="Wing width, % of forward" step="0.1" value={f.width_pct} onChange={(v) => set("width_pct", v ?? 0)} />
        <Num name="maxloss" label="Max loss per trade, % of capital" step="0.1" value={f.max_loss_pct} onChange={(v) => set("max_loss_pct", v ?? 0)} />
        <Num name="capital" label="Capital (₹)" step="10000" value={f.capital} onChange={(v) => set("capital", v ?? 0)} />
        <Num name="conc" label="Open at once" value={f.max_concurrent} onChange={(v) => set("max_concurrent", v ?? 1)} />
        <Num name="perexp" label="Per expiry" value={f.max_per_expiry} onChange={(v) => set("max_per_expiry", v ?? 1)} />
        <Num name="gap" label="Days between entries" value={f.min_days_between} onChange={(v) => set("min_days_between", v ?? 1)} />
      </fieldset>

      <fieldset className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <legend className="col-span-full mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Management</legend>
        <Num name="pt" label={`Profit-take, % of the ${credit ? "credit" : "debit"}`} value={f.profit_take_pct} onChange={(v) => set("profit_take_pct", v)} />
        <Num name="stop" label={credit ? "Stop, × the credit" : "Stop, fraction of the debit"} step="0.1" value={f.stop_mult} onChange={(v) => set("stop_mult", v)} />
        <Num name="exitdte" label="Exit at days to expiry" value={f.exit_dte} onChange={(v) => set("exit_dte", v)} />
        <div className="space-y-1 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.exit_before_expiry_day} onChange={(e) => set("exit_before_expiry_day", e.target.checked)} />Exit before expiry day</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.exit_before_results} onChange={(e) => set("exit_before_results", e.target.checked)} />Exit before results (stocks)</label>
        </div>
      </fieldset>
      {f.exit_before_results && <p className="text-xs text-muted">{blocks.results_note}</p>}
      <Button type="submit" disabled={busy}>{submitLabel}</Button>
    </form>
  );
}
