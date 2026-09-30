"use client";

// The household profile block (age, income, expenses, dependants, retirement age, tax regime). Stored in the
// investor profile but never sent to the advisor (LLM) prompt.

import { UserRound } from "lucide-react";
import { useState } from "react";

import { Button, Card, Field, InfoTip, cx, inputClass } from "@/components/ui";
import { api } from "@/lib/api";

import { type Household, num } from "./types";

const s = (v: number | string | null | undefined) => (v == null ? "" : String(v));

export function HouseholdPanel({ h, onSaved }: { h: Household; onSaved: () => void }) {
  const [f, setF] = useState({
    age: s(h.age), retirement_age: s(h.retirement_age), monthly_income_inr: s(h.monthly_income_inr), monthly_expenses_inr: s(h.monthly_expenses_inr),
    dependants: s(h.dependants), earners: s(h.earners), emergency_months_target: s(h.emergency_months_target), support_years: s(h.support_years),
    tax_regime: h.tax_regime, target_equity_pct: s(h.target_equity_pct),
  });
  const [msg, setMsg] = useState<string | null>(null);
  const int = (v: string) => (v.trim() === "" ? null : Number(v));
  const save = async () => {
    setMsg(null);
    try {
      await api("/api/wealth/household", { method: "PUT", body: JSON.stringify({
        age: int(f.age), retirement_age: int(f.retirement_age) ?? 60, monthly_income_inr: num(f.monthly_income_inr), monthly_expenses_inr: num(f.monthly_expenses_inr),
        dependants: int(f.dependants) ?? 0, earners: int(f.earners) ?? 1, emergency_months_target: num(f.emergency_months_target),
        support_years: int(f.support_years), tax_regime: f.tax_regime, target_equity_pct: num(f.target_equity_pct) }) });
      setMsg("Saved");
      onSaved();
    } catch (e) { setMsg((e as Error).message); }
  };
  const box = (k: keyof typeof f, label: React.ReactNode, hint?: string, placeholder?: string) => (
    <Field label={label} hint={hint}><input inputMode="decimal" value={f[k]} placeholder={placeholder} onChange={(e) => setF({ ...f, [k]: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
  );
  return (
    <Card title="Household" icon={<UserRound className="size-4" />}
      help="Used only by the checks on this page (emergency fund, insurance, EMIs, glide path). Kept in the local database and never sent to an LLM, including the advisor.">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {box("age", "Age")}
        {box("retirement_age", "Retirement age")}
        {box("monthly_income_inr", "Monthly net income (₹)", "Take-home, all earners")}
        {box("monthly_expenses_inr", <span className="inline-flex items-center gap-1">Essential expenses a month (₹) <InfoTip>Rent, food, bills, school fees, insurance premiums. Leave out EMIs (counted separately) and SIPs.</InfoTip></span>)}
        {box("dependants", "Dependants")}
        {box("earners", "Earning members")}
        {box("emergency_months_target", "Emergency fund target (months)", "Blank = rule of thumb (6, 9 or 12)")}
        {box("support_years", "Years dependants need support", "Blank = until your retirement age")}
        <Field label={<span className="inline-flex items-center gap-1">Tax regime <InfoTip>Decides whether home-loan interest (s.24(b), old regime, up to ₹2 lakh a year for a self-occupied home) lowers the loan&apos;s after-tax cost.</InfoTip></span>}>
          <select value={f.tax_regime} onChange={(e) => setF({ ...f, tax_regime: e.target.value as Household["tax_regime"] })} className={cx(inputClass, "w-full")}>
            <option value="new">New regime</option><option value="old">Old regime</option>
          </select>
        </Field>
        {box("target_equity_pct", "Your equity target, %", "Blank = the age rule of thumb")}
      </div>
      <div className="mt-3 flex items-center gap-3"><Button onClick={save}>Save household</Button>{msg && <span className="text-xs text-muted">{msg}</span>}</div>
    </Card>
  );
}
