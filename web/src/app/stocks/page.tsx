"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Card, ErrorNote } from "@/components/ui";
import { api, type Company, useApi } from "@/lib/api";

type Hit = { symbol: string; name: string; series: string; listed: string | null; isin: string; slug: string | null };

export default function Stocks() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const companies = useApi<Company[]>("/api/companies");

  const search = async () => {
    if (!q.trim()) return;
    try {
      setHits(await api<Hit[]>(`/api/stocks/search?q=${encodeURIComponent(q.trim())}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const ensure = async (symbol: string) =>
    (await api<{ slug: string }>("/api/companies", { method: "POST", body: JSON.stringify({ nse_symbol: symbol }) })).slug;

  const research = async (symbol: string) => {
    if (!confirm(`Start a full stock research run for ${symbol}? It uses your Claude plan window.`)) return;
    setBusy(symbol);
    try {
      const slug = await ensure(symbol);
      const r = await api<{ run_id: number }>("/api/runs", {
        method: "POST",
        body: JSON.stringify({ company: slug, kind: "stock_report" }),
      });
      router.push(`/runs/${r.run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  const watch = async (symbol: string) => {
    setBusy(symbol);
    try {
      const slug = await ensure(symbol);
      await api("/api/watches", { method: "POST", body: JSON.stringify({ company: slug, kind: "stock" }) });
      setNote(`Watching ${symbol}: results filings, corporate actions, holding changes and big moves are checked after each close.`);
      companies.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-4">
      <Card title="Listed stocks (NSE)">
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            search();
          }}
        >
          <input
            className="flex-1 rounded border border-border bg-background px-2 py-1 text-sm"
            placeholder="Symbol or company name, e.g. INFY or Infosys"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <Button type="submit">Search</Button>
        </form>
        <div className="mt-3">
          <ErrorNote error={error} />
          {note && <p className="text-sm text-emerald-600">{note}</p>}
        </div>
        {hits && (
          <table className="mt-3 w-full text-sm">
            <thead className="text-left text-muted">
              <tr>
                <th className="py-1">Symbol</th>
                <th>Company</th>
                <th>Listed</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {hits.map((h) => (
                <tr key={h.symbol} className="border-t border-border">
                  <td className="py-2 font-medium">{h.symbol}</td>
                  <td>{h.name}</td>
                  <td className="text-muted">{h.listed}</td>
                  <td className="space-x-2 text-right">
                    <Button disabled={busy === h.symbol} onClick={() => research(h.symbol)}>
                      Research
                    </Button>
                    <Button disabled={busy === h.symbol} onClick={() => watch(h.symbol)}>
                      Watch
                    </Button>
                  </td>
                </tr>
              ))}
              {hits.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-2 text-muted">
                    No listed equity matches.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </Card>
      <Card title="Researched companies">
        <ul className="space-y-1 text-sm">
          {companies.data
            ?.filter((c) => c.latest_run)
            .map((c) => (
              <li key={c.slug}>
                <span className="font-medium">{c.name}</span> <span className="text-muted">{c.nse_symbol}</span> ·{" "}
                <Link className="underline" href={`/runs/${c.latest_run}/report`}>
                  latest report (run {c.latest_run})
                </Link>
              </li>
            ))}
        </ul>
      </Card>
    </div>
  );
}
