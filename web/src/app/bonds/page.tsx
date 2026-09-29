"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Card, ErrorNote } from "@/components/ui";
import { api, day } from "@/lib/api";

type Bond = {
  symbol: string;
  series: string | null;
  isin: string;
  coupon_pct: string | null;
  face_value: string | null;
  last_price: string | null;
  maturity: string | null;
  rating: string | null;
  rating_agency: string | null;
  warnings: string[];
  slug: string | null;
};

export default function Bonds() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Bond[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const search = async () => {
    try {
      setHits(await api<Bond[]>(`/api/bonds?q=${encodeURIComponent(q.trim())}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const research = async (b: Bond) => {
    if (!confirm(`Start a full research run for ${b.symbol} ${b.series ?? ""} (${b.isin})? It uses your Claude plan window.`)) return;
    setBusy(b.isin);
    try {
      const made = await api<{ slug: string }>("/api/bonds", { method: "POST", body: JSON.stringify({ isin: b.isin }) });
      const r = await api<{ run_id: number }>("/api/runs", {
        method: "POST",
        body: JSON.stringify({ company: made.slug, kind: "bond_report" }),
      });
      router.push(`/runs/${r.run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  return (
    <Card title="Listed bonds (NSE capital market)">
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          search();
        }}
      >
        <input
          className="flex-1 rounded border border-border bg-background px-2 py-1 text-sm"
          placeholder="Symbol or ISIN, e.g. NHAI or INE906B07DF8 (empty: most traded)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <Button type="submit">Search</Button>
      </form>
      <div className="mt-3">
        <ErrorNote error={error} />
      </div>
      {hits && (
        <table className="mt-3 w-full text-sm">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-1">Bond</th>
              <th>Coupon</th>
              <th>Maturity</th>
              <th>Last price</th>
              <th>Rating</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {hits.map((b) => (
              <tr key={`${b.isin}-${b.series}`} className="border-t border-border">
                <td className="py-2">
                  <div className="font-medium">
                    {b.symbol} {b.series}
                  </div>
                  <div className="text-xs text-muted">
                    {b.isin} · face {b.face_value}
                  </div>
                  {b.warnings.map((w) => (
                    <div key={w} className="text-xs text-amber-600">
                      {w}
                    </div>
                  ))}
                </td>
                <td>{b.coupon_pct != null ? `${b.coupon_pct}%` : ""}</td>
                <td>{day(b.maturity)}</td>
                <td>{b.last_price}</td>
                <td className="text-xs">
                  {b.rating} {b.rating_agency && <span className="text-muted">({b.rating_agency})</span>}
                </td>
                <td className="text-right">
                  <Button disabled={busy === b.isin} onClick={() => research(b)}>
                    Research
                  </Button>
                </td>
              </tr>
            ))}
            {hits.length === 0 && (
              <tr>
                <td colSpan={6} className="py-2 text-muted">
                  No listed bond matches.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </Card>
  );
}
