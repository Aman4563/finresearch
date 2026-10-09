"use client";

// Goals: a seeded Monte Carlo per goal (Python, /api/wealth/goals/{id}/plan). Shows P(success) with its range, a
// P10/P50/P90 fan against the inflated target, and the SIP the numbers imply for 75 % / 90 %.

import { Plus, SlidersHorizontal, Target } from "lucide-react";
import { useState } from "react";

import { TimeSeriesChart, fmtCompactINR } from "@/components/charts";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, Field, InfoTip, Modal, SkeletonRows, cx, inputClass } from "@/components/ui";
import { api, day, useApi } from "@/lib/api";

import { ASSET_KIND_LABEL, type Assumptions, type Goal, type GoalPlan, type Wealth, inr, num, prob } from "./types";

const s = (v: number | string | null | undefined) => (v == null ? "" : String(v));

function addMonths(iso: string, m: number) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCMonth(d.getUTCMonth() + m);
  return d.toISOString().slice(0, 10);
}

function GoalForm({ g, w, onSaved, close }: { g: Goal | null; w: Wealth; onSaved: () => void; close: () => void }) {
  const [f, setF] = useState({
    name: g?.name ?? "", target_inr: s(g?.target_inr), target_date: g?.target_date ?? "", priority: g?.priority ?? "medium",
    inflation_pct: s(g?.inflation_pct ?? w.assumptions.inflation_pct), current_inr: s(g?.current_inr ?? 0), monthly_sip: s(g?.monthly_sip ?? 0),
    step_up_pct: s(g?.step_up_pct ?? 0), linked: g?.linked_asset_ids ?? [], portfolio_pct: s(g?.portfolio_pct ?? 0),
    equity_pct: s(g?.equity_pct), gold_pct: s(g?.gold_pct ?? 0), in_cover: g?.in_cover ?? false, notes: g?.notes ?? "",
  });
  const [msg, setMsg] = useState<string | null>(null);
  const set = (k: keyof typeof f, v: unknown) => setF({ ...f, [k]: v });
  const req = async (method: "POST" | "PUT" | "DELETE", body?: unknown) => {
    setMsg(null);
    try {
      await api(g ? `/api/wealth/goals/${g.id}` : "/api/wealth/goals", { method, body: body ? JSON.stringify(body) : undefined });
      onSaved(); close();
    } catch (e) { setMsg((e as Error).message); }
  };
  const save = () => req(g ? "PUT" : "POST", {
    name: f.name, target_inr: num(f.target_inr), target_date: f.target_date, priority: f.priority, inflation_pct: num(f.inflation_pct) ?? "6",
    current_inr: num(f.current_inr) ?? "0", monthly_sip: num(f.monthly_sip) ?? "0", step_up_pct: num(f.step_up_pct) ?? "0",
    linked_asset_ids: f.linked, portfolio_pct: num(f.portfolio_pct) ?? "0", equity_pct: num(f.equity_pct), gold_pct: num(f.gold_pct) ?? "0", in_cover: f.in_cover, notes: f.notes || null,
  });
  const n = (k: keyof typeof f, label: React.ReactNode, hint?: string) => (
    <Field label={label} hint={hint}><input inputMode="decimal" value={f[k] as string} onChange={(e) => set(k, e.target.value)} className={cx(inputClass, "w-full num")} /></Field>
  );
  return (
    <div className="max-h-[75vh] space-y-4 overflow-y-auto px-4 py-4 sm:px-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Goal"><input value={f.name} onChange={(e) => set("name", e.target.value)} className={cx(inputClass, "w-full")} placeholder="e.g. Child's education" /></Field>
        {n("target_inr", "Target in today's ₹")}
        <Field label="Target date"><input type="date" value={f.target_date} onChange={(e) => set("target_date", e.target.value)} className={cx(inputClass, "w-full")} /></Field>
        <Field label="Priority"><select value={f.priority} onChange={(e) => set("priority", e.target.value)} className={cx(inputClass, "w-full")}>
          <option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></Field>
        {n("inflation_pct", <span className="inline-flex items-center gap-1">Inflation for this goal, % <InfoTip>General CPI ≈ 5–6 % long-run [unverified]; education and medical costs usually rise faster (rule of thumb).</InfoTip></span>)}
        {n("monthly_sip", "Monthly SIP towards it (₹)")}
        {n("step_up_pct", "SIP step-up each year, %")}
        {n("current_inr", "Other savings for it (₹)", "Money not entered as an asset")}
        {n("portfolio_pct", "Share of the portfolio earmarked, %", `Portfolio ${w.portfolio.value == null ? "unknown" : inr(w.portfolio.value)}${w.portfolio.day ? ` on ${day(w.portfolio.day)}` : " (no snapshot yet)"}${w.portfolio.value != null && w.portfolio.complete === false ? " (incomplete)" : ""}`)}
        {n("equity_pct", "Equity %, fixed", "Blank = glide by time left (rule of thumb)")}
        {n("gold_pct", "Gold %")}
      </div>
      {w.assets.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-medium text-muted">Assets set aside for this goal</p>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {w.assets.map((a) => (
              <label key={a.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={f.linked.includes(a.id)} onChange={(e) => set("linked", e.target.checked ? [...f.linked, a.id] : f.linked.filter((i) => i !== a.id))} />
                <span className="truncate">{a.name}</span><span className="text-[11px] text-muted">{ASSET_KIND_LABEL[a.kind]} · {inr(a.valuation.value)}</span>
              </label>
            ))}
          </div>
        </div>
      )}
      <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={f.in_cover} onChange={(e) => set("in_cover", e.target.checked)} />
        <span>Count this goal in the term-cover need<span className="block text-[11px] text-muted">For goals your family would still need if your income stopped (e.g. a child&apos;s education); not for your own retirement.</span></span></label>
      {msg && <p className="text-xs text-loss">{msg}</p>}
      <div className="flex flex-wrap gap-2">
        <Button onClick={save} disabled={!f.name.trim() || !f.target_inr || !f.target_date}>{g ? "Save" : "Add goal"}</Button>
        {g && <Button variant="ghost" onClick={() => confirm(`Delete ${g.name}?`) && req("DELETE")}>Delete</Button>}
      </div>
    </div>
  );
}

