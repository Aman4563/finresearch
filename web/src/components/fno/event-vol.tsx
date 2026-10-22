"use client";

import { Zap } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge, Card, InfoTip } from "@/components/ui";
import { api, day } from "@/lib/api";

type Implied = { j2: number | null; move: number | null; expected_abs: number | null; median_abs: number | null; method: string | null; reason: string | null };
type ShortGamma = { status: "flag" | "no_flag" | "too_few" | "unknown"; note: string; n: number };
type Upcoming = Implied & { type: string; title: string; day: string; source: string; short_gamma: ShortGamma };
type HistMove = { day: string; title: string; r: number | null; reason: string | null; implied_move: number | null };
type History = { type: string; title: string; n: number; median_abs: number | null; fairness: { n: number; rho: number | null }; moves: HistMove[] };
export type EventVolData = {
  symbol: string; status: "ok" | "no_data" | "not_index"; note?: string; as_of?: string; clock?: "business" | "calendar";
  upcoming?: Upcoming[]; context?: string[]; history?: History[];
  elevation?: { status: "elevated" | "unknown" | "none"; note: string; ex_event_iv: number | null; event_share: number | null };
};

const pct = (x: number | null | undefined, d = 2) => (x == null ? "unknown" : `${(x * 100).toFixed(d)}%`);
const ELEV = { elevated: "warn", unknown: "warn", none: "neutral" } as const;

/** #391: implied event moves from the stored end-of-day term structure and the realised history by event type.
 * Informational: the plan card's probabilities do not use it. Unknown is shown as unknown, never as a 0 % move. */
export function EventVolPanel({ symbol }: { symbol: string }) {
  const [data, setData] = useState<EventVolData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<EventVolData>(`/api/fno/${encodeURIComponent(symbol)}/event-vol`)
      .then((d) => live && (setData(d), setError(null)))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [symbol]);
  if (error) return <p className="text-xs text-muted">Event volatility unavailable: {error}</p>;
  if (!data || data.status === "not_index") return null;
  const help = "Implied event move = the extra variance the near expiry carries over the next one (the term structure), as a one-sigma move. It is unknown when the term structure cannot show it (near IV not above next, no expiry after the event, two events between the same expiries). Past moves are the index's close-to-close log returns across each event, using only events public before it.";
  return (
    <Card title="Event volatility" icon={<Zap className="size-4" />} help={help}
      subtitle={data.as_of ? `from the ${day(data.as_of)} close · ${data.clock} clock · informational` : data.note}>
      <div className="space-y-2 text-xs" aria-label={`Event volatility for ${data.symbol}`}>
        {data.elevation && (
          <p className="flex flex-wrap items-center gap-1.5">
            <Badge tone={ELEV[data.elevation.status]}>IV {data.elevation.status === "elevated" ? "elevated by event" : data.elevation.status === "none" ? "no event inside" : "event effect unknown"}</Badge>
            <span>{data.elevation.note}</span>
            {data.elevation.ex_event_iv != null && <span className="text-muted">ex-event IV {pct(data.elevation.ex_event_iv, 1)}</span>}
          </p>
        )}
        {(data.upcoming ?? []).map((u) => (
          <p key={`${u.type}-${u.day}`} className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">{u.title} · {day(u.day)}</span>
            {u.move == null ? <Badge tone="warn">implied move unknown</Badge> : <span>implied ±{pct(u.move)} (expected |move| {pct(u.expected_abs)})</span>}
            {u.reason && <span className="text-muted">{u.reason}</span>}
            {u.short_gamma.status === "flag" && <Badge tone="warn">short gamma looks cheap</Badge>}
            <InfoTip label="Past moves rule">{u.short_gamma.note}</InfoTip>
          </p>
        ))}
        {!!data.context?.length && <p className="text-muted">Context events in the window: {data.context.join(", ")}</p>}
        {(data.history ?? []).map((h) => (
          <details key={h.type}>
            <summary className="cursor-pointer">
              {h.title}: {h.n} past moves · median |move| {pct(h.median_abs)}
              {h.fairness.rho != null && ` · realised ÷ implied variance ${h.fairness.rho.toFixed(2)} (${h.fairness.n} paired)`}
            </summary>
            <ul className="mt-1 space-y-0.5 pl-4">
              {h.moves.map((m) => (
                <li key={m.day}>{day(m.day)}: {m.r == null ? `unknown (${m.reason})` : pct(m.r)}{m.implied_move != null && ` · implied ±${pct(m.implied_move)}`}</li>
              ))}
            </ul>
          </details>
        ))}
      </div>
    </Card>
  );
}
