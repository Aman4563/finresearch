"use client";

// The joint view's pre-trade check (#397): how D_total and L(−10 %) would change if one F&O leg were added to today's
// book. The POST stores nothing; a figure that cannot be computed shows what is missing, never 0. Analysis only.

import { useState } from "react";

import { Button, Field, Table, cx, inputClass } from "@/components/ui";
import { api } from "@/lib/api";

import { Reasons } from "./joint-risk";
import type { Pretrade } from "./joint-risk-types";
import { signed } from "./types";

type Leg = { underlying: string; expiry: string; right: "call" | "put" | "future"; strike: string; qty: string };

const per = (v: number | null) => (v == null ? null : v / 100);

export function JointPretrade({ beta1 }: { beta1: boolean }) {
  const [leg, setLeg] = useState<Leg>({ underlying: "NIFTY", expiry: "", right: "put", strike: "", qty: "" });
  const [res, setRes] = useState<Pretrade | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const check = async () => {
    setBusy(true); setErr(null);
    try {
      const body = { legs: [{ ...leg, strike: leg.right === "future" ? 0 : Number(leg.strike), qty: Number(leg.qty) }] };
      setRes(await api<Pretrade>(`/api/portfolio/analytics/joint-risk/pretrade?assume_beta1=${beta1}`, { method: "POST", body: JSON.stringify(body) }));
    } catch (e) { setErr((e as Error).message); setRes(null); } finally { setBusy(false); }
  };
  const set = (k: keyof Leg) => (e: { target: { value: string } }) => setLeg({ ...leg, [k]: e.target.value });
  return (
    <div className="space-y-2 border-t border-border pt-3">
      <p className="text-sm font-medium">Before an F&amp;O trade</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Field label="Underlying"><input value={leg.underlying} onChange={set("underlying")} className={cx(inputClass, "w-full")} /></Field>
        <Field label="Expiry"><input type="date" value={leg.expiry} onChange={set("expiry")} className={cx(inputClass, "w-full")} /></Field>
        <Field label="Type">
          <select value={leg.right} onChange={set("right")} className={cx(inputClass, "w-full")}>
            <option value="call">Call</option><option value="put">Put</option><option value="future">Future</option>
          </select>
        </Field>
        <Field label="Strike"><input inputMode="decimal" value={leg.strike} disabled={leg.right === "future"} onChange={set("strike")} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Units (− = sell)"><input inputMode="numeric" value={leg.qty} onChange={set("qty")} placeholder="-75" className={cx(inputClass, "w-full num")} /></Field>
      </div>
      <Button variant="secondary" onClick={check} disabled={busy || !leg.expiry || !leg.qty}>{busy ? "Checking…" : "Check the change"}</Button>
      {err && <p className="text-xs text-loss">{err}</p>}
      {res && (
        <>
          <Table label="Pre-trade change">
            <thead><tr><th /><th className="text-right">Now</th><th className="text-right">With the trade</th><th className="text-right">Change</th></tr></thead>
            <tbody>
              <tr><td>D_total, ₹ per 1 % NIFTY move</td><td className="num text-right">{signed(per(res.d_before.total))}</td>
                <td className="num text-right">{signed(per(res.d_after.total))}</td><td className="num text-right">{signed(per(res.d_change))}</td></tr>
              <tr><td>L(−10 %)</td><td className="num text-right">{signed(res.l_before.total)}</td>
                <td className="num text-right">{signed(res.l_after.total)}</td><td className="num text-right">{signed(res.l_change)}</td></tr>
            </tbody>
          </Table>
          <p className="text-xs text-muted">The trade alone: {signed(per(res.trade_delta))} per 1 % NIFTY move; {signed(res.trade_l)} at −10 %.</p>
          <Reasons items={[...res.d_after.reasons, ...res.l_after.reasons, ...res.trade_reasons]} label="What is missing for the check" />
        </>
      )}
    </div>
  );
}
