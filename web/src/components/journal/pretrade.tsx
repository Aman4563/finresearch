"use client";

import { CircleCheck, CircleHelp, CircleX, ClipboardCheck, Info, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Callout, Card, ErrorNote, Field, Segmented, cx, inputClass } from "@/components/ui";
import { api, useApi } from "@/lib/api";

import type { CheckStatus, Checklist, HoldingLite } from "./types";

const ICON: Record<CheckStatus, React.ReactNode> = {
  ok: <CircleCheck className="size-4 text-gain" />,
  warn: <TriangleAlert className="size-4 text-warn" />,
  block: <CircleX className="size-4 text-loss" />,
  info: <Info className="size-4 text-info" />,
  unknown: <CircleHelp className="size-4 text-muted" />,
};

/** The checklist items, as returned by POST /api/journal/pretrade (also stored with a planned entry). */
export function ChecklistView({ c, compact }: { c: Checklist; compact?: boolean }) {
  return (
    <div className="space-y-2">
      {!compact && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={c.status === "block" ? "loss" : c.status === "warn" ? "warn" : "gain"}>
            {c.status === "block" ? "cannot be done as entered" : c.status === "warn" ? "check the warnings" : "nothing flagged"}
          </Badge>
          <span className="text-muted">
            {c.side} {c.quantity.toLocaleString("en-IN")} × ₹{c.price.toLocaleString("en-IN")} of {c.name || c.instrument}
          </span>
        </div>
      )}
      <ul className="divide-y divide-border/60 rounded-lg border border-border">
        {c.items.map((i) => (
          <li key={i.key} className="flex gap-3 px-3 py-2.5">
            <span className="mt-0.5 shrink-0">{ICON[i.status]}</span>
            <div className="min-w-0">
              <p className="text-sm font-medium">{i.label}</p>
              <p className="text-xs text-muted">{i.detail}</p>
              {i.source && <p className="mt-0.5 text-[10px] text-muted/80">Source: {i.source}</p>}
            </div>
          </li>
        ))}
      </ul>
      {!compact && <p className="text-[11px] text-muted">{c.note}</p>}
    </div>
  );
}

type Form = { side: "buy" | "sell"; holding_id: string; name: string; nse_symbol: string; asset_type: "stock" | "mf";
  quantity: string; price: string; charges: string; day: string };

