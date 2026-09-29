"use client";

import { useState } from "react";

import { Loader2, Sparkles } from "lucide-react";

import { Badge, Button, ErrorNote, InfoTip, SkeletonRows } from "@/components/ui";
import { api, type Decision, useApi, when } from "@/lib/api";

const ACTION: Record<string, string> = { APPLY: "done", "APPLY-CONDITIONAL": "unverified", SKIP: "blocked" };
const RULE: Record<string, string> = { fired: "blocked", clear: "done", unknown: "unverified" };
const num = (v: string | null) => (v == null ? "unknown" : Number.isInteger(Number(v)) ? v : Number(v).toFixed(2));

export function DecisionCard({ d }: { d: Decision }) {
  const m = d.inputs.metrics;
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge status={ACTION[d.action]}>{d.action}</Badge>
        <span>
          {d.lots} lot{d.lots === 1 ? "" : "s"} · {d.category}
          {m.lot_cost?.value && d.lots > 0 && ` · ₹${(Number(m.lot_cost.value) * d.lots).toLocaleString("en-IN")}`}
        </span>
        <span className="text-xs text-muted">
          {when(d.inputs.at)} · {d.suggestion.model} · agent said {d.suggestion.agent.action} ({d.suggestion.agent.confidence})
        </span>
      </div>
      {d.suggestion.conditions.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Before bidding</h4>
          <ul className="list-disc pl-5">
            {d.suggestion.conditions.map((c) => <li key={c}>{c}</li>)}
          </ul>
        </div>
      )}
      <div>
        <h4 className="mb-1.5 flex items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted">Your rules <InfoTip>Your personal rules from the Rules page, checked against live data. A fired skip rule decides the outcome.</InfoTip></h4>
        <ul className="space-y-1">
          {d.inputs.rules.map((r) => (
            <li key={r.rule.id} className="flex flex-wrap items-center gap-2">
              <Badge status={RULE[r.status]}>{r.status}</Badge>
              <code className="num rounded bg-background-subtle px-1.5 py-0.5 text-xs">
                {r.rule.metric} {r.rule.op} {r.rule.value} → {r.rule.action}
              </code>
              <span className="text-xs text-muted">
                now {num(r.value)} ({r.source})
              </span>
            </li>
          ))}
        </ul>
      </div>
      {[...d.suggestion.enforcement_notes, ...d.suggestion.warnings].length > 0 && (
        <ul className="list-disc rounded-lg bg-warn-soft py-2 pr-3 pl-7 text-xs text-warn">
          {[...d.suggestion.enforcement_notes, ...d.suggestion.warnings].map((n) => <li key={n}>{n}</li>)}
        </ul>
      )}
      <div className="rounded-lg border border-border bg-background-subtle/50 p-3">
        <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Exit plan</p>
        <p>{d.suggestion.agent.exit_plan}</p>
      </div>
      <details>
        <summary className="cursor-pointer text-sm font-medium text-brand hover:underline">Advisor rationale and live inputs</summary>
        <p className="mt-2 whitespace-pre-wrap text-foreground/90">{d.suggestion.agent.rationale_markdown}</p>
        <table className="mt-3 w-full text-xs [&_td]:border-b [&_td]:border-border/60 [&_td]:py-1">
          <tbody>
            {Object.entries(m).map(([k, v]) => (
              <tr key={k}>
                <td className="pr-3 text-muted">{k}</td>
                <td className="num pr-3 text-right">{v.value == null ? "—" : num(v.value)}</td>
                <td className="text-muted">
                  {v.source}
                  {v.as_of ? ` @ ${when(v.as_of)}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

export function SuggestionPanel({ runId }: { runId: string }) {
  const list = useApi<Decision[]>(`/api/decisions?run_id=${runId}`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latest = list.data?.[0];

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await api(`/api/runs/${runId}/suggest`, { method: "POST" });
      list.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={run} disabled={busy} icon={busy ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}>
          {busy ? "Checking live data and asking the advisor…" : latest ? "Refresh with live data" : "Get my suggestion"}
        </Button>
        <span className="text-xs text-muted">Uses your profile, your rules and live subscription data; takes about a minute and a little of your Claude plan.</span>
      </div>
      <ErrorNote error={error ?? list.error} />
      {!list.data && !list.error && <SkeletonRows rows={3} />}
      {list.data && !latest && <p className="text-sm text-muted">No suggestion yet for this run. Press the button to get one sized to your profile and rules.</p>}
      {latest && <DecisionCard d={latest} />}
    </div>
  );
}
