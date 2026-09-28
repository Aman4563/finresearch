"use client";

import { useParams } from "next/navigation";

import { AlertList } from "@/components/alerts";
import { Badge, Card, ErrorNote } from "@/components/ui";
import { useApi, when, type WatchDetail } from "@/lib/api";

const CATS: [string, string][] = [["1", "QIB"], ["2", "NII"], ["3", "Retail"]];

export default function WatchView() {
  const { id } = useParams<{ id: string }>();
  const { data, error, reload } = useApi<WatchDetail>(`/api/watches/${id}`, 60000);
  if (!data) return <ErrorNote error={error} />;
  return (
    <div className="space-y-4">
      <Card title={`${data.company_name} (${data.nse_symbol})`}>
        {data.kind === "stock" ? (
          <p className="text-sm">
            Listed stock, checked after each close: results filings, corporate actions and ex-dates, promoter holding, moves of 5% or more.
            {typeof data.meta.promoter_pct === "string" && ` Last promoter holding ${data.meta.promoter_pct}%.`}
          </p>
        ) : (
          <p className="text-sm">
            Bidding {data.open_date} → {data.close_date} · allotment {data.allotment_date} · listing {data.listing_date}
            {data.meta.listing_confirmed ? " (confirmed by NSE)" : " (expected)"}
            {data.anchor_shares && ` · anchor book ${Number(data.anchor_shares).toLocaleString("en-IN")} shares`}
          </p>
        )}
      </Card>
      <Card title="Subscription (NSE combined NSE+BSE, times subscribed)">
        {data.subscription.length === 0 ? (
          <p className="text-sm text-muted">No snapshots yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-muted">
              <tr>
                <th className="py-1">As of (NSE)</th>
                {CATS.map(([, n]) => <th key={n}>{n}</th>)}
                <th>Total</th>
              </tr>
            </thead>
            <tbody>
              {data.subscription.map((s) => (
                <tr key={s.as_of} className="border-t border-border">
                  <td className="py-1">{when(s.as_of)}</td>
                  {CATS.map(([code, n]) => {
                    const c = s.categories.find((x) => x.code === code);
                    return <td key={n}>{c?.times ? `${Number(c.times).toFixed(2)}x` : "—"}</td>;
                  })}
                  <td className="font-medium">{Number(s.total_times).toFixed(2)}x</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      <Card title="Alerts">
        <AlertList alerts={data.alerts} onRead={reload} />
      </Card>
      <Card title="Scheduled checks">
        <table className="w-full text-xs">
          <tbody>
            {data.jobs.map((j) => (
              <tr key={j.id} className="border-t border-border">
                <td className="py-1">{when(j.due_at)}</td>
                <td>{j.kind}</td>
                <td>
                  <Badge status={j.status} />
                </td>
                <td className="text-muted">{j.error ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.jobs.length === 0 && <p className="text-sm text-muted">Checks are planned on the next monitor pass.</p>}
      </Card>
    </div>
  );
}