function PlanView({ g, w, refresh }: { g: Goal; w: Wealth; refresh: number }) {
  const { data: p, error, reload } = useApi<GoalPlan>(g.months_left > 0 ? `/api/wealth/goals/${g.id}/plan?r=${refresh}` : null);
  if (g.months_left <= 0) return <Callout tone="warn">The goal date is less than a month away; there is nothing to simulate.</Callout>;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!p) return <SkeletonRows rows={4} />;
  if (p.funded_complete === false && p.bound !== "lower") {
    // never a P(success) or a "SIP for 75 %" computed from ₹0 or from part of the money (#262), unless that part is a
    // true lower bound (#286, below)
    return <Callout tone="warn" title={`Set aside today: ${p.start == null ? "unknown" : `${inr(p.start)} or more (incomplete)`}`}>{p.message}</Callout>;
  }
  const fan = p.fan.map((r) => ({ date: addMonths(w.as_of, r.month), p10: r.p10, p50: r.p50, p90: r.p90, target: r.target }));
  // a lower bound (#286): run on the priced part only, so chances and outcomes read "at least", SIPs "at most"
  const lb = p.bound === "lower";
  const least = (s: string) => (lb ? `at least ${s}` : s);
  const sip = (v: number | null) => (v == null ? (lb ? "unknown" : "not reachable") : lb ? `at most ${inr(v)}` : inr(v));
  const tone = p.p_success >= 0.75 ? "gain" : lb ? "neutral" : p.p_success >= 0.5 ? "warn" : "loss";
  const unpriced = p.unpriced ?? [];
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
      {lb && (
        <div className="lg:col-span-2">
          <Callout tone="warn" title="Lower bound: part of the earmarked portfolio has no price today">
            {unpriced.length} holding{unpriced.length === 1 ? "" : "s"} without a price {unpriced.length === 1 ? "is" : "are"} counted as nothing: {unpriced.join(", ")}
            {" "}({p.unpriced_share_pct == null ? "share of the earmark unknown: no earlier price" : `about ${p.unpriced_share_pct.toFixed(1)} % of the earmarked portfolio at their last known prices`}).
            {" "}So the money set aside, the chances and the outcomes below are at least what is shown, and the SIPs needed at most.
          </Callout>
        </div>
      )}
      <div className="space-y-3">
        <div>
          <p className="flex items-center gap-1 text-xs text-muted">P(goal met) <InfoTip>Share of {p.n.toLocaleString("en-IN")} simulated paths that reach the inflated target. The range in brackets is the simulation&apos;s own 95 % interval; the haircut row shows the same with equity returns 2 pp lower. A model output under stated assumptions, not a forecast.</InfoTip></p>
          <p className="num text-3xl font-semibold"><span className={cx(tone === "gain" ? "text-gain" : tone === "warn" ? "text-warn" : tone === "loss" ? "text-loss" : "")}>{lb && <span className="text-base font-normal text-muted">at least </span>}{prob(p.p_success)}</span>
            <span className="ml-2 text-sm font-normal text-muted">({prob(p.p_ci[0])}–{prob(p.p_ci[1])})</span></p>
          <p className="text-xs text-muted">Equity −2 pp: <span className="num">{least(prob(p.p_haircut))}</span> · uncalibrated model</p>
        </div>
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
          <dt className="text-muted">Target on {day(g.target_date)}</dt><dd className="num text-right">{inr(p.target_nominal)}</dd>
          <dt className="text-muted">Set aside today</dt><dd className="num text-right">{least(inr(p.start))}</dd>
          <dt className="text-muted">SIP now (step-up {p.step_up_pct} %)</dt><dd className="num text-right">{inr(p.sip0)}</dd>
          <dt className="text-muted">SIP for 75 %</dt><dd className="num text-right">{sip(p.sip_for_75)}</dd>
          <dt className="text-muted">SIP for 90 %</dt><dd className="num text-right">{sip(p.sip_for_90)}</dd>
          <dt className="text-muted">One year later</dt><dd className="num text-right">{least(prob(p.p_plus_year))}</dd>
          <dt className="text-muted">Step-up +5 pp</dt><dd className="num text-right">{least(prob(p.p_step_up_plus5))}</dd>
          <dt className="text-muted">Outcome P10 / P50 / P90</dt><dd className="num text-right">{lb ? "at least " : ""}{fmtCompactINR(p.terminal_pcts.p10)} / {fmtCompactINR(p.terminal_pcts.p50)} / {fmtCompactINR(p.terminal_pcts.p90)}</dd>
        </dl>
        <p className="text-sm">{p.message}</p>
      </div>
      <div>
        <TimeSeriesChart data={fan} area={false} showChange={false} height={220} format={fmtCompactINR}
          series={[{ key: "p90", label: "P90", color: "var(--chart-2)", dashed: true }, { key: "p50", label: "P50 (median)", color: "var(--chart-1)" },
            { key: "p10", label: "P10", color: "var(--chart-2)", dashed: true }, { key: "target", label: "Target (inflated)", color: "var(--warn)" }]} />
        <p className="mt-2 text-[11px] text-muted">{p.method} Weights: {p.equity_path}.</p>
      </div>
    </div>
  );
}

