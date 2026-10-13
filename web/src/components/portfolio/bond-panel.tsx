"use client";

// A held bond or SGB (#294): its maturity and next coupon in the holdings table, and in the holding's editor its terms
// and the "Record redemption / coupon" action. Personal data from the local API only.

import { useState } from "react";

import { Badge, Button, Field, InfoTip, cx, inputClass } from "@/components/ui";
import { api, day } from "@/lib/api";

import { BOND_KIND_LABEL, type BondView, type Holding } from "./types";

/** The badges under a bond's name in the holdings table. */
export function BondBadges({ b }: { b: BondView }) {
  if (b.matured) return <Badge tone="warn">{b.status ?? "matured — record redemption"}</Badge>;
  return (
    <>
      <Badge tone="neutral">{BOND_KIND_LABEL[b.kind]}</Badge>
      <span title={b.sources.maturity ?? undefined}>{b.maturity ? `matures ${day(b.maturity)}` : "maturity unknown"}</span>
      {b.next_coupon ? <span>· next coupon {day(b.next_coupon)}</span> : null}
    </>
  );
}

const TERMS: { key: keyof BondView & string; label: string; hint?: string; type?: "date" }[] = [
  { key: "issue_date", label: "Issue date", type: "date", hint: "An SGB matures 8 years after it and pays interest every 6 months from it." },
  { key: "maturity", label: "Maturity", type: "date" },
  { key: "coupon_pct", label: "Coupon (% a year)" },
  { key: "freq", label: "Coupons a year", hint: "1, 2, 4 or 12, from the offer document." },
  { key: "face_value", label: "Face value (₹/unit)" },
  { key: "redemption_price", label: "Redemption price (₹/unit)", hint: "An SGB's is set by RBI from the gold price: enter it once announced." },
];

function show(v: unknown): string {
  return v == null ? "" : String(v);
}

/** Terms, dates and the record-redemption/coupon form in the holding editor. */
export function BondPanel({ h, onSaved }: { h: Holding; onSaved: () => void }) {
  const b = h.bond!;
  const [terms, setTerms] = useState<Record<string, string>>(() =>
    Object.fromEntries(TERMS.map((t) => [t.key, b.sources[t.key] === "entered" ? show(b[t.key]) : ""])));
  const [rec, setRec] = useState({ kind: b.matured ? "redemption" : "interest", day: b.matured ? b.maturity ?? "" : b.next_coupon ?? "",
    quantity: String(h.units ?? ""), price: b.redemption_price != null ? String(b.redemption_price) : b.kind !== "sgb" && b.face_value != null ? String(b.face_value) : "",
    amount: "", held_to_maturity: b.matured });
  const [msg, setMsg] = useState<string | null>(null);
  const saveTerms = async () => {
    setMsg(null);
    try {
      await api(`/api/portfolio/holdings/${h.id}`, { method: "PUT", body: JSON.stringify({ bond_terms: terms }) });
      setMsg("Terms saved"); onSaved();
    } catch (e) { setMsg((e as Error).message); }
  };
  const record = async () => {
    setMsg(null);
    const body = rec.kind === "redemption"
      ? { holding_id: h.id, day: rec.day, kind: "redemption", quantity: rec.quantity, price: rec.price, stt_paid: false,
          meta: b.kind === "sgb" ? { held_to_maturity: rec.held_to_maturity } : {} }
      : { holding_id: h.id, day: rec.day, kind: "interest", amount: rec.amount };
    try {
      await api("/api/portfolio/transactions", { method: "POST", body: JSON.stringify(body) });
      setMsg(rec.kind === "redemption" ? "Redemption recorded" : "Interest recorded"); onSaved();
    } catch (e) { setMsg((e as Error).message); }
  };
  return (
    <div className="space-y-3 rounded-lg border border-border px-3 py-3" data-testid="bond-panel">
      <p className="text-xs font-medium">{BOND_KIND_LABEL[b.kind]}: maturity {b.maturity ? day(b.maturity) : "unknown"}
        {" · "}next coupon {b.next_coupon ? day(b.next_coupon) : (b.next_coupon_reason ?? "unknown")}
        {b.matured && <span className="ml-2"><Badge tone="warn">{b.status}</Badge></span>}</p>
      {b.redemption_windows.length > 0 && (
        <p className="text-xs text-muted">Early redemption with RBI (on coupon dates after the fifth year): {b.redemption_windows.map((d) => day(d)).join(", ")}</p>
      )}
      {h.price_basis === "dirty" && <p className="text-xs text-muted">Price from NSE&apos;s bond market: it includes accrued interest (dirty price).</p>}
      <div className="grid gap-2 sm:grid-cols-3">
        {TERMS.map((t) => (
          <Field key={t.key} label={t.hint ? <span className="inline-flex items-center gap-1">{t.label} <InfoTip>{t.hint}</InfoTip></span> : t.label}>
            <input type={t.type ?? "text"} inputMode={t.type ? undefined : "decimal"} value={terms[t.key]} aria-label={t.label}
              placeholder={b.sources[t.key] && b.sources[t.key] !== "entered" ? `${show(b[t.key])} (${b.sources[t.key]})` : ""}
              onChange={(e) => setTerms({ ...terms, [t.key]: e.target.value })} className={cx(inputClass, "w-full num")} />
          </Field>
        ))}
      </div>
      <Button variant="secondary" onClick={saveTerms}>Save terms</Button>
      <div className="border-t border-border pt-3">
        <p className="mb-2 text-xs font-medium">Record redemption / coupon</p>
        <div className="grid gap-2 sm:grid-cols-4">
          <Field label="What">
            <select value={rec.kind} onChange={(e) => setRec({ ...rec, kind: e.target.value })} className={cx(inputClass, "w-full")} aria-label="What">
              <option value="redemption">Redemption</option><option value="interest">Interest / coupon</option>
            </select>
          </Field>
          <Field label="Date"><input type="date" value={rec.day} onChange={(e) => setRec({ ...rec, day: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
          {rec.kind === "redemption" ? <>
            <Field label="Units"><input inputMode="decimal" value={rec.quantity} onChange={(e) => setRec({ ...rec, quantity: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
            <Field label="Price per unit (₹)"><input inputMode="decimal" value={rec.price} onChange={(e) => setRec({ ...rec, price: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
            {b.kind === "sgb" && (
              <Field label={<span className="inline-flex items-center gap-1">On maturity <InfoTip>From 1-Apr-2026 an SGB redeemed by RBI (at maturity or early after year 5) is tax-free only for the original subscriber who held it until then; a secondary-market buyer pays LTCG (ClearTax, a secondary source). Set &lsquo;Subscribed at issue?&rsquo; on the holding. Before 1-Apr-2026 every RBI redemption was exempt.</InfoTip></span>}>
                <label className="flex h-9 items-center gap-2 text-sm"><input type="checkbox" checked={rec.held_to_maturity} onChange={(e) => setRec({ ...rec, held_to_maturity: e.target.checked })} /> Held to maturity</label>
              </Field>
            )}
          </> : (
            <Field label="Amount before TDS (₹)"><input inputMode="decimal" value={rec.amount} onChange={(e) => setRec({ ...rec, amount: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
          )}
        </div>
        <div className="mt-2 flex items-center gap-3">
          <Button onClick={record} disabled={!rec.day || (rec.kind === "redemption" ? !rec.quantity || !rec.price : !rec.amount)}>Record</Button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
