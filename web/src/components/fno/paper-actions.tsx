"use client";

import { Calculator, NotebookPen } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { fmtINR } from "@/components/charts";
import { Button, Callout, InfoTip, cx, inputClass } from "@/components/ui";
import { api } from "@/lib/api";

import type { MarginEstimate, PaperTrade } from "./book-types";
import type { Leg } from "./model";

const inr = (x: number) => fmtINR(Math.round(x));

/** Under the strategy builder: record the legs as a paper trade, and an approximate margin for them (#308). */
export function PaperActions({ symbol, expiry, legs }: { symbol: string; expiry: string; legs: Leg[] }) {
  const [slip, setSlip] = useState("0.5");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState<"" | "paper" | "margin">("");
  const [made, setMade] = useState<PaperTrade | null>(null);
  const [margin, setMargin] = useState<MarginEstimate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const body = { symbol, expiry, legs: legs.map(({ right, strike, side, lots }) => ({ right, strike, side, lots })) };

  const paper = async () => {
    setBusy("paper");
    try {
      setMade(await api<PaperTrade>("/api/fno/paper", { method: "POST", body: JSON.stringify({ ...body, slippage_pct: slip, label }) }));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const estimate = async () => {
    setBusy("margin");
    try {
      setMargin(await api<MarginEstimate>("/api/fno/margin", { method: "POST", body: JSON.stringify(body) }));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-2 rounded-xl border border-border bg-background-subtle/40 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-muted">Slippage
          <input aria-label="Slippage %" type="number" min={0} max={20} step="0.1" className={cx(inputClass, "num w-16 py-1 text-xs")} value={slip} onChange={(e) => setSlip(e.target.value)} />%
          <InfoTip>A paper buy fills at the live LTP plus this %, a sell at the LTP minus it (Groww&apos;s chain has no bid/ask, so the LTP stands in for the ask and the slippage for the spread).</InfoTip>
        </label>
        <input aria-label="Label" placeholder="label (optional)" maxLength={120} className={cx(inputClass, "w-40 py-1 text-xs")} value={label} onChange={(e) => setLabel(e.target.value)} />
        <Button onClick={paper} disabled={!!busy || !legs.length} icon={<NotebookPen className="size-3.5" />}>Paper trade this</Button>
        <Button variant="ghost" onClick={estimate} disabled={!!busy || !legs.length} icon={<Calculator className="size-3.5" />}>Estimate margin</Button>
        <Link href="/fno/paper" className="ml-auto text-brand hover:underline">Paper book →</Link>
      </div>
      {error && <p role="alert" className="text-loss">{error}</p>}
      {made && (
        <Callout tone="gain" title={`Paper trade #${made.id} recorded`}>
          Filled {made.legs.map((x) => `${x.side} ${x.right} ${x.strike} @ ₹${x.entry_price}`).join(", ")}; entry costs ₹{made.entry_costs}. Prices: {made.price_source}. Nothing was sent to a broker.
        </Callout>
      )}
      {margin && (
        <Callout tone="warn" title={`≈ ₹${inr(margin.total)} margin (approximate)`}>
          Scan risk ₹{inr(margin.scan_risk)} (worst move {margin.worst_move_pct}%) + exposure ₹{inr(margin.exposure)} + premium ₹{inr(margin.premium_paid)}. {margin.label}. {margin.note}
        </Callout>
      )}
    </div>
  );
}