function AssumptionsForm({ a, onSaved }: { a: Assumptions; onSaved: () => void }) {
  const [f, setF] = useState({
    equity_mu: s(a.equity.mu_pct), equity_sd: s(a.equity.sigma_pct), debt_mu: s(a.debt.mu_pct), debt_sd: s(a.debt.sigma_pct),
    gold_mu: s(a.gold.mu_pct), gold_sd: s(a.gold.sigma_pct), inflation: s(a.inflation_pct), seed: s(a.seed), n: s(a.n),
  });
  const [msg, setMsg] = useState<string | null>(null);
  const put = async (body: unknown) => {
    setMsg(null);
    try { await api("/api/wealth/assumptions", { method: "PUT", body: JSON.stringify(body) }); setMsg("Saved"); onSaved(); } catch (e) { setMsg((e as Error).message); }
  };
  const save = () => put({
    equity: { mu_pct: Number(f.equity_mu), sigma_pct: Number(f.equity_sd) }, debt: { mu_pct: Number(f.debt_mu), sigma_pct: Number(f.debt_sd) },
    gold: { mu_pct: Number(f.gold_mu), sigma_pct: Number(f.gold_sd) }, inflation_pct: Number(f.inflation), seed: Number(f.seed), n: Number(f.n),
  });
  const box = (k: keyof typeof f, label: string) => (
    <Field label={label}><input inputMode="decimal" value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
  );
  return (
    <Card title="Assumptions" icon={<SlidersHorizontal className="size-4" />} help="Used by the goal simulations, the term-cover discount rate (debt return and inflation) and the prepay-or-invest comparison. They are assumptions, not forecasts; change them to see how sensitive the answers are.">
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {box("equity_mu", "Equity return %")}{box("equity_sd", "Equity volatility %")}
        {box("debt_mu", "Debt return %")}{box("debt_sd", "Debt volatility %")}
        {box("gold_mu", "Gold return %")}{box("gold_sd", "Gold volatility %")}
        {box("inflation", "Inflation %")}{box("seed", "Random seed")}{box("n", "Paths")}
      </div>
      <p className="mt-2 text-[11px] text-muted">Defaults: equity {a.equity.default.mu_pct} % / {a.equity.default.sigma_pct} %, debt {a.debt.default.mu_pct} % / {a.debt.default.sigma_pct} %, gold {a.gold.default.mu_pct} % / {a.gold.default.sigma_pct} % (round long-run assumptions, not measured from data). The same seed always gives the same numbers.</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button onClick={save}>Save assumptions</Button><Button variant="ghost" onClick={() => put({})}>Restore defaults</Button>
        {msg && <span className="text-xs text-muted">{msg}</span>}
      </div>
    </Card>
  );
}

