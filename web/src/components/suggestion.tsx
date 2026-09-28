"use client";

import { useState } from "react";

import { Badge, Button, ErrorNote } from "@/components/ui";
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
          <h4 className="font-medium">Before bidding</h4>
          <ul className="list-disc pl-5">
            {d.suggestion.conditions.map((c) => <li key={c}>{c}</li>)}
          </ul>
        </div>
      )}
      <div>
        <h4 className="font-medium">Your rules</h4>
        <ul className="space-y-1">
          {d.inputs.rules.map((r) => (
            <li key={r.rule.id} className="flex flex-wrap items-center gap-2">
              <Badge status={RULE[r.status]}>{r.status}</Badge>
              <code className="text-xs">
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
        <ul className="list-disc pl-5 text-xs text-amber-700 dark:text-amber-300">
          {[...d.suggestion.enforcement_notes, ...d.suggestion.warnings].map((n) => <li key={n}>{n}</li>)}
        </ul>
      )}
      <p>
        <span className="font-medium">Exit plan: </span>
        {d.suggestion.agent.exit_plan}
      </p>
      <details>
        <summary className="cursor-pointer text-muted">Advisor rationale and live inputs</summary>
        <p className="mt-2 whitespace-pre-wrap">{d.suggestion.agent.rationale_markdown}</p>
        <table className="mt-2 text-xs">
          <tbody>
            {Object.entries(m).map(([k, v]) => (
              <tr key={k}>
                <td className="pr-3 text-muted">{k}</td>
                <td className="pr-3">{v.value == null ? "—" : num(v.value)}</td>
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
      <div className="flex items-center gap-3">
        <Button onClick={run} disabled={busy}>
          {busy ? "Checking live data and asking the advisor…" : latest ? "Refresh with live data" : "Get my suggestion"}
        </Button>
        <span className="text-xs text-muted">Uses your profile and rules; takes about a minute.</span>
      </div>
      <ErrorNote error={error ?? list.error} />
      {latest && <DecisionCard d={latest} />}
    </div>
  );
}