/** Plan a trade: the checklist first, then the thesis, recorded as a planned journal entry. Nothing is ordered. */
export function PreTradePanel({ onRecorded }: { onRecorded: () => void }) {
  const { data: pf } = useApi<{ holdings: HoldingLite[] }>("/api/portfolio?prices=cached");
  const holdings = (pf?.holdings ?? []).filter((h) => !h.closed);
  const [f, setF] = useState<Form>({ side: "buy", holding_id: "", name: "", nse_symbol: "", asset_type: "stock", quantity: "", price: "", charges: "", day: "" });
  const [c, setC] = useState<Checklist | null>(null);
  const [t, setT] = useState({ thesis: "", invalidation: "", expected_holding_days: "", confidence_pct: "", review_on: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const run = async () => {
    setBusy(true);
    setDone(false);
    try {
      const body: Record<string, unknown> = { side: f.side, quantity: f.quantity, price: f.price, charges: f.charges || "0" };
      if (f.day) body.day = f.day;
      if (f.holding_id) body.holding_id = Number(f.holding_id);
      else Object.assign(body, { name: f.name, nse_symbol: f.nse_symbol || null, asset_type: f.asset_type });
      setC(await api<Checklist>("/api/journal/pretrade", { method: "POST", body: JSON.stringify(body) }));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setC(null);
    } finally {
      setBusy(false);
    }
  };
  const record = async () => {
    if (!c) return;
    setBusy(true);
    try {
      await api("/api/journal/notes", {
        method: "POST",
        body: JSON.stringify({
          side: c.side, status: "planned", holding_id: c.holding_id, name: c.name || f.name || c.instrument, instrument: c.instrument,
          asset_type: c.asset_type === "mf" ? "mf" : c.asset_type === "stock" ? "stock" : "other",
          trade_day: c.day, quantity: String(c.quantity), price: String(c.price), thesis: t.thesis || null, invalidation: t.invalidation || null,
          expected_holding_days: t.expected_holding_days ? Number(t.expected_holding_days) : null,
          confidence_pct: t.confidence_pct ? Number(t.confidence_pct) : null, review_on: t.review_on || null, checklist: c,
        }),
      });
      setDone(true);
      setC(null);
      setT({ thesis: "", invalidation: "", expected_holding_days: "", confidence_pct: "", review_on: "" });
      setError(null);
      onRecorded();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const input = (k: keyof Form, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <Field label={label}>
      <input className={cx(inputClass, "w-full", props.inputMode === "decimal" && "num")} value={f[k]} {...props}
        onChange={(e) => { setF({ ...f, [k]: e.target.value }); setC(null); }} />
    </Field>
  );
  return (
    <Card title="Plan a trade" icon={<ClipboardCheck className="size-4" />}
      subtitle="Run the checklist before you trade, then write down why. The app places no orders."
      help="Position size and concentration use the last daily valuation; tax uses your lots first-in-first-out and this year's realised gains; the signal shows its probability interval and how it was validated.">
      <div className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <Segmented<"buy" | "sell"> value={f.side} onChange={(v) => { setF({ ...f, side: v }); setC(null); }}
            options={[{ value: "buy", label: "Buy" }, { value: "sell", label: "Sell" }]} />
          <div className="min-w-56 flex-1">
            <Field label={f.side === "sell" ? "Holding to sell" : "Holding (or a new instrument below)"}>
              <select className={cx(inputClass, "w-full")} value={f.holding_id} onChange={(e) => { setF({ ...f, holding_id: e.target.value }); setC(null); }}>
                <option value="">{f.side === "sell" ? "Choose…" : "A new instrument"}</option>
                {holdings.map((h) => <option key={h.id} value={h.id}>{h.name} · {h.account}</option>)}
              </select>
            </Field>
          </div>
        </div>
        {f.side === "buy" && !f.holding_id && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {input("name", "Name")}
            {input("nse_symbol", "NSE symbol (stocks)")}
            <Field label="Type">
              <select className={cx(inputClass, "w-full")} value={f.asset_type} onChange={(e) => setF({ ...f, asset_type: e.target.value as "stock" | "mf" })}>
                <option value="stock">Stock</option>
                <option value="mf">Mutual fund</option>
              </select>
            </Field>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {input("quantity", "Units", { inputMode: "decimal" })}
          {input("price", "Price ₹", { inputMode: "decimal" })}
          {input("charges", "Charges ₹ (optional)", { inputMode: "decimal" })}
          {input("day", "Day", { type: "date" })}
        </div>
        {f.side === "sell" && (() => {
          const e = holdings.find((h) => String(h.id) === f.holding_id)?.elss;
          if (!e) return null;
          return (
            <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
              <Badge tone={e.locked_units > 0 ? "warn" : "gain"}>ELSS</Badge>
              You can redeem up to {e.sellable_units.toLocaleString("en-IN")} unit(s) today; {e.locked_units.toLocaleString("en-IN")} are still in the 3-year lock-in
              {e.next_unlock ? ` (next ${e.next_unlock.units.toLocaleString("en-IN")} on ${e.next_unlock.day})` : ""}{e.unknown_units > 0 ? `; ${e.unknown_units.toLocaleString("en-IN")} have no purchase date` : ""}.
              {e.sellable_units > 0 && <button type="button" className="font-medium text-brand hover:underline" onClick={() => { setF({ ...f, quantity: String(e.sellable_units) }); setC(null); }}>Use {e.sellable_units.toLocaleString("en-IN")}</button>}
            </p>
          );
        })()}
        <Button onClick={run} disabled={busy || !f.quantity || !f.price || (f.side === "sell" && !f.holding_id)}>
          {busy && !c ? "Checking…" : "Run the checklist"}
        </Button>
        <ErrorNote error={error} />
        {done && <Callout tone="gain" title="Recorded as a planned trade">When the trade is imported it becomes this entry (same holding, side, within 7 days).</Callout>}
        {c && (
          <div className="space-y-4 animate-fade-in">
            <ChecklistView c={c} />
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <Field label="Why (the thesis)">
                <textarea className={cx(inputClass, "h-20 w-full py-2")} value={t.thesis} onChange={(e) => setT({ ...t, thesis: e.target.value })} />
              </Field>
              <Field label="What would prove it wrong (exit rule)">
                <textarea className={cx(inputClass, "h-20 w-full py-2")} value={t.invalidation} onChange={(e) => setT({ ...t, invalidation: e.target.value })} />
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Field label="Expected holding (days)">
                <input className={cx(inputClass, "num w-full")} inputMode="numeric" value={t.expected_holding_days} onChange={(e) => setT({ ...t, expected_holding_days: e.target.value })} />
              </Field>
              <Field label="Confidence %">
                <input className={cx(inputClass, "num w-full")} inputMode="numeric" value={t.confidence_pct} onChange={(e) => setT({ ...t, confidence_pct: e.target.value })} />
              </Field>
              <Field label="Review on">
                <input type="date" className={cx(inputClass, "w-full")} value={t.review_on} onChange={(e) => setT({ ...t, review_on: e.target.value })} />
              </Field>
            </div>
            <Button onClick={record} disabled={busy || c.status === "block"}>Record the planned trade</Button>
          </div>
        )}
      </div>
    </Card>
  );
}
