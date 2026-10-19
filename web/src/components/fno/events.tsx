"use client";

import { CalendarDays } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge, InfoTip } from "@/components/ui";
import { api, day } from "@/lib/api";

import type { EventStripData } from "./book-types";

const TONE = { results: "warn", rbi: "info", expiry: "neutral" } as const;

/** Next results date (stocks), RBI policy dates (when the app has a source) and the next expiries (#308). */
export function EventStrip({ symbol }: { symbol: string }) {
  const [data, setData] = useState<EventStripData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<EventStripData>(`/api/fno/${encodeURIComponent(symbol)}/events`)
      .then((d) => live && (setData(d), setError(null)))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [symbol]);
  if (error) return <p className="text-xs text-muted">Event calendar unavailable: {error}</p>;
  if (!data) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs" aria-label={`Events for ${data.symbol}`}>
      <span className="inline-flex items-center gap-1 text-muted">
        <CalendarDays className="size-3.5" /> Events
        <InfoTip>Results dates come from NSE board-meeting filings; expiries from NSE contract info. An event before expiry can move the price and the IV sharply.</InfoTip>
      </span>
      {data.items.map((x) => (
        <Badge key={`${x.kind}-${x.day}`} tone={TONE[x.kind]}>
          <span title={x.source}>{x.title} · {day(x.day)}</span>
        </Badge>
      ))}
      {data.results.status === "unknown" && <Badge tone="warn">results date unknown ({data.results.note})</Badge>}
      {data.results.status === "none" && <span className="text-muted">no results meeting announced</span>}
      {data.rbi.status === "no_source" && <span className="text-muted">{data.rbi.note}</span>}
    </div>
  );
}
