"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui";
import { ALERTS_CHANGED, api, type AlertItem, useApi, when } from "@/lib/api";

const LEVEL: Record<string, string> = { info: "pending", warn: "unverified", action: "running" };

export function AlertBadge() {
  const { data, reload } = useApi<AlertItem[]>("/api/alerts?unread=true", 60000);
  useEffect(() => {
    window.addEventListener(ALERTS_CHANGED, reload);
    return () => window.removeEventListener(ALERTS_CHANGED, reload);
  }, [reload]);
  if (!data?.length) return null;
  return (
    <Link href="/monitor" className="rounded-full bg-rose-600 px-2 py-0.5 text-xs font-semibold text-white">
      {data.length} alert{data.length === 1 ? "" : "s"}
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
