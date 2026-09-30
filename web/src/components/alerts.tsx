"use client";

import { Bell } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui";
import { ALERTS_CHANGED, api, type AlertItem, useApi, when } from "@/lib/api";
import { inQuietHours, useTimeFrames } from "@/lib/timeframes";

const LEVEL: Record<string, string> = { info: "pending", warn: "unverified", action: "running" };

export function AlertBadge() {
  const { data, reload } = useApi<AlertItem[]>("/api/alerts?unread=true", 60000);
  useEffect(() => {
    window.addEventListener(ALERTS_CHANGED, reload);
    return () => window.removeEventListener(ALERTS_CHANGED, reload);
  }, [reload]);
  // quiet hours (profile → Time frames & watch): info alerts are still recorded but do not ring the bell
  const { watch } = useTimeFrames();
  const quiet = inQuietHours(watch);
  const n = (quiet ? data?.filter((a) => a.level !== "info") : data)?.length ?? 0;
  const hidden = quiet ? (data?.length ?? 0) - n : 0;
  const title = (n ? `${n} unread alert${n === 1 ? "" : "s"}` : "No unread alerts") + (quiet ? ` · quiet hours until ${watch.quiet_end}${hidden ? ` (${hidden} info alert${hidden === 1 ? "" : "s"} waiting)` : ""}` : "");
  return (
    <Link href="/monitor" title={title} aria-label={`${n} unread alerts`}
      className="relative grid size-9 place-items-center rounded-lg text-muted transition hover:bg-background-subtle hover:text-foreground">
      <Bell className={n ? "size-4 origin-top animate-[wiggle_1s_ease-in-out_2]" : "size-4"} />
      {n > 0 && (
        <span className="absolute top-1 right-1 grid min-w-4 place-items-center rounded-full bg-loss px-1 text-[10px] font-semibold leading-4 text-white animate-scale-in">
          {n > 99 ? "99+" : n}
        </span>
      )}
    </Link>
  );
}

export function AlertList({ alerts, onRead }: { alerts: AlertItem[]; onRead: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const read = async (id: number) => {
    try {
      await api(`/api/alerts/${id}/read`, { method: "POST" });
      setError(null);
      window.dispatchEvent(new Event(ALERTS_CHANGED));
      onRead();
    } catch (e) {
      setError(`Could not mark the alert read: ${(e as Error).message}`);
    }
  };
  if (!alerts.length) return <p className="text-sm text-muted">No alerts yet.</p>;
  return (
    <ul className="space-y-2 text-sm">
      {error && <li className="text-rose-600">{error}</li>}
      {alerts.map((a) => (
        <li key={a.id} className={`flex items-start gap-2 ${a.read_at ? "opacity-60" : ""}`}>
          <Badge status={LEVEL[a.level]}>{a.kind.replace("_", " ")}</Badge>
          <div className="flex-1">
            <p>{a.message}</p>
            <p className="text-xs text-muted">{when(a.created_at)}</p>
          </div>
          {!a.read_at && (
            <button type="button" className="text-xs underline" onClick={() => read(a.id)}>
              mark read
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
