"use client";

import { ArrowUpRight, Calculator, FileText, FlaskConical, Landmark, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ensureBond, startResearch } from "@/components/markets/actions";
import { SearchBox, inr, ratingLabel, ratingTone } from "@/components/markets/common";
import type { Bond } from "@/components/markets/types";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, SkeletonRows, Table } from "@/components/ui";
import { api, type Company, day, useApi } from "@/lib/api";

export default function Bonds() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Bond[] | null>(null);
  const [shownQuery, setShownQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const companies = useApi<Company[]>("/api/companies");
  const researched = (companies.data ?? []).filter((c) => c.kind === "bond_report" && c.latest_run);

  const search = async (query = q) => {
    setSearching(true);
    try {
      setHits(await api<Bond[]>(`/api/bonds?q=${encodeURIComponent(query.trim())}`));
      setShownQuery(query.trim());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSearching(false);
    }
  };

  // open with the most traded bonds so the page is useful before any search
  useEffect(() => {
    api<Bond[]>("/api/bonds?limit=25")
      .then((b) => setHits((h) => h ?? b))
      .catch((e: Error) => setError(e.message));
  }, []);

  const research = async (b: Bond) => {
    if (!confirm(`Start a full research run for ${b.symbol} ${b.series ?? ""} (${b.isin})? It uses your Claude plan window.`)) return;
    setBusy(b.isin);
    try {
      router.push(`/runs/${await startResearch(await ensureBond(b.isin), "bond_report")}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  return (
    <div>
      <PageHeader
        icon={<Landmark className="size-5" />}
        eyebrow="Markets"
        title="Bonds"
        description="Bonds and NCDs traded on NSE's capital-market segment. Open one for its yield, after-tax yield for your slab, cash flows and interest-rate sensitivity, or start a research run."
      />

      <div className="space-y-5">
        <Card title={shownQuery ? `Bonds matching “${shownQuery}”` : "Most traded bonds today"} icon={<Landmark className="size-4" />}
          subtitle="Search by issuer symbol fragment or ISIN; leave it empty for the most traded">
          <SearchBox value={q} onChange={setQ} onSubmit={() => search()} busy={searching} placeholder="Symbol or ISIN, e.g. NHAI or INE906B07DF8 (empty: most traded)" />
          <div className="mt-3">
            <ErrorNote error={error} onRetry={() => search()} />
          </div>
          <div className="mt-3">
            <Callout tone="info" icon={<TriangleAlert className="size-4" />} title="Prices here include accrued interest">
              NSE trades and settles these bonds on the dirty price: the quoted price includes the interest earned since the last coupon. The bond page
              splits it into the clean price and accrued interest before working out the yield.
            </Callout>
          </div>
          {!hits && !error ? (
            <div className="mt-4"><SkeletonRows rows={6} /></div>
          ) : hits && hits.length === 0 ? (
            <div className="mt-4">
              <EmptyState title="No listed bond matches">Try part of the NSE symbol (e.g. IRFC, NHAI) or the full 12-character ISIN.</EmptyState>
            </div>
          ) : hits ? (
            <div className="mt-4">
              <Table label="Bond search results">
                <thead>
                  <tr>
                    <th>Bond</th>
                    <th className="text-right!">
                      <span className="inline-flex items-center gap-1">Coupon <InfoTip>The fixed interest the bond pays each year, as a % of its face value.</InfoTip></span>
                    </th>
                    <th>Maturity</th>
                    <th className="text-right!">Last price</th>
                    <th>Rating</th>
                    <th className="text-right!">Actions</th>
                  </tr>
                </thead>
                <tbody className="stagger">
                  {hits.map((b) => (
                    <tr key={`${b.isin}-${b.series}`}>
                      <td>
                        <Link href={`/bonds/${b.isin}`} className="group block">
                          <span className="flex items-center gap-1.5 font-semibold group-hover:text-brand">
                            {b.symbol} <span className="text-xs font-normal text-muted">{b.series}</span>
                            {b.slug && <Badge tone="accent">in library</Badge>}
                          </span>
                          <span className="num block text-xs text-muted">{b.isin} · face {b.face_value ? inr(b.face_value, 0) : "—"}</span>
                        </Link>
                        {(() => {
                          const checks = b.warnings.filter((w) => !w.startsWith("no rating"));
                          return checks.length ? (
                            <span className="mt-0.5 flex max-w-[18rem] items-center gap-1 text-[11px] text-warn" title={checks.join("\n")}>
                              <TriangleAlert className="size-3 shrink-0" />
                              <span className="truncate">{checks[0]}</span>
                              {checks.length > 1 && <span className="shrink-0">+{checks.length - 1}</span>}
                            </span>
                          ) : null;
                        })()}
                      </td>
                      <td className="num text-right">{b.coupon_pct != null ? `${Number(b.coupon_pct).toFixed(2)}%` : "—"}</td>
                      <td className="num text-xs">{day(b.maturity)}</td>
                      <td className="num text-right">{b.last_price ? inr(b.last_price) : "—"}</td>
                      <td>
                        {b.rating ? <Badge tone={ratingTone(b.rating)}>{ratingLabel(b.rating)}</Badge> : <span className="text-xs text-muted">unrated</span>}
                        {b.rating_agency && <span className="block text-[11px] text-muted">{b.rating_agency}</span>}
                      </td>
                      <td>
                        <div className="flex justify-end gap-1.5">
                          <Link href={`/bonds/${b.isin}`}
                            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-card px-3 text-xs font-medium ring-1 ring-inset ring-border transition hover:bg-card-hover hover:ring-border-strong">
                            <Calculator className="size-3.5" /> Yield
                          </Link>
                          <Button disabled={busy === b.isin} onClick={() => research(b)} icon={<FlaskConical className="size-3.5" />}
                            title="Full research run (uses your Claude plan)">
                            <span className="hidden xl:inline">Research</span>
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </div>
          ) : null}
        </Card>

        <Card title="Researched bonds" subtitle="Bonds with a research report" icon={<FileText className="size-4" />}>
          {companies.error ? (
            <ErrorNote error={companies.error} onRetry={companies.reload} />
          ) : !companies.data ? (
            <SkeletonRows rows={2} />
          ) : researched.length === 0 ? (
            <EmptyState icon={<FlaskConical className="size-5" />} title="No bond research yet">
              Pick a bond above and press Research for a cited look at the issuer, rating, security and pricing against alternatives.
            </EmptyState>
          ) : (
            <ul className="-mx-2 stagger">
              {researched.map((c) => (
                <li key={c.slug} className="flex items-center gap-2 rounded-lg px-2 py-2 transition hover:bg-card-hover">
                  <Link href={`/bonds/${c.slug.replace(/^bond-/, "").toUpperCase()}`} className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium hover:text-brand">{c.name}</p>
                    <p className="num text-xs text-muted">{c.slug.replace(/^bond-/, "").toUpperCase()}</p>
                  </Link>
                  <Link href={`/runs/${c.latest_run}/report`}
                    className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-medium text-brand ring-1 ring-inset ring-brand/25 transition hover:bg-brand-soft">
                    Report (run {c.latest_run}) <ArrowUpRight className="size-3" />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
