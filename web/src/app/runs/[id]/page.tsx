"use client";

import { ArrowLeft, CirclePause, CircleX, Cpu, FileText, Gauge as GaugeIcon, GitBranch, Play, Radio, ShieldAlert, ShieldCheck, Timer, TriangleAlert, Waypoints } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { DonutChart } from "@/components/charts";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, InfoTip, Modal, PageHeader, Progress, Segmented, Skeleton, Stat } from "@/components/ui";
import { PipelineStepper, StepRow, StepTimeline, type StepX, buildLanes } from "@/components/workspace/pipeline";
import { KindIcon, fmtDuration, kindMeta, runSeconds } from "@/components/workspace/run-meta";
import { API_URL, type RunDetail, type Step, api, when } from "@/lib/api";

function useRunEvents(id: string) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [steps, setSteps] = useState<Record<number, Step>>({});
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    const es = new EventSource(`${API_URL}/api/runs/${id}/events`);
    es.addEventListener("open", () => {
      setLive(true);
      setError(null);
    });
    es.addEventListener("snapshot", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as RunDetail;
      setRun(d);
      setSteps(Object.fromEntries(d.steps.map((s) => [s.id, s])));
    });
    es.addEventListener("step", (e) => {
      const s = JSON.parse((e as MessageEvent).data) as Step;
      setSteps((prev) => ({ ...prev, [s.id]: s }));
    });
    es.addEventListener("run", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as Partial<RunDetail>;
      setRun((prev) => (prev ? { ...prev, ...d } : prev));
    });
    es.addEventListener("end", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as { status: string };
      setRun((prev) => (prev ? { ...prev, status: d.status } : prev));
      setLive(false);
      es.close();
      // refresh totals (claims, usage) once the stream ends
      api<RunDetail>(`/api/runs/${id}`).then(setRun).catch(() => undefined);
    });
    es.onerror = () => {
      // a down API leaves the stream reconnecting (CONNECTING), never CLOSED: report every error
      setLive(false);
      setError(
        es.readyState === EventSource.CLOSED
          ? `Lost the event stream from ${API_URL}`
          : `Cannot reach the FinResearch API at ${API_URL}; retrying… (is \`uv run finresearch serve\` running?)`,
      );
    };
    return () => es.close();
  }, [id, epoch]);

  return { run, steps: Object.values(steps) as StepX[], live, error, reconnect: () => setEpoch((x) => x + 1) };
}

const CLAIM_COLOR: Record<string, string> = {
  verified: "var(--gain)", needs_review: "var(--warn)", unverified: "var(--chart-3)", contradicted: "var(--loss)",
  unsupported: "var(--chart-4)", pending: "var(--muted)",
};

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

/** Why the run is paused, failed or stalled, with the way forward. Nothing is shown for a healthy run. */
function RunStateNote({ run, stuckStep, action }: { run: RunDetail; stuckStep?: string; action: React.ReactNode }) {
  const workerAlive = !!run.worker?.alive;
  if (run.stalled) {
    return (
      <Callout tone="warn" title="Stalled: nothing is working on this run" icon={<TriangleAlert className="size-4" />}>
        <p>
          {run.stalled.reason}
          {stuckStep ? <> Step <span className="font-mono text-xs">{stuckStep}</span> was in progress</> : null}
          {run.stalled.since ? <>{stuckStep ? "; " : " "}last activity {when(run.stalled.since)}.</> : stuckStep ? "." : null}
        </p>
        <p className="mt-1 text-xs text-muted">
          Usually the Mac slept, restarted or the worker was stopped. Resuming continues from the last finished step; finished steps are not repeated.
        </p>
        {action && <div className="mt-2.5">{action}</div>}
      </Callout>
    );
  }
  if (run.status === "failed" && run.last_error) {
    return (
      <Callout tone="loss" title="The run failed" icon={<CircleX className="size-4" />}>
        <p className="break-words font-mono text-xs leading-relaxed">{run.last_error.message}</p>
        <p className="mt-1 text-xs text-muted">
          {run.last_error.at ? <>Stopped {when(run.last_error.at)}. </> : null}
          Fix the cause if it needs fixing (for example sign in to Claude Code again), then resume: finished steps are kept.
        </p>
        {action && <div className="mt-2.5">{action}</div>}
      </Callout>
    );
  }
  if (run.status === "paused" && run.pause_kind === "transient") {
    return (
      <Callout tone="warn" title="Paused after a temporary Claude error" icon={<CirclePause className="size-4" />}>
        {run.pause_reason && <p className="break-words">{run.pause_reason}</p>}
        <p className="mt-1 text-xs text-muted">
          {run.resume_after ? <>It resumes automatically after {when(run.resume_after)}</> : <>It resumes automatically</>}
          {workerAlive ? " (its worker is waiting)." : "; you can also resume it now."}
        </p>
        {action && <div className="mt-2.5">{action}</div>}
      </Callout>
    );
  }
  if (run.status === "paused" || run.resume_after) {
    return (
      <Callout tone="warn" title="Paused for the plan window" icon={<GaugeIcon className="size-4" />}>
        The Claude plan&apos;s 5-hour window is nearly used up, so the run waits
        {run.resume_after ? <> and resumes after {when(run.resume_after)}</> : null}.
        {run.pause_reason && <p className="mt-1 text-xs text-muted">{run.pause_reason}</p>}
        {action && <div className="mt-2.5">{action}</div>}
      </Callout>
    );
  }
  return null;
}

