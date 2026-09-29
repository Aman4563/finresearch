"use client";

import Link from "next/link";
import { useState } from "react";

import { AlertList } from "@/components/alerts";
import { Badge, Button, Card, ErrorNote } from "@/components/ui";
import { api, type AlertItem, type Company, useApi, when, type WatchSummary } from "@/lib/api";

export default function Monitor() {
  const watches = useApi<WatchSummary[]>("/api/watches", 60000);
  const alerts = useApi<AlertItem[]>("/api/alerts", 60000);
  const companies = useApi<Company[]>("/api/companies");
  const [slug, setSlug] = useState("");
  const [error, setError] = useState<string | null>(null);

  const add = async () => {
    if (!slug) return;
    // a listed stock gets the daily after-close watch, anything else the IPO timeline
    const kind = companies.data?.find((c) => c.slug === slug)?.kind === "stock_report" ? "stock" : "ipo";
    try {
      await api("/api/watches", { method: "POST", body: JSON.stringify({ company: slug, kind }) });
      setError(null);
      watches.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="space-y-4">
      <Card
        title="Monitored IPOs"
        actions={
          <div className="flex items-center gap-2">
            <select className="rounded border border-border bg-background px-2 py-1 text-sm" value={slug} onChange={(e) => setSlug(e.target.value)}>
              <option value="">Watch a company…</option>
              {companies.data?.filter((c) => c.nse_symbol && (c.kind === "ipo_report" || c.kind === "stock_report")).map((c) => (
                <option key={c.slug} value={c.slug}>
                  {c.name} {c.kind === "stock_report" ? "(listed)" : "(IPO)"}
                </option>
              ))}
            </select>
            <Button onClick={add} disabled={!slug}>
              Watch
            </Button>
          </div>
        }
      >
        <ErrorNote error={error ?? watches.error} />
        <p className="mb-2 text-xs text-muted">
          Checks run inside <code>finresearch serve</code> (or <code>finresearch monitor run</code>): subscription six times a
          bidding day, allotment (T+1), listing open and close (T+3; retried until NSE lists the stock) and anchor
          lock-ins (30 and 90 days). Dates after the close are expected dates until NSE confirms them.
        </p>
        <table className="w-full text-sm">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-1">IPO</th>
              <th>Bidding</th>
              <th>Allotment</th>
              <th>Listing</th>
              <th>Last subscription</th>
              <th>Next check</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {watches.data?.map((w) => (
              <tr key={w.id} className="border-t border-border">
                <td className="py-2">
                  <Link className="font-medium underline" href={`/monitor/${w.id}`}>
                    {w.company_name}
                  </Link>{" "}
                  {!w.active && <Badge status="pending">stopped</Badge>}
                  {!!w.unread_alerts && <Badge status="running">{w.unread_alerts} new</Badge>}
                </td>
                {w.kind === "stock" ? (
                  <td colSpan={3} className="text-xs text-muted">
                    listed stock · daily after-close check (results, corporate actions, holdings, big moves)
                  </td>
                ) : (
                  <>
                    <td>
                      {w.open_date} → {w.close_date}
                    </td>
                    <td>{w.allotment_date}</td>
                    <td>
                      {w.listing_date} {w.meta.listing_confirmed ? "✓" : <span className="text-xs text-muted">(expected)</span>}
                    </td>
                  </>
                )}
                <td>{w.last_subscription ? `${Number(w.last_subscription.total_times).toFixed(2)}x @ ${when(w.last_subscription.as_of)}` : "—"}</td>
                <td className="text-xs">{w.next_check ? `${w.next_check.kind} ${when(w.next_check.due_at)}` : "—"}</td>
                <td className="text-right">
                  {w.active && (
                    <button
                      type="button"
                      className="text-xs underline"
                      onClick={async () => {
                        try {
                          await api(`/api/watches/${w.id}/stop`, { method: "POST" });
                          setError(null);
                          watches.reload();
                        } catch (e) {
                          setError((e as Error).message);
                        }
                      }}
                    >
                      stop
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card title="Alerts">
        <AlertList alerts={alerts.data ?? []} onRead={alerts.reload} />
      </Card>
    </div>
  );
}
