"use client";

// The agent pipeline of a research run: a stage stepper, the steps of each stage and a Gantt-style timeline.

import { AlertTriangle, Check, CircleDashed, Loader2, RotateCcw, X } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge, InfoTip, cx } from "@/components/ui";
import { STAGES, fmtDuration, shortModel, stageMeta, stepLabel } from "@/components/workspace/run-meta";
import { type Step, pct, when } from "@/lib/api";

export type StepX = Step & { cost_usd_est?: number | null };

export type Lane = { key: string; label: string; help: string; steps: StepX[]; status: string; done: number; seconds: number };

const ORDER = ["facts", ...STAGES.map((s) => s.key).filter((k) => k !== "facts")];

export function buildLanes(steps: StepX[]): Lane[] {
  const by: Record<string, StepX[]> = {};
  for (const s of [...steps].sort((a, b) => a.id - b.id)) (by[s.stage] ??= []).push(s);
  const keys = [...ORDER.filter((k) => by[k]), ...Object.keys(by).filter((k) => !ORDER.includes(k))];
  return keys.map((k) => {
    const list = by[k];
    const done = list.filter((s) => s.status === "done").length;
    const status = list.some((s) => s.status === "running")
      ? "running"
      : list.some((s) => s.status === "failed" || s.status === "blocked")
        ? "failed"
        : done === list.length
          ? "done"
          : "pending";
    const m = stageMeta(k);
    return { key: k, label: m.label, help: m.help, steps: list, status, done, seconds: list.reduce((a, s) => a + (s.duration_s ?? 0), 0) };
  });
}

function StageDot({ status, index }: { status: string; index: number }) {
  const base = "relative z-10 grid size-9 shrink-0 place-items-center rounded-full ring-4 ring-card transition";
  if (status === "done") return <span className={cx(base, "bg-gain text-white")}><Check className="size-4" /></span>;
  if (status === "running")
    return (
      <span className={cx(base, "bg-info text-white")}>
        <span className="absolute inset-0 rounded-full text-info animate-pulse-ring" />
        <Loader2 className="size-4 animate-spin" />
      </span>
    );
  if (status === "failed") return <span className={cx(base, "bg-loss text-white")}><X className="size-4" /></span>;
  return <span className={cx(base, "bg-background-subtle text-muted ring-card")}><span className="num text-xs">{index + 1}</span></span>;
}

