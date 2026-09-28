"use client";

import { Card, ErrorNote } from "@/components/ui";
import { pct, useApi, when } from "@/lib/api";

type Snapshot = Record<string, number | string | null>;
type TierState = { snapshot?: Snapshot; cooling_until?: number; reason?: string | null; failures?: number };

function Meter({ label, used, ceiling, resets }: { label: string; used?: number; ceiling?: number; resets?: number }) {
  if (used == null) return null;
  const color = ceiling && used >= ceiling ? "bg-rose-500" : used > 0.7 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div>
      <div className="flex justify-between text-sm">
        <span>{label}</span>
        <span>
          {pct(used)}
          {resets ? <span className="text-muted"> · resets {when(new Date(resets * 1000).toISOString())}</span> : null}
        </span>
      </div>
      <div className="relative mt-1 h-2 rounded bg-background">
        <div className={`h-2 rounded ${color}`} style={{ width: `${Math.min(100, used * 100)}%` }} />
        {ceiling && <div className="absolute top-0 h-2 w-px bg-foreground" style={{ left: `${ceiling * 100}%` }} title="ceiling" />}
      </div>
    </div>
  );
}

export default function Usage() {
  const { data, error } = useApi<{ tiers: Record<string, TierState>; now: number; ceilings: { five_hour: number } }>("/api/limits", 30000);
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      {data &&
        Object.entries(data.tiers).map(([tier, st]) => {
          const s = st.snapshot ?? {};
          const cooling = st.cooling_until && st.cooling_until > data.now;
          return (
            <Card key={tier} title={tier}>
              <div className="space-y-3">
                <Meter label="5-hour window" used={s.five_hour_utilization as number | undefined} ceiling={data.ceilings.five_hour}
                  resets={s.five_hour_resets_at as number | undefined} />
                <Meter label="7-day window" used={s.seven_day_utilization as number | undefined}
                  resets={s.seven_day_resets_at as number | undefined} />
                {cooling && (
                  <p className="text-sm text-amber-600">
                    Cooling down until {when(new Date(st.cooling_until! * 1000).toISOString())}: {st.reason}
                  </p>
                )}
                {!!st.failures && <p className="text-sm text-muted">{st.failures} consecutive transient failures</p>}
                {!Object.keys(s).length && <p className="text-sm text-muted">No usage recorded yet.</p>}
              </div>
            </Card>
          );
        })}
    </div>
  );
}
