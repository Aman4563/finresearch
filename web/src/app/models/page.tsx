"use client";

import { ShieldCheck } from "lucide-react";
import { useState } from "react";

import { Badge, Callout, Card, ErrorNote, PageHeader, Segmented, Skeleton, Table, type Tone } from "@/components/ui";
import { useApi } from "@/lib/api";

type Split = { from: string | null; to: string | null; status: string; reason: string | null;
  metrics: { trades?: number; expectancy?: number | null; t_stat?: number | null; ci_low?: number | null; ci_high?: number | null } | null };
type Review = { reviewed_on?: string; reviewer?: string; verdict?: string; prereg_commit?: string;
  prereg_committed_at?: string; path: string };
type Entry = {
  key: string; name: string; kind: string; tier: "A" | "B" | "C"; purpose: string; intended_use: string;
  restrictions: string; limits_of_use: string; outcome_metric: string; status: "validated" | "unvalidated" | "failed";
  status_reason: string; evidence: string; evidence_as_of: string | null; evidence_age_days: number | null;
  evidence_stale: string[] | null; red_team: Review[];
  revalidation: { slot: string; created_at: string; splits: Record<string, Split> } | null;
};
type Models = {
  as_of: string; limits_of_use: string; max_evidence_age_days: number; tiers: Record<string, string>;
  revalidation: { after: string; every: string; splits: string[] }; redteam_invalid: { path: string; why: string }[];
  entries: Entry[];
};

const TONE: Record<Entry["status"], Tone> = { validated: "gain", unvalidated: "warn", failed: "loss" };
type Filter = "all" | Entry["status"];
const num = (x: number | null | undefined) => (x == null ? "unknown" : String(x));

function ReValidation({ rv, splits }: { rv: NonNullable<Entry["revalidation"]>; splits: string[] }) {
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted">Re-validated {rv.created_at.slice(0, 10)} ({rv.slot}): a post hoc read-only replay; it never changes the status.</p>
      <Table label="Re-validation by regime split" bleed={false}>
        <thead><tr><th>Split</th><th>Trades</th><th>₹/trade</th><th>t</th><th>CI</th></tr></thead>
        <tbody>
          {splits.filter((k) => rv.splits[k]).map((k) => {
            const s = rv.splits[k];
            return s.metrics ? (
              <tr key={k}><td>{k}</td><td>{num(s.metrics.trades)}</td><td>{num(s.metrics.expectancy)}</td>
                <td>{num(s.metrics.t_stat)}</td><td>{num(s.metrics.ci_low)} … {num(s.metrics.ci_high)}</td></tr>
            ) : (
              <tr key={k}><td>{k}</td><td colSpan={4} className="text-muted">{s.reason ?? "no data"}</td></tr>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}

function EntryCard({ e, splits }: { e: Entry; splits: string[] }) {
  const age = e.evidence_age_days == null ? "unknown" : `${e.evidence_age_days} days (${e.evidence_as_of})`;
  return (
    <article aria-label={e.name} className="space-y-2 rounded-xl border border-border bg-card p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-medium">{e.name}</h2>
        <Badge tone={TONE[e.status]}>{e.status}</Badge>
        <Badge tone="neutral">tier {e.tier}</Badge>
        <span className="text-xs text-muted">{e.kind} · <code>{e.key}</code></span>
      </div>
      <p className="text-sm">{e.status_reason}</p>
      <p className="text-sm text-muted">{e.purpose} {e.intended_use}</p>
      <dl className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-[auto_1fr]">
        <dt className="text-muted">Limits of use</dt><dd>{e.restrictions} {e.limits_of_use}</dd>
        <dt className="text-muted">Outcome metric</dt><dd>{e.outcome_metric}</dd>
      </dl>
      <p className="text-xs">Evidence age: {age}</p>
      {e.evidence_stale?.map((w) => <p key={w} className="text-xs text-warn">{w}</p>)}
      {e.red_team.length > 0 && (
        <ul className="text-xs">
          {e.red_team.map((r) => (
            <li key={r.path}>Red team {r.reviewed_on ?? ""} {r.verdict ? `· ${r.verdict}` : ""} · <code>{r.path}</code>
              {r.prereg_commit ? ` (PREREG ${r.prereg_commit.slice(0, 12)}, ${r.prereg_committed_at})` : ""}</li>
          ))}
        </ul>
      )}
      {e.revalidation && <ReValidation rv={e.revalidation} splits={splits} />}
    </article>
  );
}

export default function ModelsPage() {
  const { data, error, reload } = useApi<Models>("/api/models");
  const [filter, setFilter] = useState<Filter>("all");
  const count = (s: Filter) => (data?.entries ?? []).filter((e) => s === "all" || e.status === s).length;
  const shown = (data?.entries ?? []).filter((e) => filter === "all" || e.status === filter);
  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <PageHeader icon={<ShieldCheck className="size-5" />} eyebrow="Workspace" title="Model inventory"
        description="Every model and rule the app uses, with its status computed from dated evidence: validated only while its evidence is current, unvalidated when it is missing or expired, failed when it failed its pre-registered test." />
      <ErrorNote error={error} onRetry={reload} />
      {!data ? (
        !error && <Skeleton className="h-40 rounded-xl" />
      ) : (
        <>
          <Callout tone="info" title="Limits of use">
            {data.limits_of_use} Backtest evidence expires after {data.max_evidence_age_days} days or at a market regime change.
            The recipes are re-validated every {data.revalidation.every} after {data.revalidation.after} IST by regime split.
          </Callout>
          {data.redteam_invalid.length > 0 && (
            <Callout tone="warn" title="Red-team files that could not be read">
              {data.redteam_invalid.map((x) => <span key={x.path} className="block"><code>{x.path}</code>: {x.why}</span>)}
            </Callout>
          )}
          <Card padded>
            <Segmented label="Status" value={filter} onChange={setFilter}
              options={(["all", "validated", "unvalidated", "failed"] as Filter[]).map((s) => ({ value: s, label: `${s} (${count(s)})` }))} />
          </Card>
          <div className="space-y-3">
            {shown.map((e) => <EntryCard key={e.key} e={e} splits={data.revalidation.splits} />)}
          </div>
        </>
      )}
    </div>
  );
}
