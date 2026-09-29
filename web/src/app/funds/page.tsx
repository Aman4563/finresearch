"use client";

import { ArrowUpRight, ChartLine, FileText, FlaskConical, PieChart } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ensureFund, startResearch } from "@/components/markets/actions";
import { SearchBox } from "@/components/markets/common";
import type { Scheme } from "@/components/markets/types";
import { Badge, Button, Card, EmptyState, ErrorNote, InfoTip, PageHeader, SkeletonRows } from "@/components/ui";
import { api, type Company, day, useApi } from "@/lib/api";

const EXAMPLES = ["parag parikh flexi cap direct", "axis midcap direct", "nifty 50 index direct", "liquid fund direct"];

export default function Funds() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Scheme[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const companies = useApi<Company[]>("/api/companies");
  const researched = (companies.data ?? []).filter((c) => c.kind === "fund_report" && c.latest_run);

  const search = async (query = q) => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      setHits(await api<Scheme[]>(`/api/funds/search?q=${encodeURIComponent(query.trim())}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSearching(false);
    }
  };

  const research = async (s: Scheme) => {
    if (!confirm(`Start a full research run for ${s.name} (${s.plan}, ${s.option})? It uses your Claude plan window.`)) return;
    setBusy(s.scheme_code);
    try {
      router.push(`/runs/${await startResearch(await ensureFund(s.scheme_code), "fund_report")}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  return (
    <div>
      <PageHeader
        icon={<PieChart className="size-5" />}
        eyebrow="Markets"
        title="Mutual funds"
        description="Every scheme in AMFI's daily NAV file: open one for its NAV history, returns, rolling returns, risk and a SIP calculator on its real NAVs, or start a research run."
      />

      <div className="space-y-5">
        <Card title="Find a scheme" subtitle="Search by name words or AMFI scheme code; direct-growth plans are listed first" icon={<PieChart className="size-4" />}>
          <SearchBox value={q} onChange={setQ} onSubmit={() => search()} busy={searching} placeholder="Scheme name or AMFI code, e.g. axis midcap direct" />
          <div className="mt-3">
            <ErrorNote error={error} onRetry={q.trim() ? () => search() : undefined} />
          </div>
          {!hits && (
            <p className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-muted">
              Try
              {EXAMPLES.map((s) => (
                <button key={s} type="button" onClick={() => { setQ(s); search(s); }}
                  className="rounded-full bg-background-subtle px-2 py-0.5 font-medium text-foreground ring-1 ring-inset ring-border transition hover:ring-brand">
                  {s}
                </button>
              ))}
            </p>
          )}
          {hits && hits.length === 0 && (
            <div className="mt-4">
              <EmptyState title="No scheme matches">Use fewer words (e.g. &quot;midcap direct&quot;) or the AMFI scheme code from your statement.</EmptyState>
            </div>
          )}
          {hits && hits.length > 0 && (
            <ul className="mt-4 grid gap-3 md:grid-cols-2 stagger">
              {hits.map((s) => {
                const direct = /direct/i.test(`${s.plan} ${s.name}`);
                const growth = /growth/i.test(`${s.option} ${s.name}`);
                return (
                  <li key={s.scheme_code} className="group flex flex-col rounded-xl border border-border bg-card p-4 transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow">
                    <div className="flex items-start justify-between gap-3">
                      <Link href={`/funds/${s.scheme_code}`} className="min-w-0">
                        <p className="line-clamp-2 text-sm font-semibold group-hover:text-brand">{s.name}</p>
                        <p className="mt-0.5 truncate text-xs text-muted">{s.amc}</p>
                      </Link>
                      <div className="shrink-0 text-right">
                        <p className="num text-base font-semibold">{s.nav ? `₹${Number(s.nav).toLocaleString("en-IN", { maximumFractionDigits: 4 })}` : "—"}</p>
                        <p className="text-[11px] text-muted">NAV {day(s.nav_date)}</p>
                      </div>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {s.category && <Badge tone="info">{s.category.replace(/^.*? - /, "")}</Badge>}
                      <Badge tone={direct ? "gain" : "warn"}>{direct ? "Direct" : s.plan ?? "Regular"}</Badge>
                      <Badge tone="neutral">{growth ? "Growth" : s.option ?? "—"}</Badge>
                      {s.slug && <Badge tone="accent">in library</Badge>}
                      <span className="num ml-auto text-[11px] text-muted">#{s.scheme_code}</span>
                    </div>
                    <div className="mt-3 flex gap-2 border-t border-border/60 pt-3">
                      <Link href={`/funds/${s.scheme_code}`}
                        className="inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg bg-card text-xs font-medium ring-1 ring-inset ring-border transition hover:bg-card-hover hover:ring-border-strong">
                        <ChartLine className="size-3.5" /> Returns &amp; SIP
                      </Link>
                      <Button className="flex-1" disabled={busy === s.scheme_code} onClick={() => research(s)} icon={<FlaskConical className="size-3.5" />}>
                        Research
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          <p className="mt-4 flex items-center gap-1 text-[11px] text-muted">
            Direct vs regular
            <InfoTip>A direct plan is bought straight from the fund house and has no distributor commission, so its expense ratio is lower and its returns higher than the regular plan of the same fund.</InfoTip>
            · Growth reinvests gains; IDCW pays them out.
          </p>
        </Card>

        <Card title="Researched funds" subtitle="Schemes with a fund research report" icon={<FileText className="size-4" />}>
          {companies.error ? (
            <ErrorNote error={companies.error} onRetry={companies.reload} />
          ) : !companies.data ? (
            <SkeletonRows rows={3} />
          ) : researched.length === 0 ? (
            <EmptyState icon={<FlaskConical className="size-5" />} title="No fund research yet">
              Search above and press Research for a full, cited analysis of a scheme: portfolio, costs, risk and peers.
            </EmptyState>
          ) : (
            <ul className="-mx-2 stagger">
              {researched.map((c) => (
                <li key={c.slug} className="flex items-center gap-2 rounded-lg px-2 py-2 transition hover:bg-card-hover">
                  <Link href={`/funds/${c.slug.replace(/^mf-/, "")}`} className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium hover:text-brand">{c.name}</p>
                    <p className="text-xs text-muted">AMFI {c.slug.replace(/^mf-/, "")}</p>
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