/** Horizontal stepper on wide screens, vertical list on phones. Click a stage to focus its steps. */
export function PipelineStepper({ lanes, selected, onSelect }: { lanes: Lane[]; selected: string | null; onSelect: (k: string | null) => void }) {
  return (
    <ol className="grid gap-1 sm:grid-flow-col sm:auto-cols-fr sm:gap-0">
      {lanes.map((l, i) => {
        const last = i === lanes.length - 1;
        const active = selected === l.key;
        return (
          <li key={l.key} className="relative">
            {/* connector */}
            {!last && (
              <>
                <span aria-hidden className={cx("absolute top-[18px] left-[calc(50%+18px)] hidden h-0.5 w-[calc(100%-36px)] sm:block", l.status === "done" ? "bg-gain/60" : "bg-border")} />
                <span aria-hidden className={cx("absolute top-9 bottom-[-4px] left-[18px] w-0.5 sm:hidden", l.status === "done" ? "bg-gain/60" : "bg-border")} />
              </>
            )}
            <button
              type="button"
              onClick={() => onSelect(active ? null : l.key)}
              aria-pressed={active}
              className={cx(
                "flex w-full items-center gap-3 rounded-xl p-0 text-left transition sm:flex-col sm:gap-2 sm:px-1 sm:py-2 sm:text-center",
                active ? "sm:bg-brand-soft/60" : "sm:hover:bg-background-subtle",
              )}
            >
              <StageDot status={l.status} index={i} />
              <span className="min-w-0 py-2 sm:py-0">
                <span className={cx("flex items-center gap-1 text-sm font-medium sm:justify-center", active && "text-brand")}>{l.label}</span>
                <span className="num block text-[11px] text-muted">
                  {l.done}/{l.steps.length} · {fmtDuration(l.seconds)}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function StepStatusIcon({ status }: { status: string }) {
  if (status === "done") return <Check className="size-3.5 text-gain" />;
  if (status === "running") return <Loader2 className="size-3.5 animate-spin text-info" />;
  if (status === "failed" || status === "blocked") return <AlertTriangle className="size-3.5 text-loss" />;
  return <CircleDashed className="size-3.5 text-muted" />;
}

export function StepRow({ s }: { s: StepX }) {
  const l = stepLabel(s);
  const delta = s.five_hour_after != null && s.five_hour_before != null ? s.five_hour_after - s.five_hour_before : null;
  return (
    <li className={cx("rounded-lg border px-3 py-2.5 transition", s.status === "running" ? "border-info/40 bg-info-soft/40" : "border-border bg-card hover:border-border-strong")}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <StepStatusIcon status={s.status} />
        <span className="min-w-0 flex-1 truncate text-sm font-medium capitalize">{l.name}</span>
        {l.round && <Badge tone="accent">round {l.round.slice(1)}</Badge>}
        {l.pass && <Badge tone="neutral">{l.pass}</Badge>}
        {l.fix && <Badge tone="warn">{l.fix.replace("fix", "fix ")}</Badge>}
        {s.attempts > 1 && (
          <Badge tone="warn">
            <RotateCcw className="size-3" /> {s.attempts} attempts
          </Badge>
        )}
        <Badge status={s.status} />
      </div>
      <dl className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted">
        {s.model && (
          <div>
            <dt className="sr-only">Model</dt>
            <dd className="num">{shortModel(s.model)}{s.tier ? ` · ${s.tier.replace("_", " ")}` : ""}</dd>
          </div>
        )}
        <div>
          <dt className="sr-only">Duration</dt>
          <dd className="num">{fmtDuration(s.duration_s)}</dd>
        </div>
        {s.num_turns != null && (
          <div>
            <dt className="sr-only">Turns</dt>
            <dd className="num">{s.num_turns} turns</dd>
          </div>
        )}
        {delta != null && (
          <div title={`5-hour window ${pct(s.five_hour_before)} → ${pct(s.five_hour_after)}`}>
            <dt className="sr-only">Plan window</dt>
            <dd className="num">+{Math.round(delta * 100)}% window</dd>
          </div>
        )}
      </dl>
      {/* a running or deferred step's error is a retry in progress or a pause, not a failure */}
      {s.error && (
        <p className={`mt-1.5 line-clamp-3 rounded px-2 py-1 text-xs ${s.status === "failed" ? "bg-loss-soft text-loss" : "bg-warn-soft text-warn"}`}>{s.error}</p>
      )}
    </li>
  );
}

const STAGE_COLOR: Record<string, string> = {
  facts: "var(--chart-5)", plan: "var(--chart-2)", stream: "var(--chart-1)", verify: "var(--chart-3)",
  case: "var(--chart-6)", synthesis: "var(--chart-5)", critic: "var(--chart-4)",
};

/** Gantt-style: each step as a bar on the run's clock, so parallel work and the critic's rounds are visible. */
export function StepTimeline({ steps, now }: { steps: StepX[]; now: number }) {
  const [hover, setHover] = useState<StepX | null>(null);
  const rows = useMemo(() => {
    const timed = steps.filter((s) => s.started_at).sort((a, b) => new Date(a.started_at!).getTime() - new Date(b.started_at!).getTime());
    if (!timed.length) return null;
    const t0 = new Date(timed[0].started_at!).getTime();
    const t1 = Math.max(...timed.map((s) => (s.finished_at ? new Date(s.finished_at).getTime() : now)));
    const span = Math.max(1, t1 - t0);
    return {
      span,
      list: timed.map((s) => {
        const a = new Date(s.started_at!).getTime() - t0;
        const b = (s.finished_at ? new Date(s.finished_at).getTime() : now) - t0;
        return { s, left: (a / span) * 100, width: Math.max(0.6, ((b - a) / span) * 100) };
      }),
    };
  }, [steps, now]);
  if (!rows) return <p className="text-sm text-muted">No step has started yet.</p>;
  const stages = [...new Set(steps.map((s) => s.stage))];
  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-3 text-[11px] text-muted">
        {stages.map((k) => (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span className="size-2.5 rounded-sm" style={{ background: STAGE_COLOR[k] ?? "var(--muted)" }} />
            {stageMeta(k).label}
          </span>
        ))}
        <span className="ml-auto num">total {fmtDuration(rows.span / 1000)}</span>
      </div>
      <div className="relative space-y-1" onMouseLeave={() => setHover(null)}>
        {rows.list.map(({ s, left, width }) => (
          <div key={s.id} className="group relative h-5 rounded bg-background-subtle/60" onMouseEnter={() => setHover(s)}>
            <div
              className={cx("absolute inset-y-0.5 rounded-sm transition-opacity", hover && hover.id !== s.id && "opacity-40", s.status === "running" && "animate-pulse")}
              style={{ left: `${left}%`, width: `${width}%`, background: STAGE_COLOR[s.stage] ?? "var(--muted)" }}
            />
            <span
              className={cx("pointer-events-none absolute inset-y-0 flex items-center truncate text-[10px] font-medium text-foreground/80", left > 60 ? "pr-1" : "pl-1")}
              style={left > 60 ? { right: `${100 - left}%` } : { left: `calc(${left + width}% + 2px)` }}
            >
              {stepLabel(s).name}
              {stepLabel(s).round ? ` · ${stepLabel(s).round}` : ""}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-3 min-h-8 text-xs text-muted">
        {hover ? (
          <>
            <span className="font-medium text-foreground">{hover.key}</span> · {stageMeta(hover.stage).label} · {shortModel(hover.model) ?? "—"} ·{" "}
            <span className="num">{fmtDuration(hover.duration_s)}</span> · started {when(hover.started_at)}
          </>
        ) : (
          <>
            Hover a bar for details. Bars side by side ran in parallel.{" "}
            <InfoTip>Researchers run up to four at a time; each verifier starts as soon as its researcher finishes.</InfoTip>
          </>
        )}
      </p>
    </div>
  );
}
