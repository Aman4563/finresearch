"use client";

// Data health (#219): how complete the data behind each portfolio analysis is, what each gap blocks and how to fix it,
// with one overall completeness figure (GET /api/portfolio/health; weights stated in the info tip). A check that could
// not run is shown as unknown and counts as 0 %, never as complete.

import { AlertTriangle, CheckCircle2, CircleSlash, HeartPulse, HelpCircle, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge, Card, ErrorNote, InfoTip, Progress, SkeletonRows, cx } from "@/components/ui";
import { useApi } from "@/lib/api";

type Status = "ok" | "partial" | "missing" | "unknown" | "not_applicable";
type Row = {
  key: string; label: string; weight: number; coverage_pct: number | null; status: Status;
  detail: string; blocks: string; fix: string; href: string;
};
type Health = { as_of: string; rows: Row[]; overall: { pct: number | null; verdict: string; unknown: string[]; how: string }; privacy: string };

const STATUS: Record<Status, { label: string; tone: "gain" | "warn" | "loss" | "neutral"; icon: typeof CheckCircle2 }> = {
  ok: { label: "complete", tone: "gain", icon: CheckCircle2 },
  partial: { label: "partial", tone: "warn", icon: AlertTriangle },
  missing: { label: "missing", tone: "loss", icon: XCircle },
  unknown: { label: "unknown", tone: "neutral", icon: HelpCircle },
  not_applicable: { label: "not needed", tone: "neutral", icon: CircleSlash },
};

const toneOf = (pct: number | null) => (pct == null ? "neutral" : pct >= 90 ? "gain" : pct >= 60 ? "warn" : "loss");

export function DataHealth({ refresh }: { refresh: number }) {
  const { data, error, reload } = useApi<Health>(`/api/portfolio/health?r=${refresh}`);
  const [open, setOpen] = useState(false);
  if (error) return <div className="mb-4"><ErrorNote error={`Data health: ${error}`} onRetry={reload} /></div>;
  const gaps = data?.rows.filter((r) => r.status !== "ok" && r.status !== "not_applicable") ?? [];
  const pct = data?.overall.pct ?? null;
  return (
    <Card className="mb-4" title="Data health" icon={<HeartPulse className="size-4" />}
      help={<>How complete the data behind the analysis is. {data?.overall.how}</>}
      actions={data && <button type="button" onClick={() => setOpen((o) => !o)} className="text-xs font-medium text-brand">{open ? "Hide details" : gaps.length ? `Show ${gaps.length} gap${gaps.length > 1 ? "s" : ""}` : "Show details"}</button>}>
      {!data ? <SkeletonRows rows={2} /> : (
        <div className="space-y-3" data-testid="data-health">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <p className="num text-2xl font-semibold">{pct == null ? "—" : `${pct.toFixed(0)}%`}<span className="ml-1 text-xs font-normal text-muted">complete</span></p>
            <p className="min-w-0 flex-1 text-sm">{data.overall.verdict}</p>
          </div>
          <Progress value={pct} max={100} tone={toneOf(pct)} />
          {open && (
            <ul className="divide-y divide-border">
              {data.rows.map((r) => {
                const st = STATUS[r.status];
                const Icon = st.icon;
                return (
                  <li key={r.key} className="grid gap-x-4 gap-y-1 py-2.5 sm:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
                    <div className="space-y-1">
                      <p className="flex items-center gap-1.5 text-sm font-medium">
                        <Icon className={cx("size-3.5 shrink-0", st.tone === "gain" ? "text-gain" : st.tone === "warn" ? "text-warn" : st.tone === "loss" ? "text-loss" : "text-muted")} />
                        {r.label}
                      </p>
                      <div className="flex items-center gap-2">
                        <Badge tone={st.tone}>{st.label}</Badge>
                        <span className="num text-xs text-muted">{r.coverage_pct == null ? "—" : `${r.coverage_pct.toFixed(0)}%`}</span>
                        <InfoTip>{`Weight ${r.weight} of 100 in the overall figure.`}</InfoTip>
                      </div>
                      {r.status !== "not_applicable" && r.coverage_pct != null && <Progress value={r.coverage_pct} max={100} tone={toneOf(r.coverage_pct)} className="max-w-[15rem]" />}
                    </div>
                    <div className="space-y-0.5 text-xs">
                      <p className="text-foreground/90">{r.detail}</p>
                      {r.status !== "ok" && r.status !== "not_applicable" && (
                        <>
                          <p className="text-muted"><span className="font-medium text-foreground/80">Blocks: </span>{r.blocks}</p>
                          <p>{r.href.startsWith("/portfolio#")
                            // a tab on this page: a plain hash link fires the page's hashchange listener
                            ? <a href={r.href.slice("/portfolio".length)} className="font-medium text-brand hover:underline">{r.fix} →</a>
                            : <Link href={r.href} className="font-medium text-brand hover:underline">{r.fix} →</Link>}</p>
                        </>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}
