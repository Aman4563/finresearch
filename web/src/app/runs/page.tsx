"use client";

import { ArrowRight, CheckCircle2, Clock, FileText, FlaskConical, LayoutGrid, List, ShieldAlert, ShieldCheck, Timer } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { KindIcon, KIND, fmtDuration, kindMeta, runSeconds } from "@/components/workspace/run-meta";
import { Badge, Card, EmptyState, ErrorNote, PageHeader, Segmented, Skeleton, Stat, Table, cx, inputClass } from "@/components/ui";
import { type RunSummary, useApi, when } from "@/lib/api";

type View = "cards" | "table";
const STATUS_FILTERS = ["all", "running", "done", "paused", "failed"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

const matchesStatus = (r: RunSummary, f: StatusFilter) =>
  f === "all" || (f === "failed" ? r.status === "failed" || r.status === "blocked" : f === "paused" ? r.status === "paused" || !!r.resume_after : r.status === f);

function GateBadge({ gate }: { gate: RunSummary["final_gate"] }) {
  if (!gate) return <Badge tone="neutral">no gate record</Badge>;
  return gate.ok ? (
    <Badge tone="gain">
      <ShieldCheck className="size-3" /> gate passed
    </Badge>
  ) : (
    <Badge tone="loss">
      <ShieldAlert className="size-3" /> gate blocked
    </Badge>
  );
}

function StepsBar({ steps }: { steps: Record<string, number> }) {
  const total = Object.values(steps).reduce((a, b) => a + b, 0);
  const done = steps.done ?? 0;
  const failed = (steps.failed ?? 0) + (steps.blocked ?? 0);
  const running = steps.running ?? 0;
  if (!total) return <span className="text-xs text-muted">no steps yet</span>;
  return (
    <div className="min-w-28" title={Object.entries(steps).map(([k, v]) => `${v} ${k}`).join(" · ")}>
      <div className="flex h-1.5 overflow-hidden rounded-full bg-background-subtle">
        <div className="bg-gain transition-[width] duration-700" style={{ width: `${(done / total) * 100}%` }} />
        <div className="animate-pulse bg-info" style={{ width: `${(running / total) * 100}%` }} />
        <div className="bg-loss" style={{ width: `${(failed / total) * 100}%` }} />
      </div>
      <p className="num mt-1 text-[11px] text-muted">
        {done}/{total} steps
      </p>
    </div>
  );
}

function RunCard({ r }: { r: RunSummary }) {
  const secs = runSeconds(r.created_at, r.finished_at);
  return (
    <Card interactive padded={false} className="group flex flex-col">
      <Link href={`/runs/${r.id}`} className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-start gap-3">
          <KindIcon kind={r.kind} />
          <div className="min-w-0 flex-1">
            <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
              {kindMeta(r.kind).label} · <span className="num">#{r.id}</span>
            </p>
            <p className="line-clamp-2 text-sm font-semibold leading-snug">{r.company_name ?? r.company ?? "Unknown company"}</p>
          </div>
          <Badge status={r.status} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <GateBadge gate={r.final_gate} />
          {r.worker?.alive && <Badge tone="info" dot>worker live</Badge>}
          {r.resume_after && <Badge status="paused">paused for plan window</Badge>}
        </div>
        <StepsBar steps={r.steps} />
        <div className="mt-auto flex items-center justify-between gap-2 text-xs text-muted">
          <span className="inline-flex items-center gap-1">
            <Clock className="size-3.5" /> {when(r.created_at)}
          </span>
          <span className="num inline-flex items-center gap-1">
            <Timer className="size-3.5" /> {fmtDuration(secs)}
          </span>
        </div>
      </Link>
      <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs">
        <Link href={`/runs/${r.id}`} className="inline-flex items-center gap-1 font-medium text-brand hover:underline">
          Pipeline <ArrowRight className="size-3.5 transition group-hover:translate-x-0.5" />
        </Link>
        {r.has_report ? (
          <Link href={`/runs/${r.id}/report`} className="inline-flex items-center gap-1 font-medium text-foreground hover:text-brand">
            <FileText className="size-3.5" /> Read report
          </Link>
        ) : (
          <span className="text-muted">No report yet</span>
        )}
      </div>
    </Card>
  );
}

export default function Runs() {
  const { data, error, reload } = useApi<RunSummary[]>("/api/runs", 10000);
  const [kind, setKind] = useState<string>("all");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [q, setQ] = useState("");
  const [view, setView] = useState<View>("cards");

  const kpi = useMemo(() => {
    const runs = data ?? [];
    const by: Record<string, number> = {};
    for (const r of runs) by[r.status] = (by[r.status] ?? 0) + 1;
    const finished = runs.filter((r) => ["done", "failed", "blocked"].includes(r.status));
    const ok = finished.filter((r) => r.status === "done" && r.final_gate?.ok !== false);
    const durations = runs.filter((r) => r.finished_at).map((r) => runSeconds(r.created_at, r.finished_at)).filter((x): x is number => x != null);
    return {
      by,
      total: runs.length,
      rate: finished.length ? (ok.length / finished.length) * 100 : null,
      avg: durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : null,
      reports: runs.filter((r) => r.has_report).length,
      live: runs.filter((r) => r.worker?.alive).length,
    };
  }, [data]);

  const rows = useMemo(
    () =>
      (data ?? []).filter(
        (r) =>
          (kind === "all" || r.kind === kind) &&
          matchesStatus(r, status) &&
          (!q || `${r.company_name ?? ""} ${r.company ?? ""} #${r.id}`.toLowerCase().includes(q.toLowerCase())),
      ),
    [data, kind, status, q],
  );

  const kinds = [{ value: "all", label: "All" }, ...Object.entries(KIND).filter(([k]) => data?.some((r) => r.kind === k)).map(([k, m]) => ({ value: k, label: m.label }))];

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<FlaskConical className="size-5" />}
        eyebrow="Workspace"
        title="Research runs"
        description="Every research run the agents have done: its live progress, whether the report passed the fact-check gate, and a link to read it."
      />

      <div className="stagger grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Runs" value={data ? kpi.total : null} format={(n) => String(Math.round(n))} icon={<FlaskConical className="size-4" />}
          hint={data ? `${kpi.by.done ?? 0} done · ${kpi.by.running ?? 0} running · ${(kpi.by.failed ?? 0) + (kpi.by.blocked ?? 0)} failed` : undefined} />
        <Stat label="Success rate" value={kpi.rate} format={(n) => `${Math.round(n)}%`} tone="gain" icon={<CheckCircle2 className="size-4" />}
          help="Finished runs that completed and whose report passed the publish gate (every figure cited to a verified source)." hint="of finished runs" />
        <Stat label="Average duration" value={kpi.avg} format={fmtDuration} tone="info" icon={<Timer className="size-4" />}
          hint="start to finish, wall clock" />
        <Stat label="Reports ready" value={data ? kpi.reports : null} format={(n) => String(Math.round(n))} tone="accent" icon={<FileText className="size-4" />}
          hint={kpi.live ? `${kpi.live} worker(s) live now` : "no worker running"} />
      </div>

      <Card padded={false}>
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3 sm:px-5">
          <div className="-mx-1 max-w-full overflow-x-auto px-1">
            <Segmented value={kind} onChange={setKind} options={kinds} />
          </div>
          <select aria-label="Status" className={cx(inputClass, "h-8 text-xs")} value={status} onChange={(e) => setStatus(e.target.value as StatusFilter)}>
            {STATUS_FILTERS.map((s) => (
              <option key={s} value={s}>
                {s === "all" ? "Any status" : s === "failed" ? "failed / blocked" : s}
              </option>
            ))}
          </select>
          <input aria-label="Search runs" placeholder="Search company or #id" className={cx(inputClass, "h-8 min-w-0 flex-1 text-xs sm:max-w-60")} value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="ml-auto">
            <Segmented value={view} onChange={setView} options={[
              { value: "cards", label: <LayoutGrid className="size-3.5" aria-label="Cards" /> },
              { value: "table", label: <List className="size-3.5" aria-label="Table" /> },
            ]} />
          </div>
        </div>

        <div className="p-4 sm:p-5">
          <ErrorNote error={error} onRetry={reload} />
          {!data && !error && (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 6 }, (_, i) => (
                <Skeleton key={i} className="h-44 rounded-xl" />
              ))}
            </div>
          )}
          {data && data.length === 0 && (
            <EmptyState icon={<FlaskConical className="size-5" />} title="No research runs yet">
              Start one from the IPOs, Stocks, Funds or Bonds page: pick a company and press Research. Its live progress shows up here.
            </EmptyState>
          )}
          {data && data.length > 0 && rows.length === 0 && (
            <EmptyState title="No runs match these filters">Clear the search or pick another kind or status.</EmptyState>
          )}
          {rows.length > 0 && view === "cards" && (
            <div className="stagger grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {rows.map((r) => (
                <RunCard key={r.id} r={r} />
              ))}
            </div>
          )}
          {rows.length > 0 && view === "table" && (
            <Table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Company</th>
                  <th>Status</th>
                  <th>Gate</th>
                  <th>Steps</th>
                  <th className="!text-right">Duration</th>
                  <th>Started</th>
                  <th className="!text-right">Report</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <Link href={`/runs/${r.id}`} className="num font-medium text-brand hover:underline">
                        #{r.id}
                      </Link>
                    </td>
                    <td>
                      <div className="flex items-center gap-2.5">
                        <KindIcon kind={r.kind} className="size-7" />
                        <div className="min-w-0">
                          <p className="max-w-56 truncate font-medium" title={r.company_name ?? undefined}>{r.company_name}</p>
                          <p className="text-[11px] text-muted">{kindMeta(r.kind).label}</p>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="flex items-center gap-1">
                        <Badge status={r.status} />
                        {r.worker?.alive && <Badge tone="info" dot>worker</Badge>}
                      </div>
                    </td>
                    <td>
                      <GateBadge gate={r.final_gate} />
                    </td>
                    <td>
                      <StepsBar steps={r.steps} />
                    </td>
                    <td className="num text-right">{fmtDuration(runSeconds(r.created_at, r.finished_at))}</td>
                    <td className="text-muted">{when(r.created_at)}</td>
                    <td className="text-right">
                      {r.has_report ? (
                        <Link className="inline-flex items-center gap-1 font-medium text-brand hover:underline" href={`/runs/${r.id}/report`}>
                          <FileText className="size-3.5" /> Read
                        </Link>
                      ) : (
                        <span className="text-xs text-muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </div>
      </Card>
    </div>
  );
}
