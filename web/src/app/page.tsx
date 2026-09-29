"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote } from "@/components/ui";
import { api, type Company, type Issue, type ResearchKind, useApi, when } from "@/lib/api";

const KIND_LABEL: Record<ResearchKind, string> = {
  ipo_report: "IPO",
  stock_report: "stock",
  fund_report: "fund",
  bond_report: "bond",
};

/** Start a research run; with `issue` (an NSE issue not in the store yet) the company is added first. */
function StartButton({ slug, kind, issue }: { slug?: string; kind: ResearchKind; issue?: Issue }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = slug ?? issue?.symbol;
  const start = async () => {
    if (!confirm(`Start a full ${KIND_LABEL[kind]} research run for ${label}? It uses your Claude plan window.`)) return;
    setBusy(true);
    try {
      const company =
        slug ??
        (
          await api<{ slug: string }>("/api/companies", {
            method: "POST",
            body: JSON.stringify({ nse_symbol: issue!.symbol, name: issue!.company }),
          })
        ).slug;
      const r = await api<{ run_id: number }>("/api/runs", { method: "POST", body: JSON.stringify({ company, kind }) });
      router.push(`/runs/${r.run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <span className="inline-flex items-center gap-2">
      <Button onClick={start} disabled={busy}>
        {busy ? "Starting…" : "Research"}
      </Button>
      {error && <span className="text-xs text-rose-600">{error}</span>}
    </span>
  );
}

const PHASE: Record<string, string> = { open: "running", current: "running", upcoming: "pending", closed: "done" };

export default function Radar() {
  const radar = useApi<{ fetched_at: string; issues: Issue[]; errors: string[] }>("/api/ipos");
  const companies = useApi<Company[]>("/api/companies");

  return (
    <div className="space-y-6">
      <Card
        title="IPO radar (NSE, BSE SME)"
        actions={<span className="text-xs text-muted">{radar.data && `as of ${when(radar.data.fetched_at)}`}</span>}
      >
        <ErrorNote error={radar.error} />
        {radar.data?.errors.map((e) => <ErrorNote key={e} error={e.startsWith("bse:") ? `BSE: ${e.slice(5)}` : `NSE: ${e}`} />)}
        {radar.data && radar.data.issues.length === 0 && <p className="text-sm text-muted">No open or upcoming issues.</p>}
        {radar.data && radar.data.issues.length > 0 && (
          <table className="w-full text-sm">
            <thead className="text-left text-muted">
              <tr>
                <th className="py-1">Issue</th>
                <th>Window</th>
                <th>Price band</th>
                <th>Subscribed</th>
                <th>Research</th>
              </tr>
            </thead>
            <tbody>
              {radar.data.issues.map((i) => (
                <tr key={`${i.exchange}-${i.phase}-${i.symbol}`} className="border-t border-border">
                  <td className="py-2">
                    <div className="font-medium">{i.company}</div>
                    <div className="text-xs text-muted">
                      {i.exchange} {i.symbol} · <Badge status={PHASE[i.phase] ?? "pending"}>{i.phase}</Badge>
                      {i.series && i.series !== "EQ" && <span className="ml-1"><Badge status="unverified">{i.series}</Badge></span>}
                    </div>
                  </td>
                  <td>
                    {i.issue_start} → {i.issue_end}
                  </td>
                  <td>
                    {i.price_band}
                    {i.lot_size ? (
                      <div className="text-xs text-muted">
                        lot {i.lot_size}
                        {i.min_lots ? ` · min ${i.min_lots} lots` : ""}
                      </div>
                    ) : null}
                  </td>
                  <td>{i.times_subscribed ? `${Number(i.times_subscribed).toFixed(2)}x` : ""}</td>
                  <td>
                    {i.latest_run ? (
                      <Link href={`/runs/${i.latest_run}`} className="underline">
                        run {i.latest_run} <Badge status={i.latest_run_status ?? "pending"} />
                      </Link>
                    ) : i.slug ? (
                      <StartButton slug={i.slug} kind="ipo_report" />
                    ) : i.bse_ipo_no ? (
                      <code className="text-xs text-muted">finresearch ipo run … --bse-ipo {i.bse_ipo_no}</code>
                    ) : (
                      <StartButton issue={i} kind="ipo_report" />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Companies in the store">
        <ErrorNote error={companies.error} />
        <table className="w-full text-sm">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-1">Company</th>
              <th>NSE</th>
              <th>Documents</th>
              <th>Kind</th>
              <th>Latest run</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {companies.data?.map((c) => (
              <tr key={c.slug} className="border-t border-border">
                <td className="py-2 font-medium">{c.name}</td>
                <td>{c.nse_symbol}</td>
                <td>{c.documents}</td>
                <td className="text-muted">{KIND_LABEL[c.kind] ?? c.kind}</td>
                <td>
                  {c.latest_run && (
                    <Link className="underline" href={`/runs/${c.latest_run}`}>
                      run {c.latest_run}
                    </Link>
                  )}
                </td>
                <td className="text-right">
                  <StartButton slug={c.slug} kind={c.kind} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
