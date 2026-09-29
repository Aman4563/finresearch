"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Card, ErrorNote } from "@/components/ui";
import { api, when } from "@/lib/api";

type Scheme = {
  scheme_code: string;
  name: string;
  plan: string | null;
  option: string | null;
  category: string | null;
  amc: string | null;
  nav: string | null;
  nav_date: string | null;
  slug: string | null;
};

export default function Funds() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Scheme[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const search = async () => {
    if (!q.trim()) return;
    try {
      setHits(await api<Scheme[]>(`/api/funds/search?q=${encodeURIComponent(q.trim())}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const research = async (s: Scheme) => {
    if (!confirm(`Start a full research run for ${s.name} (${s.plan}, ${s.option})? It uses your Claude plan window.`)) return;
    setBusy(s.scheme_code);
    try {
      const made = await api<{ slug: string }>("/api/funds", { method: "POST", body: JSON.stringify({ scheme_code: s.scheme_code }) });
      const r = await api<{ run_id: number }>("/api/runs", {
        method: "POST",
        body: JSON.stringify({ company: made.slug, kind: "fund_report" }),
      });
      router.push(`/runs/${r.run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  return (
    <Card title="Mutual funds (AMFI)">
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          search();
        }}
      >
        <input
          className="flex-1 rounded border border-border bg-background px-2 py-1 text-sm"
          placeholder="Scheme name or AMFI code, e.g. axis midcap direct"
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
              <th className="py-1">Scheme</th>
              <th>Category</th>
              <th>NAV</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {hits.map((s) => (
              <tr key={s.scheme_code} className="border-t border-border">
                <td className="py-2">
                  <div className="font-medium">{s.name}</div>
                  <div className="text-xs text-muted">
                    {s.scheme_code} · {s.plan} · {s.option} · {s.amc}
                  </div>
                </td>
                <td className="text-xs">{s.category}</td>
                <td>
                  {s.nav} <span className="text-xs text-muted">{when(s.nav_date)}</span>
                </td>
                <td className="text-right">
                  <Button disabled={busy === s.scheme_code} onClick={() => research(s)}>
                    Research
                  </Button>
                </td>
              </tr>
            ))}
            {hits.length === 0 && (
              <tr>
                <td colSpan={4} className="py-2 text-muted">
                  No scheme matches.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </Card>
  );
}
