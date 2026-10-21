"use client";

// The brief's "Market data checks" card (#368): what /api/brief puts under `data_health` (finresearch.dq.report),
// i.e. the monitor's post-load checks per dataset (21:00 and 08:00 IST) and which pages they gate. Unknown means not
// checked, never ok. Also exports the small gate badge the F&O pages show next to numbers that read these datasets.

import { DatabaseZap } from "lucide-react";

import { Badge, Callout, Card, type Tone, cx } from "@/components/ui";
import { day } from "@/lib/api";

export type DqCheck = { check: string; day: string; status: string; critical: boolean; value: number | null; threshold: number | null; note: string | null };
export type DqDataset = {
  dataset: string; label: string; status: "ok" | "warn" | "fail" | "unknown"; day: string; last_loaded: string | null;
  lag_sessions: number | null; failed: string[]; checked_at: string | null; checks: DqCheck[];
};
export type DataGate = { status: "verified" | "unverified" | "unknown"; failed: string[] };
export type DataHealth = {
  status: "ok" | "warn" | "fail" | "unknown"; checked_at: string | null; datasets: DqDataset[];
  dependents: ({ page: string; datasets: string[] } & DataGate)[]; method: string;
};

const TONE: Record<string, Tone> = { ok: "gain", verified: "gain", warn: "warn", fail: "loss", unverified: "loss", unknown: "neutral" };
const LABEL: Record<string, string> = { ok: "ok", warn: "warning", fail: "failed", unknown: "not verified", verified: "verified", unverified: "data unverified" };

export function DataHealthCard({ health }: { health?: DataHealth }) {
  if (!health) return null;
  return (
    <Card title="Market data checks" icon={<DatabaseZap className="size-4" />}
      subtitle={health.checked_at ? `Checked ${day(health.checked_at.slice(0, 10))}. ${health.method}` : health.method}>
      {!health.datasets.length ? (
        <p className="py-3 text-sm text-muted">The checks have not run yet: F&amp;O and index figures are not verified until the monitor&apos;s 21:00 pass.</p>
      ) : (
        <div className="space-y-3 text-sm" data-testid="data-health">
          <ul className="space-y-1.5">
            {health.datasets.map((d) => (
              <li key={d.dataset} className="flex flex-wrap items-baseline gap-2">
                <Badge tone={TONE[d.status]}>{LABEL[d.status]}</Badge>
                <span className="font-medium">{d.label}</span>
                <span className="text-xs text-muted">
                  {d.last_loaded ? `last loaded ${day(d.last_loaded)}` : ""}
                  {d.lag_sessions ? ` · ${d.lag_sessions} session${d.lag_sessions === 1 ? "" : "s"} behind` : ""}
                </span>
                {d.checks.length > 0 && (
                  <ul className="basis-full pl-4 text-xs text-muted">
                    {d.checks.slice(0, 4).map((c) => (
                      <li key={c.day + c.check} className={cx(c.status === "fail" && c.critical && "text-loss")}>
                        {c.check} ({LABEL[c.status] ?? c.status}){c.note ? `: ${c.note}` : ""}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2 text-xs">
            {health.dependents.map((p) => <Badge key={p.page} tone={TONE[p.status]}>{p.page}: {LABEL[p.status]}</Badge>)}
          </div>
        </div>
      )}
    </Card>
  );
}

/** A line above numbers that read checked datasets: nothing when verified, else why they are not. */
export function DataGateNote({ gate }: { gate?: DataGate }) {
  if (!gate || gate.status === "verified") return null;
  return gate.status === "unverified" ? (
    <Callout tone="loss" title="Data unverified">
      A data check failed on {gate.failed.join(", ")}: the figures below may be wrong. See the brief&apos;s market data checks.
    </Callout>
  ) : (
    <Callout tone="neutral" title="Data not verified">The data checks have not passed for these inputs yet (not run, or could not be evaluated).</Callout>
  );
}
