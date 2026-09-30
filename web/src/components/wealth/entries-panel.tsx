"use client";

// Manual assets, loans and insurance policies: tables with add / edit / delete. Values are the user's own entries;
// FD/RD/EPF/PPF values are computed from their rates (the method is shown per row).

import { Landmark, Plus, Shield, Wallet } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Card, EmptyState, Field, InfoTip, Modal, Table, cx, inputClass } from "@/components/ui";
import { api, day } from "@/lib/api";

import { ASSET_KIND_LABEL, type Asset, type AssetKind, LOAN_KIND_LABEL, type Loan, type LoanKind, type Policy, type Wealth, inr, num, pc, prob } from "./types";

const s = (v: number | string | null | undefined) => (v == null ? "" : String(v));

function Err({ msg }: { msg: string | null }) {
  return msg ? <p className="text-xs text-loss">{msg}</p> : null;
}

function useSave(onSaved: () => void, close: () => void) {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (path: string, method: "POST" | "PUT" | "DELETE", body?: unknown) => {
    setMsg(null); setBusy(true);
    try {
      await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
      onSaved(); close();
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };
  return { msg, busy, run };
}

// ------------------------------------------------------------------ assets
function AssetForm({ a, w, onSaved, close }: { a: Asset | null; w: Wealth; onSaved: () => void; close: () => void }) {
  const [f, setF] = useState({
    kind: (a?.kind ?? "cash") as AssetKind, name: a?.name ?? "", institution: a?.institution ?? "", asset_class: a?.asset_class ?? "",
    principal: s(a?.principal), rate_pct: s(a?.rate_pct), compounding: String(a?.compounding ?? 4), monthly_contribution: s(a?.monthly_contribution),
    start_date: a?.start_date ?? "", maturity_date: a?.maturity_date ?? "", liquid: a?.liquid ?? false, equity_pct: s(a?.equity_pct),
    notes: a?.notes ?? "", value: "", value_date: w.as_of,
  });
  const { msg, busy, run } = useSave(onSaved, close);
  const set = (k: keyof typeof f, v: string | boolean) => setF({ ...f, [k]: v });
  const formula = f.kind === "fd" || f.kind === "rd";
  const provident = f.kind === "epf" || f.kind === "ppf";
  const rateHint = w.rates[f.kind];
  const save = () => run(a ? `/api/wealth/assets/${a.id}` : "/api/wealth/assets", a ? "PUT" : "POST", {
    kind: f.kind, name: f.name, institution: f.institution || null, asset_class: f.kind === "other" ? f.asset_class || null : null,
    principal: num(f.principal), rate_pct: num(f.rate_pct), compounding: Number(f.compounding), monthly_contribution: num(f.monthly_contribution),
    start_date: f.start_date || null, maturity_date: f.maturity_date || null, liquid: f.liquid, equity_pct: f.kind === "nps" ? num(f.equity_pct) : null,
    notes: f.notes || null, value: formula ? null : num(f.value), value_date: f.value_date || null,
  });
  return (
    <div className="space-y-4 px-4 py-4 sm:px-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Type">
          <select value={f.kind} onChange={(e) => set("kind", e.target.value)} className={cx(inputClass, "w-full")} disabled={!!a}>
            {(Object.keys(ASSET_KIND_LABEL) as AssetKind[]).map((k) => <option key={k} value={k}>{ASSET_KIND_LABEL[k]}</option>)}
          </select>
        </Field>
        <Field label="Name"><input value={f.name} onChange={(e) => set("name", e.target.value)} className={cx(inputClass, "w-full")} placeholder="e.g. Salary account" /></Field>
        {(f.kind === "cash" || f.kind === "fd" || f.kind === "rd" || f.kind === "nps" || f.kind === "ppf") && (
          <Field label={<span className="inline-flex items-center gap-1">{f.kind === "nps" ? "CRA / provider" : "Bank"} <InfoTip>A label only (never an account number). Deposits at the same bank are added up for the ₹5 lakh DICGC check.</InfoTip></span>}>
            <input value={f.institution} onChange={(e) => set("institution", e.target.value)} className={cx(inputClass, "w-full")} placeholder="e.g. HDFC Bank" />
          </Field>
        )}
        {f.kind === "other" && (
          <Field label="Counts as">
            <select value={f.asset_class} onChange={(e) => set("asset_class", e.target.value)} className={cx(inputClass, "w-full")}>
              <option value="">Other</option>{["Equity", "Debt", "Gold", "Cash", "Real estate"].map((c) => <option key={c}>{c}</option>)}
            </select>
          </Field>
        )}
        {f.kind === "fd" && <Field label="Principal (₹)"><input inputMode="decimal" value={f.principal} onChange={(e) => set("principal", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>}
        {f.kind === "rd" && <Field label="Monthly instalment (₹)"><input inputMode="decimal" value={f.monthly_contribution} onChange={(e) => set("monthly_contribution", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>}
        {(formula || provident) && (
          <Field label="Rate (% a year)" hint={rateHint ? `${rateHint.rate_pct} % (${rateHint.as_of}); ${rateHint.note}` : undefined}>
            <input inputMode="decimal" value={f.rate_pct} onChange={(e) => set("rate_pct", e.target.value)} className={cx(inputClass, "w-full num")} placeholder={rateHint?.rate_pct} />
          </Field>
        )}
        {f.kind === "fd" && (
          <Field label={<span className="inline-flex items-center gap-1">Compounding <InfoTip>Quarterly is the usual bank convention for cumulative FDs [unverified: check your FD advice]. &quot;Interest paid out&quot; keeps the value at the principal.</InfoTip></span>}>
            <select value={f.compounding} onChange={(e) => set("compounding", e.target.value)} className={cx(inputClass, "w-full")}>
              <option value="4">Quarterly</option><option value="12">Monthly</option><option value="2">Half-yearly</option><option value="1">Yearly</option><option value="0">Interest paid out</option>
            </select>
          </Field>
        )}
        {formula && <Field label="Start date"><input type="date" value={f.start_date} onChange={(e) => set("start_date", e.target.value)} className={cx(inputClass, "w-full")} /></Field>}
        {(formula || f.kind === "sgb" || f.kind === "ppf") && <Field label="Maturity date"><input type="date" value={f.maturity_date} onChange={(e) => set("maturity_date", e.target.value)} className={cx(inputClass, "w-full")} /></Field>}
        {(provident || f.kind === "nps") && <Field label="Monthly contribution (₹)"><input inputMode="decimal" value={f.monthly_contribution} onChange={(e) => set("monthly_contribution", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>}
        {f.kind === "nps" && <Field label="Equity share (E), %" hint="Blank = 50 % assumed"><input inputMode="decimal" value={f.equity_pct} onChange={(e) => set("equity_pct", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>}
        {!formula && (
          <>
            <Field label={a ? "New value (₹), optional" : provident ? "Balance (₹)" : "Current value (₹)"} hint={a?.valuation.as_of ? `Last: ${inr(a.valuation.value)} on ${day(a.valuation.as_of)}` : undefined}>
              <input inputMode="decimal" value={f.value} onChange={(e) => set("value", e.target.value)} className={cx(inputClass, "w-full num")} />
            </Field>
            <Field label="Value as of"><input type="date" value={f.value_date} onChange={(e) => set("value_date", e.target.value)} className={cx(inputClass, "w-full")} /></Field>
          </>
        )}
      </div>
      {f.kind !== "real_estate" && f.kind !== "cash" && (
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={f.liquid} onChange={(e) => set("liquid", e.target.checked)} />
          Counts towards the emergency fund{(f.kind === "fd" || f.kind === "rd") && " (breakable: counted at 90 %)"}</label>
      )}
      <Field label="Notes"><input value={f.notes} onChange={(e) => set("notes", e.target.value)} className={cx(inputClass, "w-full")} /></Field>
      <Err msg={msg} />
      <div className="flex flex-wrap gap-2">
        <Button onClick={save} disabled={busy || !f.name.trim()}>{a ? "Save" : "Add asset"}</Button>
        {a && <Button variant="ghost" onClick={() => confirm(`Delete ${a.name}?`) && run(`/api/wealth/assets/${a.id}`, "DELETE")}>Delete</Button>}
      </div>
    </div>
  );
}

function Assets({ w, onChanged }: { w: Wealth; onChanged: () => void }) {
  const [edit, setEdit] = useState<Asset | "new" | null>(null);
  return (
    <Card padded={false} title="Assets outside the portfolio" icon={<Wallet className="size-4" />}
      subtitle="Deposits, retirement accounts, gold and property. Values older than 90 days are flagged."
      actions={<Button icon={<Plus className="size-3.5" />} onClick={() => setEdit("new")}>Add</Button>}>
      {w.assets.length === 0 ? (
        <div className="px-4 pb-4 sm:px-5"><EmptyState title="No assets entered">Add your savings account, FDs, EPF/PPF, NPS, gold or property to see your whole net worth.</EmptyState></div>
      ) : (
        <Table className="!mx-0">
          <thead><tr><th>Asset</th><th>Type</th><th className="text-right">Value</th><th>How it is valued</th><th /></tr></thead>
          <tbody>{w.assets.map((a) => (
            <tr key={a.id}>
              <td className="max-w-[14rem]"><span className="block truncate font-medium">{a.name}</span><span className="block truncate text-[11px] text-muted">{a.institution ?? ""}</span></td>
              <td className="text-xs">{ASSET_KIND_LABEL[a.kind]}{a.liquid && <Badge tone="info">liquid</Badge>}</td>
              <td className="num text-right font-medium">{inr(a.valuation.value)}</td>
              <td className="max-w-[18rem] text-[11px] text-muted">{a.valuation.method}{a.valuation.stale && <> <Badge tone="warn">over 90 days old</Badge></>}</td>
              <td className="text-right"><Button variant="ghost" onClick={() => setEdit(a)}>Edit</Button></td>
            </tr>
          ))}</tbody>
        </Table>
      )}
      <Modal wide open={edit != null} onClose={() => setEdit(null)} title={edit === "new" ? "Add an asset" : edit ? `Edit ${edit.name}` : null}>
        {edit != null && <AssetForm key={edit === "new" ? "new" : edit.id} a={edit === "new" ? null : edit} w={w} onSaved={onChanged} close={() => setEdit(null)} />}
      </Modal>
    </Card>
  );
}

// ------------------------------------------------------------------ loans
function LoanForm({ l, w, onSaved, close }: { l: Loan | null; w: Wealth; onSaved: () => void; close: () => void }) {
  const [f, setF] = useState({
    kind: (l?.kind ?? "home") as LoanKind, name: l?.name ?? "", lender: l?.lender ?? "", principal: s(l?.principal), rate_pct: s(l?.rate_pct),
    tenure_months: s(l?.tenure_months), emi: s(l?.emi), start_date: l?.start_date ?? "", outstanding: s(l?.outstanding),
    outstanding_as_of: l?.outstanding_as_of ?? "", floating: l?.floating ?? true, notes: l?.notes ?? "",
  });
  const { msg, busy, run } = useSave(onSaved, close);
  const set = (k: keyof typeof f, v: string | boolean) => setF({ ...f, [k]: v });
  const save = () => run(l ? `/api/wealth/loans/${l.id}` : "/api/wealth/loans", l ? "PUT" : "POST", {
    kind: f.kind, name: f.name, lender: f.lender || null, principal: num(f.principal), rate_pct: num(f.rate_pct), tenure_months: Number(f.tenure_months),
    emi: num(f.emi), start_date: f.start_date, outstanding: num(f.outstanding), outstanding_as_of: f.outstanding_as_of || null, floating: f.floating, notes: f.notes || null,
  });
  return (
    <div className="space-y-4 px-4 py-4 sm:px-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Type"><select value={f.kind} onChange={(e) => set("kind", e.target.value)} className={cx(inputClass, "w-full")}>
          {(Object.keys(LOAN_KIND_LABEL) as LoanKind[]).map((k) => <option key={k} value={k}>{LOAN_KIND_LABEL[k]}</option>)}</select></Field>
        <Field label="Name"><input value={f.name} onChange={(e) => set("name", e.target.value)} className={cx(inputClass, "w-full")} /></Field>
        <Field label="Lender"><input value={f.lender} onChange={(e) => set("lender", e.target.value)} className={cx(inputClass, "w-full")} /></Field>
        <Field label="Amount borrowed (₹)"><input inputMode="decimal" value={f.principal} onChange={(e) => set("principal", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Rate (% a year)"><input inputMode="decimal" value={f.rate_pct} onChange={(e) => set("rate_pct", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Tenure (months)"><input inputMode="numeric" value={f.tenure_months} onChange={(e) => set("tenure_months", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Start date" hint="The first EMI is taken a month later."><input type="date" value={f.start_date} onChange={(e) => set("start_date", e.target.value)} className={cx(inputClass, "w-full")} /></Field>
        <Field label="EMI (₹)" hint="Blank = computed from amount, rate and tenure"><input inputMode="decimal" value={f.emi} onChange={(e) => set("emi", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Statement balance (₹), optional"><input inputMode="decimal" value={f.outstanding} onChange={(e) => set("outstanding", e.target.value)} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Balance as of"><input type="date" value={f.outstanding_as_of} onChange={(e) => set("outstanding_as_of", e.target.value)} className={cx(inputClass, "w-full")} /></Field>
      </div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={f.floating} onChange={(e) => set("floating", e.target.checked)} />Floating rate</label>
      <Err msg={msg} />
      <div className="flex flex-wrap gap-2">
        <Button onClick={save} disabled={busy || !f.name.trim() || !f.start_date}>{l ? "Save" : "Add loan"}</Button>
        {l && <Button variant="ghost" onClick={() => confirm(`Delete ${l.name}?`) && run(`/api/wealth/loans/${l.id}`, "DELETE")}>Delete</Button>}
      </div>
      <p className="text-[11px] text-muted">Today: {day(w.as_of)}. Outstanding follows the amortisation schedule unless you enter a statement balance.</p>
    </div>
  );
}

function PrepayCard({ l, slab }: { l: Loan; slab: number }) {
  const p = l.prepay_vs_invest;
  if (!p) return null;
  return (
    <div className="rounded-lg bg-background-subtle px-3 py-2 text-xs">
      <p className="mb-1 font-medium">Prepay or invest? <span className="font-normal text-muted">(slab {slab} %)</span></p>
      <p className="text-muted">Costs first: prepaying locks the money away (it is no longer liquid); prepayment charges {l.floating ? "are not levied on floating-rate loans to individuals per RBI [unverified]" : "may apply on fixed-rate loans"}; check your loan agreement.</p>
      <div className="mt-1.5 grid gap-x-4 gap-y-0.5 sm:grid-cols-2">
        <span>After-tax loan rate <span className="num font-medium">{pc(p.after_tax_loan_pct, 2)}</span></span>
        <span className="text-muted">{p.deduction_note}</span>
        <span>Post-tax debt return (assumed) <span className="num">{pc(p.debt_post_tax_pct, 2)}</span></span>
        <span>Post-tax equity return (assumed) <span className="num">{pc(p.equity_post_tax_pct, 2)}</span></span>
        <span className="sm:col-span-2">P(equity beats the after-tax loan rate over {p.years_left.toFixed(1)} y) <span className="num font-medium">{prob(p.p_equity_beats_loan)}</span>
          <InfoTip>Under the lognormal assumption with your equity return and volatility, scaled for 12.5 % LTCG. A model output, not a fact: equity can do worse for many years.</InfoTip></span>
      </div>
      <p className="mt-1.5">{p.reading}</p>
    </div>
  );
}

function Loans({ w, onChanged }: { w: Wealth; onChanged: () => void }) {
  const [edit, setEdit] = useState<Loan | "new" | null>(null);
  return (
    <Card padded={false} title="Loans" icon={<Landmark className="size-4" />} subtitle="Outstanding from the amortisation schedule (or your statement balance)."
      actions={<Button icon={<Plus className="size-3.5" />} onClick={() => setEdit("new")}>Add</Button>}>
      {w.loans.length === 0 ? (
        <div className="px-4 pb-4 sm:px-5"><EmptyState title="No loans entered">Add home, car, personal or education loans to see EMIs against income and the prepay-or-invest arithmetic.</EmptyState></div>
      ) : (
        <div className="space-y-3 px-4 pb-4 sm:px-5">
          {w.loans.map((l) => (
            <div key={l.id} className="rounded-lg border border-border p-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0"><p className="truncate font-medium">{l.name}</p><p className="text-[11px] text-muted">{LOAN_KIND_LABEL[l.kind]}{l.lender ? ` · ${l.lender}` : ""} · {pc(l.rate_pct, 2)}{l.floating ? " floating" : " fixed"}</p></div>
                <Button variant="ghost" onClick={() => setEdit(l)}>Edit</Button>
              </div>
              <div className="my-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                <span>Outstanding<span className="num block text-sm font-semibold">{inr(l.outstanding_now)}</span></span>
                <span>EMI<span className="num block text-sm font-semibold">{inr(l.emi_used)}</span>{l.emi_computed && <span className="text-[10px] text-muted">computed</span>}</span>
                <span>Months left<span className="num block text-sm font-semibold">{l.months_left == null ? "—" : Math.ceil(l.months_left)}</span></span>
                <span>Borrowed<span className="num block text-sm font-semibold">{inr(l.principal)}</span></span>
              </div>
              {l.active ? <PrepayCard l={l} slab={w.tax_slab_pct} /> : <Badge tone="neutral">{l.outstanding_now == null ? "starts later" : "repaid"}</Badge>}
            </div>
          ))}
        </div>
      )}
      <Modal wide open={edit != null} onClose={() => setEdit(null)} title={edit === "new" ? "Add a loan" : edit ? `Edit ${edit.name}` : null}>
        {edit != null && <LoanForm key={edit === "new" ? "new" : edit.id} l={edit === "new" ? null : edit} w={w} onSaved={onChanged} close={() => setEdit(null)} />}
      </Modal>
    </Card>
  );
}

// ------------------------------------------------------------------ policies
function PolicyForm({ p, onSaved, close }: { p: Policy | null; onSaved: () => void; close: () => void }) {
  const [f, setF] = useState({ kind: p?.kind ?? "term", name: p?.name ?? "", cover_inr: s(p?.cover_inr), premium_inr: s(p?.premium_inr),
    end_date: p?.end_date ?? "", employer: p?.employer ?? false, notes: p?.notes ?? "" });
  const { msg, busy, run } = useSave(onSaved, close);
  const save = () => run(p ? `/api/wealth/policies/${p.id}` : "/api/wealth/policies", p ? "PUT" : "POST", {
    kind: f.kind, name: f.name, cover_inr: num(f.cover_inr), premium_inr: num(f.premium_inr), end_date: f.end_date || null, employer: f.employer, notes: f.notes || null });
  return (
    <div className="space-y-4 px-4 py-4 sm:px-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Type"><select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value as Policy["kind"] })} className={cx(inputClass, "w-full")}>
          <option value="term">Term life</option><option value="health">Health</option><option value="other">Other</option></select></Field>
        <Field label="Name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} className={cx(inputClass, "w-full")} placeholder="e.g. Term cover" /></Field>
        <Field label="Cover / sum assured (₹)"><input inputMode="decimal" value={f.cover_inr} onChange={(e) => setF({ ...f, cover_inr: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Premium a year (₹)"><input inputMode="decimal" value={f.premium_inr} onChange={(e) => setF({ ...f, premium_inr: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Cover ends"><input type="date" value={f.end_date} onChange={(e) => setF({ ...f, end_date: e.target.value })} className={cx(inputClass, "w-full")} /></Field>
      </div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={f.employer} onChange={(e) => setF({ ...f, employer: e.target.checked })} />Through my employer (ends if I leave)</label>
      <Err msg={msg} />
      <div className="flex flex-wrap gap-2">
        <Button onClick={save} disabled={busy || !f.name.trim()}>{p ? "Save" : "Add policy"}</Button>
        {p && <Button variant="ghost" onClick={() => confirm(`Delete ${p.name}?`) && run(`/api/wealth/policies/${p.id}`, "DELETE")}>Delete</Button>}
      </div>
    </div>
  );
}

function Policies({ w, onChanged }: { w: Wealth; onChanged: () => void }) {
  const [edit, setEdit] = useState<Policy | "new" | null>(null);
  return (
    <Card padded={false} title="Insurance policies" icon={<Shield className="size-4" />} subtitle="Cover amounts only; the app has no view on products or insurers."
      actions={<Button icon={<Plus className="size-3.5" />} onClick={() => setEdit("new")}>Add</Button>}>
      {w.policies.length === 0 ? (
        <div className="px-4 pb-4 sm:px-5"><EmptyState title="No policies entered">Add term and health cover to compare with the needs-based estimate.</EmptyState></div>
      ) : (
        <Table className="!mx-0">
          <thead><tr><th>Policy</th><th>Type</th><th className="text-right">Cover</th><th className="text-right">Premium / yr</th><th>Ends</th><th /></tr></thead>
          <tbody>{w.policies.map((p) => (
            <tr key={p.id}>
              <td className="font-medium">{p.name}{p.employer && <> <Badge tone="warn">employer</Badge></>}</td>
              <td className="text-xs">{p.kind === "term" ? "Term life" : p.kind === "health" ? "Health" : "Other"}</td>
              <td className="num text-right">{inr(p.cover_inr)}</td><td className="num text-right">{inr(p.premium_inr)}</td>
              <td className="num text-xs">{p.end_date ? day(p.end_date) : "—"}</td>
              <td className="text-right"><Button variant="ghost" onClick={() => setEdit(p)}>Edit</Button></td>
            </tr>
          ))}</tbody>
        </Table>
      )}
      <Modal open={edit != null} onClose={() => setEdit(null)} title={edit === "new" ? "Add a policy" : edit ? `Edit ${edit.name}` : null}>
        {edit != null && <PolicyForm key={edit === "new" ? "new" : edit.id} p={edit === "new" ? null : edit} onSaved={onChanged} close={() => setEdit(null)} />}
      </Modal>
    </Card>
  );
}

export function EntriesPanel({ w, onChanged }: { w: Wealth; onChanged: () => void }) {
  return (
    <div className="space-y-4">
      <Assets w={w} onChanged={onChanged} />
      <Loans w={w} onChanged={onChanged} />
      <Policies w={w} onChanged={onChanged} />
    </div>
  );
}
