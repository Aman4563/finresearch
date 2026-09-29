"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Badge, Button, Card, ErrorNote } from "@/components/ui";
import { api, API_URL, pct, type RunDetail, type Step, when } from "@/lib/api";

const STAGE_ORDER = ["facts", "plan", "stream", "verify", "case", "synthesis", "critic"];

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

  return { run, steps: Object.values(steps), live, error, reconnect: () => setEpoch((x) => x + 1) };
}

function StepCard({ s }: { s: Step }) {
  const util = s.five_hour_after != null ? `${pct(s.five_hour_before)}→${pct(s.five_hour_after)}` : "";
  return (
    <div className="rounded border border-border p-2 text-xs" title={s.error ?? undefined}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium">{s.key}</span>
        <Badge status={s.status} />
      </div>
      <div className="mt-1 text-muted">
        {[s.model, s.num_turns && `${s.num_turns} turns`, s.duration_s && `${Math.round(s.duration_s)}s`, util]
          .filter(Boolean)
          .join(" · ")}
      </div>
      {s.error && <div className="mt-1 line-clamp-2 text-rose-600">{s.error}</div>}
    </div>
  );
}

export default function RunView() {
  const { id } = useParams<{ id: string }>();
  const { run, steps, live, error, reconnect } = useRunEvents(id);
  const [resumeError, setResumeError] = useState<string | null>(null);

  const lanes = useMemo(() => {
    const by: Record<string, Step[]> = {};
    for (const s of steps.sort((a, b) => a.id - b.id)) (by[s.stage] ??= []).push(s);
    const known = STAGE_ORDER.filter((k) => by[k]);
    return [...known, ...Object.keys(by).filter((k) => !STAGE_ORDER.includes(k))].map((k) => [k, by[k]] as const);
  }, [steps]);

  const resume = async () => {
    try {
      await api(`/api/runs/${id}/resume`, { method: "POST", body: "{}" });
      setResumeError(null);
      reconnect();
    } catch (e) {
      setResumeError((e as Error).message);
    }
  };

  if (!run) return error ? <ErrorNote error={error} /> : <p className="text-sm text-muted">Connecting…</p>;
  // nothing is working on the run without a live worker (including a "running" run whose worker was killed)
  const canResume = run.status !== "done" && !run.worker?.alive;
  const hasReport = steps.some((s) => s.stage === "synthesis" && s.status === "done");

  return (
    <div className="space-y-4">
      <Card
        title={`Run #${run.id} · ${run.company_name ?? ""}`}
        actions={
          <div className="flex items-center gap-3 text-sm">
            <Badge status={run.status} />
            {live && <span className="text-xs text-sky-600">● live</span>}
            {canResume && <Button onClick={resume}>Resume</Button>}
            {hasReport && (
              <Link className="underline" href={`/runs/${run.id}/report`}>
                Report
              </Link>
            )}
          </div>
        }
      >
        <ErrorNote error={error ?? resumeError} />
        <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
          <div>
            <dt className="text-muted">Started</dt>
            <dd>{when(run.created_at)}</dd>
          </div>
          <div>
            <dt className="text-muted">Plan window used</dt>
            <dd>{run.usage ? pct(run.usage.five_hour_used) : ""}</dd>
          </div>
          <div>
            <dt className="text-muted">Turns / minutes</dt>
            <dd>{run.usage ? `${run.usage.turns} / ${Math.round(run.usage.minutes)}` : ""}</dd>
          </div>
          <div>
            <dt className="text-muted">Claims</dt>
            <dd className="flex flex-wrap gap-1">
              {Object.entries(run.claims ?? {}).map(([k, v]) => (
                <Badge key={k} status={k}>
                  {v} {k.replace("_", " ")}
                </Badge>
              ))}
            </dd>
          </div>
          <div>
            <dt className="text-muted">Publish gate</dt>
            <dd>{run.final_gate ? <Badge status={run.final_gate.ok ? "done" : "blocked"}>{run.final_gate.ok ? "passed" : "blocked"}</Badge> : "—"}</dd>
          </div>
        </dl>
        {run.resume_after && <p className="mt-2 text-sm text-amber-600">Paused for the plan window; resumes after {when(run.resume_after)}.</p>}
      </Card>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {lanes.map(([stage, list]) => (
          <Card key={stage} title={`${stage} (${list.filter((s) => s.status === "done").length}/${list.length})`}>
            <div className="space-y-2">
              {list.map((s) => (
                <StepCard key={s.id} s={s} />
              ))}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