function Loading() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Skeleton className="size-11 rounded-xl" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-3 w-32" />
          <Skeleton className="h-6 w-80 max-w-full" />
        </div>
      </div>
      <Skeleton className="h-28 rounded-xl" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Skeleton className="h-96 rounded-xl lg:col-span-2" />
        <Skeleton className="h-96 rounded-xl" />
      </div>
    </div>
  );
}

export default function RunView() {
  const { id } = useParams<{ id: string }>();
  const { run, steps, live, error, reconnect } = useRunEvents(id);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [tab, setTab] = useState<"stages" | "timeline">("stages");
  // a "running" run whose worker has exited (stalled) is not being worked on
  const running = !!run && (!!run.worker?.alive || (run.status === "running" && !run.stalled));
  const now = useNow(running);

  const lanes = useMemo(() => buildLanes(steps), [steps]);

  const resume = async () => {
    setResuming(true);
    try {
      await api(`/api/runs/${id}/resume`, { method: "POST", body: "{}" });
      setResumeError(null);
      setConfirm(false);
      reconnect();
    } catch (e) {
      setResumeError((e as Error).message);
    } finally {
      setResuming(false);
    }
  };

  if (!run) return error ? <ErrorNote error={error} onRetry={reconnect} /> : <Loading />;
  // nothing is working on the run without a live worker (including a "running" run whose worker was killed)
  const canResume = run.status !== "done" && !run.worker?.alive;
  const hasReport = steps.some((s) => s.stage === "synthesis" && s.status === "done");
  const done = steps.filter((s) => s.status === "done").length;
  const current = steps.find((s) => s.status === "running");
  const resumeButton = canResume && (
    <Button variant="secondary" icon={<Play className="size-3.5" />} onClick={() => setConfirm(true)}>
      Resume run
    </Button>
  );
  const claims = Object.entries(run.claims ?? {}).map(([k, v]) => ({ name: k.replaceAll("_", " "), value: v, color: CLAIM_COLOR[k] }));
  const claimTotal = claims.reduce((a, c) => a + c.value, 0);
  const verified = run.claims?.verified ?? 0;
  const secs = runSeconds(run.created_at, run.finished_at, now);
  const shown = stage ? lanes.filter((l) => l.key === stage) : lanes;

  return (
    <div className="space-y-6">
      <Link href="/runs" className="inline-flex items-center gap-1 text-xs text-muted hover:text-foreground">
        <ArrowLeft className="size-3.5" /> All runs
      </Link>
      <PageHeader
        icon={<Waypoints className="size-5" />}
        eyebrow={
          <span className="inline-flex items-center gap-1.5">
            {kindMeta(run.kind).label} research · run <span className="num">#{run.id}</span>
          </span>
        }
        title={run.company_name ?? `Run #${run.id}`}
        description={`Started ${when(run.created_at)}${run.finished_at ? ` · finished ${when(run.finished_at)}` : ""}. Follow each agent step below; the report opens once the writer has finished.`}
        actions={
          <>
            <Badge status={run.status} />
            {run.stalled && <Badge tone="warn">stalled</Badge>}
            {live && (
              <Badge tone="info" dot>
                live
              </Badge>
            )}
            {canResume && (
              <Button variant="secondary" icon={<Play className="size-3.5" />} onClick={() => setConfirm(true)}>
                Resume
              </Button>
            )}
            {hasReport && (
              <Link href={`/runs/${run.id}/report`}
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-brand-fg shadow-sm transition hover:bg-brand-strong hover:shadow-glow">
                <FileText className="size-3.5" /> Read report
              </Link>
            )}
          </>
        }
      />

      <ErrorNote error={error ?? resumeError} onRetry={error ? reconnect : undefined} />
      <RunStateNote run={run} stuckStep={current?.key} action={resumeButton} />

      {/* progress */}
      <Card className="animate-fade-up">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex items-center gap-3">
            <KindIcon kind={run.kind} className="size-10" />
            <div>
              <p className="flex items-center gap-1.5 text-sm font-semibold">
                {running ? (
                  <>
                    <span className="relative flex size-2">
                      <span className="size-2 rounded-full bg-info text-info animate-pulse-ring" />
                    </span>
                    Working{current ? `: ${current.key}` : "…"}
                  </>
                ) : run.status === "done" ? (
                  "Pipeline complete"
                ) : run.stalled ? (
                  "Pipeline stalled: the worker stopped"
                ) : (
                  `Pipeline ${run.status.replaceAll("_", " ")}`
                )}
              </p>
              <p className="text-xs text-muted">
                <span className="num">{done}</span> of <span className="num">{steps.length}</span> steps done
                <InfoTip>The total grows while the run works: when the critic asks for another round, new research, verify and write steps are added.</InfoTip>
              </p>
            </div>
          </div>
          <p className="num text-2xl font-semibold">{steps.length ? Math.round((done / steps.length) * 100) : 0}%</p>
        </div>
        <Progress className="mt-3" value={done} max={Math.max(1, steps.length)} tone={run.status === "failed" ? "loss" : "brand"} />
        <div className="mt-5 border-t border-border pt-4">
          <div className="mb-3 flex items-center gap-1.5 text-xs font-medium text-muted">
            Agent pipeline
            <InfoTip>
              Each research run goes plan → research → verify → bull vs bear → write → critic. Click a stage to see only its steps; hover the ? on a
              stage card to learn what it does.
            </InfoTip>
          </div>
          {lanes.length ? (
            <PipelineStepper lanes={lanes} selected={stage} onSelect={setStage} />
          ) : (
            <p className="text-sm text-muted">No steps yet. The planner starts as soon as a worker picks the run up.</p>
          )}
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card
            title={tab === "stages" ? (stage ? `${shown[0]?.label ?? stage} steps` : "Steps by stage") : "Timeline"}
            icon={<GitBranch className="size-4" />}
            subtitle={tab === "stages" ? "Model, time, turns and plan window used by each agent step" : "When each step ran, on the run's own clock"}
            actions={
              <Segmented
                value={tab}
                onChange={setTab}
                options={[
                  { value: "stages", label: "Stages" },
                  { value: "timeline", label: "Timeline" },
                ]}
              />
            }
          >
            {tab === "timeline" ? (
              <StepTimeline steps={steps} now={now} />
            ) : shown.length === 0 ? (
              <EmptyState title="No steps yet">The worker creates steps as it goes; this page updates live.</EmptyState>
            ) : (
              <div className="space-y-5">
                {stage && (
                  <button type="button" className="text-xs text-brand hover:underline" onClick={() => setStage(null)}>
                    Show all stages
                  </button>
                )}
                {shown.map((l) => (
                  <section key={l.key} className="animate-fade-up">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <h3 className="flex items-center gap-1.5 text-sm font-semibold">
                        {l.label}
                        <InfoTip>{l.help}</InfoTip>
                        <Badge status={l.status} />
                      </h3>
                      <span className="num text-xs text-muted">
                        {l.done}/{l.steps.length} · {fmtDuration(l.seconds)} agent time
                      </span>
                    </div>
                    <p className="mb-2 text-xs text-muted">{l.help}</p>
                    <ul className="stagger grid gap-2 sm:grid-cols-2">
                      {l.steps.map((s) => (
                        <StepRow key={s.id} s={s} />
                      ))}
                    </ul>
                  </section>
                ))}
              </div>
            )}
          </Card>
        </div>

        <div className="order-first space-y-4 lg:order-none">
          <Card
            title="Claims ledger"
            icon={<ShieldCheck className="size-4" />}
            help="Every figure an agent finds is saved as a claim with its source. The verifier checks each one: verified (source confirms it), needs review (not yet confirmed), contradicted (the source says otherwise). Only verified claims can be used as facts in the report."
          >
            {claimTotal ? (
              <DonutChart
                data={claims}
                height={150}
                center={
                  <div>
                    <p className="num text-lg font-semibold">{Math.round((verified / claimTotal) * 100)}%</p>
                    <p className="text-[11px] text-muted">verified</p>
                  </div>
                }
              />
            ) : (
              <p className="text-sm text-muted">No claims yet. Researchers add them as they read the sources.</p>
            )}
          </Card>

          <Card title="Publish gate" icon={run.final_gate?.ok === false ? <ShieldAlert className="size-4" /> : <ShieldCheck className="size-4" />}
            help="Before a report is published, an automatic check makes sure every figure cites a verified claim and no contradicted claim is used. If it blocks, the report is shown with a warning.">
            {run.final_gate ? (
              run.final_gate.ok ? (
                <Callout tone="gain" title="Passed">Every figure in the report is cited to a checked claim.</Callout>
              ) : (
                <Callout tone="loss" title="Blocked">
                  <ul className="list-disc space-y-0.5 pl-4 text-xs">
                    {run.final_gate.blocking.slice(0, 6).map((b) => (
                      <li key={b}>{b}</li>
                    ))}
                  </ul>
                </Callout>
              )
            ) : (
              <p className="text-sm text-muted">Not checked yet: the gate runs after the report is written.</p>
            )}
          </Card>

          <Card title="Plan usage for this run" icon={<Cpu className="size-4" />}
            help="Research runs use your Claude subscription, which allows a certain amount of work per rolling 5-hour window. This is how much of that window this run's steps used, summed step by step.">
            <div className="grid grid-cols-2 gap-3">
              <Stat label="5-hour window" value={run.usage ? run.usage.five_hour_used * 100 : null} format={(n) => `${Math.round(n)}%`} tone="warn" />
              <Stat label="Agent turns" value={run.usage?.turns ?? null} format={(n) => Math.round(n).toLocaleString("en-IN")} tone="accent" />
              <Stat label="Agent time" value={run.usage ? run.usage.minutes * 60 : null} format={fmtDuration} tone="info" />
              <Stat label="Wall clock" display={<span className="num">{fmtDuration(secs)}</span>} icon={<Timer className="size-4" />} />
            </div>
            <p className="mt-3 text-xs text-muted">
              Agent time is longer than the wall clock because steps run in parallel. <Link href="/usage" className="text-brand hover:underline">See plan usage</Link>
            </p>
          </Card>
          {live && (
            <p className="flex items-center gap-1.5 text-xs text-muted">
              <Radio className="size-3.5 text-info" /> Connected: steps update here as they finish.
            </p>
          )}
        </div>
      </div>

      <Modal open={confirm} onClose={() => setConfirm(false)} title="Resume this run?">
        <div className="space-y-3 p-4 text-sm">
          <p>
            Resuming starts a worker that continues from the last finished step. It uses your Claude plan&apos;s 5-hour window, like any research run.
          </p>
          <ErrorNote error={resumeError} />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button onClick={resume} disabled={resuming} icon={<Play className="size-3.5" />}>
              {resuming ? "Starting…" : "Resume run"}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
