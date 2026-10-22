"use client";

import { BellPlus, Loader2, NotebookPen } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui";
import { api } from "@/lib/api";

import type { PaperTrade } from "./book-types";
import type { TradePlan } from "./plan-types";

/** One click from a plan: a paper trade with the plan's exit rules, or the rules alone on the Groww position group
 * (symbol + expiry) the user opens by hand. Each rule raises one alert; nothing is ever ordered (#317). */
export function PlanActions({ plan }: { plan: TradePlan }) {
  const [busy, setBusy] = useState<"" | "paper" | "alerts">("");
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rules = plan.exit_rules ?? [];
  const label = `plan:${plan.evidence.match === "exact" && plan.evidence.spec_key ? plan.evidence.spec_key : plan.kind}`;

  const arm = async (target: { paper_id: number } | { position_key: string }) => {
    for (const r of rules) {
      await api("/api/fno/exit-rules", { method: "POST", body: JSON.stringify({ ...target, kind: r.kind, threshold: r.threshold }) });
    }
  };
  const paper = async () => {
    setBusy("paper");
    try {
      const legs = plan.legs.map(({ right, strike, side, lots }) => ({ right, strike, side, lots }));
      const t = await api<PaperTrade>("/api/fno/paper", { method: "POST", body: JSON.stringify({ symbol: plan.symbol, expiry: plan.expiry, legs, label, ...(plan.card_log_id ? { card_log_id: plan.card_log_id, card_index: plan.card_index ?? 0 } : {}) }) });
      await arm({ paper_id: t.id });
      setDone(`Paper trade #${t.id} recorded with ${rules.length} exit alerts. Nothing was sent to a broker.`);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const alerts = async () => {
    setBusy("alerts");
    try {
      await arm({ position_key: `${plan.symbol}:${plan.expiry}` });
      setDone(`${rules.length} exit alerts armed on your Groww ${plan.symbol} ${plan.expiry} positions. They alert; they never trade.`);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-2 text-xs">
      <Button onClick={paper} disabled={!!busy || !plan.size?.suggested} icon={busy === "paper" ? <Loader2 className="size-3.5 animate-spin" /> : <NotebookPen className="size-3.5" />}>
        Paper trade + alerts
      </Button>
      <Button variant="ghost" onClick={alerts} disabled={!!busy || !rules.length} icon={<BellPlus className="size-3.5" />}>
        Alerts on my Groww position
      </Button>
      <span className="text-[11px] text-muted">{rules.map((r) => `${r.kind} ${r.threshold}`).join(" · ")}</span>
      {error && <p role="alert" className="w-full text-loss">{error}</p>}
      {done && <p role="status" className="w-full text-gain">{done}</p>}
    </div>
  );
}
