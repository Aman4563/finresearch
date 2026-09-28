"use client";

import Link from "next/link";

import { Badge } from "@/components/ui";
import { api, type AlertItem, useApi, when } from "@/lib/api";

const LEVEL: Record<string, string> = { info: "pending", warn: "unverified", action: "running" };

export function AlertBadge() {
  const { data } = useApi<AlertItem[]>("/api/alerts?unread=true", 60000);
  if (!data?.length) return null;
  return (
    <Link href="/monitor" className="rounded-full bg-rose-600 px-2 py-0.5 text-xs font-semibold text-white">
      {data.length} alert{data.length === 1 ? "" : "s"}
    </Link>
  );
}

export function AlertList({ alerts, onRead }: { alerts: AlertItem[]; onRead: () => void }) {
  const read = async (id: number) => {
    await api(`/api/alerts/${id}/read`, { method: "POST" });
    onRead();
  };
  if (!alerts.length) return <p className="text-sm text-muted">No alerts yet.</p>;
  return (
    <ul className="space-y-2 text-sm">
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
