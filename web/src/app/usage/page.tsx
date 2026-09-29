"use client";

import { BarChart3, Clock, Cpu, Gauge as GaugeIcon, Hourglass, Info, PauseCircle, Server } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { BarsChart, Gauge } from "@/components/charts";
import { Badge, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, Segmented, Skeleton, Table } from "@/components/ui";
import { fmtDuration, kindMeta } from "@/components/workspace/run-meta";
import { ApiError, type RunDetail, type RunSummary, api, pct, useApi, when } from "@/lib/api";

type Snapshot = Record<string, number | string | null>;
type TierState = { snapshot?: Snapshot; cooling_until?: number; reason?: string | null; failures?: number };
type Limits = { tiers: Record<string, TierState>; now: number; ceilings: { five_hour: number } };
type RunUsage = {
  run_id: number; kind: string; status: string; company_name: string | null; created_at: string | null;
  five_hour_used: number; turns: number; minutes: number; steps: number;
};

const TIER_LABEL: Record<string, { label: string; note: string }> = {
  claude_max: { label: "Claude plan", note: "Your Claude subscription, used by the research agents" },
  local: { label: "Local models", note: "Models running on this Mac; they do not use the Claude plan" },
};

function useNow() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function countdown(targetS: number | undefined, now: number) {
  if (!targetS) return null;
  const s = targetS - now / 1000;
  if (s <= 0) return "resetting now";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h ${m}m`;
  return `${h ? `${h}h ` : ""}${String(m).padStart(h ? 2 : 1, "0")}m ${String(sec).padStart(2, "0")}s`;
}

/** Per-run usage: the /api/usage/runs endpoint, or (against an older API) the run details one by one. */
function useRunUsage() {
  const [data, setData] = useState<RunUsage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const rows = await api<RunUsage[]>("/api/usage/runs?limit=30").catch(async (e) => {
          if (!(e instanceof ApiError) || e.status !== 404) throw e;
          const runs = await api<RunSummary[]>("/api/runs?limit=20");
          const details = await Promise.all(runs.map((r) => api<RunDetail>(`/api/runs/${r.id}`)));
          return details.map((d) => ({
            run_id: d.id, kind: d.kind, status: d.status, company_name: d.company_name, created_at: d.created_at,
            five_hour_used: d.usage.five_hour_used, turns: d.usage.turns, minutes: d.usage.minutes, steps: d.steps.length,
          }));
        });
        if (alive) {
          setData(rows);
          setError(null);
        }
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    })();
    return () => {
      alive = false;
    };
  }, [tick]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

function WindowGauge({ title, used, resets, ceiling, now, help }: {
  title: string; used?: number; resets?: number; ceiling?: number; now: number; help: string;
}) {
  return (
    <div className="flex flex-col items-center rounded-xl border border-border bg-background-subtle/40 px-3 pt-4 pb-3">
      <p className="mb-2 flex items-center gap-1 text-xs font-medium text-muted">
        {title}
        <InfoTip>{help}</InfoTip>
      </p>
      <Gauge value={resets && resets * 1000 <= now ? 0 : (used ?? null)} size={160} label={ceiling ? `runs pause at ${pct(ceiling)}` : "used"} />
      <div className="mt-3 w-full space-y-1 border-t border-border pt-2 text-center text-xs">
        {resets && resets * 1000 <= now ? (
          <p className="text-muted">
            <span className="font-medium text-gain">Window has reset.</span> This reading is from before; the next agent step takes a fresh one.
          </p>
        ) : (
          <p className="flex items-center justify-center gap-1 text-muted">
            <Hourglass className="size-3.5" /> resets in <span className="num font-semibold text-foreground">{countdown(resets, now) ?? "—"}</span>
          </p>
        )}
        {resets ? <p className="text-[11px] text-muted">{when(new Date(resets * 1000).toISOString())}</p> : null}
      </div>
    </div>
  );
}

export default function Usage() {
  const { data, error, reload } = useApi<Limits>("/api/limits", 30000);
  const runs = useRunUsage();
  const now = useNow();
  const [metric, setMetric] = useState<"five_hour_used" | "minutes" | "turns">("five_hour_used");

  const chart = useMemo(
    () =>
      (runs.data ?? [])
        .slice(0, 15)
        .reverse()
        .map((r) => ({
          name: `#${r.run_id} ${kindMeta(r.kind).label}`,
          five_hour_used: Math.round(r.five_hour_used * 1000) / 10,
          minutes: Math.round(r.minutes),
          turns: r.turns,
        })),
    [runs.data],
  );
  const usedRange = useMemo(() => {
    const v = (runs.data ?? []).filter((r) => r.status === "done" && r.five_hour_used > 0).map((r) => Math.round(r.five_hour_used * 100));
    return v.length ? [Math.min(...v), Math.max(...v)] : null;
  }, [runs.data]);
  const fmt = metric === "five_hour_used" ? (v: number) => `${v}%` : metric === "minutes" ? (v: number) => `${v}m` : (v: number) => String(v);

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<GaugeIcon className="size-5" />}
        eyebrow="You"
        title="Plan usage"
        description="How much of your Claude plan the research agents have used, when it resets, and which runs used the most."
      />

      <ErrorNote error={error} onRetry={reload} />

      {!data && !error && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Skeleton className="h-72 rounded-xl" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      )}

      {data && (
        <div className="stagger grid gap-4 lg:grid-cols-2">
          {Object.entries(data.tiers).map(([tier, st]) => {
            const s = st.snapshot ?? {};
            const cooling = !!st.cooling_until && st.cooling_until > now / 1000;
            const meta = TIER_LABEL[tier] ?? { label: tier.replaceAll("_", " "), note: "Model tier" };
            const has = Object.keys(s).length > 0;
            return (
              <Card key={tier} title={meta.label} subtitle={meta.note} icon={tier === "local" ? <Server className="size-4" /> : <Cpu className="size-4" />}
                actions={
                  cooling ? <Badge tone="warn">cooling down</Badge> : has ? <Badge status={s.status === "allowed" ? "ok" : String(s.status ?? "")}>{String(s.status ?? "ok")}</Badge> : <Badge tone="neutral">idle</Badge>
                }>
                {has ? (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <WindowGauge title="5-hour window" used={s.five_hour_utilization as number | undefined} resets={s.five_hour_resets_at as number | undefined}
                      ceiling={data.ceilings.five_hour} now={now}
                      help="Claude plans allow a certain amount of work in any rolling 5-hour period. Research runs pause automatically before the ceiling and resume after the reset." />
                    <WindowGauge title="7-day window" used={s.seven_day_utilization as number | undefined} resets={s.seven_day_resets_at as number | undefined}
                      now={now} help="A second, weekly allowance on top of the 5-hour one. When it is full, nothing runs until it resets." />
                  </div>
                ) : (
                  <EmptyState icon={<Server className="size-5" />} title="No usage recorded">
                    {tier === "local" ? "Local models have no plan limits; nothing to track here." : "Usage appears after the first agent step runs on this tier."}
                  </EmptyState>
                )}
                {cooling && (
                  <div className="mt-3">
                    <Callout tone="warn" icon={<PauseCircle className="size-4" />} title={`Cooling down until ${when(new Date(st.cooling_until! * 1000).toISOString())}`}>
                      {st.reason}
                    </Callout>
                  </div>
                )}
                {!!st.failures && <p className="mt-3 text-xs text-warn">{st.failures} consecutive transient failures</p>}
                {typeof s.observed_at === "number" && (
                  <p className="mt-3 flex items-center gap-1 text-[11px] text-muted">
                    <Clock className="size-3" /> last reading {when(new Date(s.observed_at * 1000).toISOString())}
                  </p>
                )}
              </Card>
            );
          })}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card title="Usage per research run" icon={<BarChart3 className="size-4" />} className="lg:col-span-2"
          subtitle="Latest runs, oldest on the left"
          help="Plan window = the share of the 5-hour window the run's steps used, added up step by step. Agent minutes add up all steps, including ones that ran in parallel.">
          <div className="-mt-1 mb-3">
            <Segmented value={metric} onChange={setMetric} options={[
              { value: "five_hour_used", label: "Window %" },
              { value: "minutes", label: "Minutes" },
              { value: "turns", label: "Turns" },
            ]} />
          </div>
          <ErrorNote error={runs.error} onRetry={runs.reload} />
          {!runs.data && !runs.error ? (
            <Skeleton className="h-64 rounded-lg" />
          ) : chart.length ? (
            <BarsChart data={chart} x="name" height={260} format={fmt}
              series={[{ key: metric, label: metric === "five_hour_used" ? "5-hour window used" : metric === "minutes" ? "Agent minutes" : "Agent turns" }]} />
          ) : runs.data ? (
            <EmptyState title="No runs yet">Start a research run and its plan usage shows up here.</EmptyState>
          ) : null}
        </Card>

        <Card title="How the plan window works" icon={<Info className="size-4" />}>
          <ul className="space-y-3 text-sm text-foreground/90">
            <li className="flex gap-2">
              <span className="num mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-brand-soft text-[11px] text-brand">1</span>
              <span>
              Your Claude subscription allows a fixed amount of work per rolling <b>5-hour</b> window and per <b>7-day</b> week.
              </span>
            </li>
            <li className="flex gap-2">
              <span className="num mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-brand-soft text-[11px] text-brand">2</span>
              <span>
              {usedRange
                ? `Your recent runs used ${usedRange[0]}–${usedRange[1]}% of a 5-hour window each. `
                : "Each research run uses a share of the 5-hour window. "}
              Asking about a report or getting a suggestion uses a little.
              </span>
            </li>
            <li className="flex gap-2">
              <span className="num mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-brand-soft text-[11px] text-brand">3</span>
              <span>
              {data ? `At ${pct(data.ceilings.five_hour)} ` : "Near the limit "}a run pauses itself and resumes after the window resets, so it never fails halfway.
              </span>
            </li>
          </ul>
          <Link href="/runs" className="mt-4 inline-block text-xs font-medium text-brand hover:underline">
            See research runs →
          </Link>
        </Card>
      </div>

      {runs.data && runs.data.length > 0 && (
        <Card title="Run details" padded>
          <Table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Company</th>
                <th>Status</th>
                <th className="!text-right">Window</th>
                <th className="!text-right">Turns</th>
                <th className="!text-right">Agent time</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.run_id}>
                  <td>
                    <Link href={`/runs/${r.run_id}`} className="num font-medium text-brand hover:underline">
                      #{r.run_id}
                    </Link>
                  </td>
                  <td className="max-w-64 truncate">{r.company_name}</td>
                  <td>
                    <Badge status={r.status} />
                  </td>
                  <td className="num text-right">{pct(r.five_hour_used)}</td>
                  <td className="num text-right">{r.turns.toLocaleString("en-IN")}</td>
                  <td className="num text-right">{fmtDuration(r.minutes * 60)}</td>
                  <td className="text-muted">{when(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}
    </div>
  );
}