export function GoalsPanel({ w, onChanged, refresh }: { w: Wealth; onChanged: () => void; refresh: number }) {
  const [edit, setEdit] = useState<Goal | "new" | null>(null);
  return (
    <div className="space-y-4">
      {w.goals.length === 0 ? (
        <EmptyState icon={<Target className="size-5" />} title="No goals yet" action={<Button icon={<Plus className="size-3.5" />} onClick={() => setEdit("new")}>Add a goal</Button>}>
          Add a goal (amount in today&apos;s rupees and a date) to see the chance your savings and SIP reach it, and the SIP the numbers imply.
        </EmptyState>
      ) : (
        <>
          <div className="flex justify-end"><Button icon={<Plus className="size-3.5" />} onClick={() => setEdit("new")}>Add a goal</Button></div>
          {w.goals.map((g) => (
            <Card key={g.id} title={<>{g.name} <Badge tone={g.priority === "high" ? "brand" : "neutral"}>{g.priority}</Badge></>}
              subtitle={`${inr(g.target_inr)} in today's rupees by ${day(g.target_date)} · ${Math.floor(g.months_left / 12)} y ${g.months_left % 12} m left`}
              icon={<Target className="size-4" />} actions={<Button variant="ghost" onClick={() => setEdit(g)}>Edit</Button>}>
              {g.shared_links.length > 0 && <div className="mb-3"><Callout tone="warn">An asset linked here is also linked to another goal, so it is counted twice.</Callout></div>}
              <PlanView g={g} w={w} refresh={refresh} />
            </Card>
          ))}
        </>
      )}
      <AssumptionsForm key={JSON.stringify(w.assumptions)} a={w.assumptions} onSaved={onChanged} />
      <Modal wide open={edit != null} onClose={() => setEdit(null)} title={edit === "new" ? "Add a goal" : edit ? `Edit ${edit.name}` : null}>
        {edit != null && <GoalForm key={edit === "new" ? "new" : edit.id} g={edit === "new" ? null : edit} w={w} onSaved={onChanged} close={() => setEdit(null)} />}
      </Modal>
    </div>
  );
}
