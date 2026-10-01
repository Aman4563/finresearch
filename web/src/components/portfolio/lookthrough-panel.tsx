"use client";

// Fund look-through (WP-C): the whole portfolio seen through the funds' published month-end portfolios. True top
// holdings (direct + via funds), sector and market-cap mix, where every rupee sits, overlap between the funds, and
// the holdings files behind it (fetched from the fund house, pasted link or uploaded). Arithmetic only; personal,
// non-advisory. Types live in this file (portfolio/types.ts belongs to the analytics package).

import { ArrowLeft, Download, ExternalLink, FileSpreadsheet, Layers, Link2, Loader2, PieChart, RefreshCw, Upload } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { BarsChart, fmtCompactINR } from "@/components/charts";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, InfoTip, PageHeader, Skeleton, Stat, Table, cx, inputClass } from "@/components/ui";
import { api, useApi } from "@/lib/api";

// ------------------------------------------------------------------ types (API: /api/lookthrough*)
export type FileStatus = {
  available: boolean; as_of?: string | null; month?: string; stale?: boolean; age_days?: number | null; scheme_name?: string;
  sha?: string; url?: string | null; filename?: string; fetched_at?: string; benchmark?: string | null; warnings?: string[];
  equity_pct?: number | null; holdings?: number;
};
export type OverlapPair = {
  overlap_pct: number | null; common: number; equity_a: number | null; equity_b: number | null; months: string[];
  top: { isin: string; name: string; a: number | null; b: number | null; min: number | null }[];
};
type Route = { source: string; value: number | null };
type StockRow = { key: string; name: string; value: number | null; pct: number | null; pct_equity: number | null; sector: string | null; cap: string; kind: string; routes: Route[] };
type Mix = { label: string; value: number | null; pct_equity: number | null };
type Bucket = { kind: string; label: string; value: number | null; pct: number | null };
type HeldFundRow = { code: string | null; name: string; key: string | null; value: number | null; pct: number | null; amc: string | null; source: string | null; file: FileStatus };
type CapMeta = { as_of?: string | null; title?: string; url?: string; fetched_at?: string } | null;
export type LookThrough = {
  as_of: string; total: number | null; equity: number | null; equity_pct: number | null; reconciliation: number | null;
  stocks: StockRow[]; stocks_count: number; sectors: Mix[]; caps: Mix[]; buckets: Bucket[]; redundancy_pct: number | null;
  funds: HeldFundRow[]; overlap: ({ a: string; b: string } & OverlapPair)[]; unpriced: number; cap_list: CapMeta;
  concentration: {
    hhi: number | null; n_effective: number | null; top5_pct_equity: number | null; largest: string | null; largest_pct: number | null;
    top_sector: string | null; top_sector_pct: number | null; fund_coverage_pct: number | null; how: string;
  };
  limits: string[]; disclaimer: string;
};
type Source = { key: string; amc: string; page: string; mode: "auto" | "url"; note: string };
type UploadedScheme = { sheet: string; name: string; key: string; as_of: string | null; holdings: number; benchmark: string | null; warnings: string[]; codes: string[] };

const inr = (v: number | null | undefined) => (v == null ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`);
const pct = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v.toFixed(d)}%`);
const monthLabel = (m?: string | null) => {
  if (!m) return "—";
  const [y, mo] = m.split("-").map(Number);
  return new Date(y, (mo || 1) - 1, 1).toLocaleDateString("en-IN", { month: "short", year: "numeric" });
};

export function readB64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
    r.onerror = () => reject(new Error("could not read the file"));
    r.readAsDataURL(file);
  });
}

const post = <T,>(path: string, body: unknown, method = "POST") => api<T>(path, { method, body: JSON.stringify(body) });

// ------------------------------------------------------------------ file status + actions (shared with the fund card)
export function FileBadge({ file }: { file: FileStatus }) {
  if (!file.available) return <Badge tone="neutral">no holdings file</Badge>;
  return (
    <Badge tone={file.stale ? "warn" : "info"}>
      {monthLabel(file.month)} portfolio{file.stale ? " · stale" : ""}
    </Badge>
  );
}

/** Get a scheme's monthly portfolio: automatic for fund houses with static links, else paste a link or upload. */
export function HoldingsFileActions({ code, auto, urlMode, page, onDone, compact }: {
  code: string | null; auto: boolean; urlMode?: boolean; page?: string | null; onDone: () => void; compact?: boolean;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [found, setFound] = useState<UploadedScheme[] | null>(null);

  const run = async (what: string, fn: () => Promise<string>) => {
    setBusy(what);
    setError(null);
    setNote(null);
    try {
      setNote(await fn());
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const fetchAuto = (months: number) =>
    run(`auto${months}`, async () => {
      const r = await post<{ months: string[]; fetched: unknown[]; errors: { error: string }[]; matched: boolean }>("/api/lookthrough/fetch", { scheme_code: code, months });
      if (!r.matched) throw new Error("the fund house's files were read but none is this scheme (link it after uploading)");
      return `Stored months: ${r.months.map(monthLabel).join(", ")}${r.errors.length ? ` · ${r.errors.length} file(s) failed` : ""}`;
    });
  const fetchUrl = () =>
    run("url", async () => {
      const r = await post<{ schemes: UploadedScheme[] }>("/api/lookthrough/fetch", { url: url.trim() });
      setFound(r.schemes);
      return `Read ${r.schemes.length} scheme portfolio(s) from the file`;
    });
  const upload = (file: File) =>
    run("upload", async () => {
      const r = await post<{ schemes: UploadedScheme[] }>("/api/lookthrough/upload", { filename: file.name, content_b64: await readB64(file) });
      setFound(r.schemes);
      return `Read ${r.schemes.length} scheme portfolio(s) from ${file.name}`;
    });
  const link = (key: string) =>
    run("link", async () => {
      await post("/api/lookthrough/links", { scheme_code: code, key }, "PUT");
      return "Linked";
    });
  const unmatched = found && code ? !found.some((s) => s.codes.includes(code)) : false;

  return (
    <div className={cx("space-y-2", compact ? "text-xs" : "text-sm")}>
      <div className="flex flex-wrap items-center gap-2">
        {auto && code && (
          <>
            <Button size="sm" onClick={() => fetchAuto(1)} disabled={!!busy} icon={busy === "auto1" ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}>
              Fetch latest
            </Button>
            <Button size="sm" variant="secondary" onClick={() => fetchAuto(6)} disabled={!!busy} icon={busy === "auto6" ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}>
              Fetch 6 months
            </Button>
          </>
        )}
        <label className={cx("inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-lg px-3 text-xs font-medium ring-1 ring-inset ring-border transition hover:bg-card-hover", busy && "pointer-events-none opacity-50")}>
          {busy === "upload" ? <Loader2 className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />} Upload file
          <input type="file" accept=".xlsx,.xls,.zip" className="sr-only" onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f); e.target.value = ""; }} />
        </label>
        {page && (
          <a href={page} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-muted hover:text-brand">
            Fund house page <ExternalLink className="size-3" />
          </a>
        )}
      </div>
      {!auto && !urlMode && (
        <p className="text-[11px] text-muted">Download this scheme&apos;s monthly portfolio (xlsx) from the fund house&apos;s website and upload it.</p>
      )}
      {urlMode && (
        <div className="flex flex-wrap items-center gap-2">
          <input className={cx(inputClass, "h-8 min-w-0 flex-1 text-xs")} placeholder="Paste the monthly portfolio file link (https://www.axismf.com/…xlsx)"
            value={url} onChange={(e) => setUrl(e.target.value)} aria-label="Portfolio file link" />
          <Button size="sm" variant="secondary" disabled={!url.trim() || !!busy} onClick={fetchUrl} icon={busy === "url" ? <Loader2 className="size-3.5 animate-spin" /> : <Link2 className="size-3.5" />}>
            Fetch link
          </Button>
        </div>
      )}
      <ErrorNote error={error} />
      {note && <p className="text-xs text-muted">{note}</p>}
      {unmatched && found && found.length > 0 && (
        <div className="rounded-lg border border-border p-2 text-xs">
          <p className="mb-1 text-muted">No scheme in the file matched this fund by name. Pick the right one:</p>
          <div className="flex flex-wrap gap-1.5">
            {found.slice(0, 40).map((s) => (
              <button key={`${s.sheet}-${s.key}`} type="button" onClick={() => link(s.key)}
                className="rounded-md px-2 py-1 ring-1 ring-inset ring-border hover:bg-card-hover">
                {s.name} <span className="text-muted">({monthLabel(s.as_of?.slice(0, 7))})</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ overlap heatmap
/** Sequential one-hue scale (brand), light → strong; the value is printed in the cell, so colour is never alone. */
export function overlapFill(v: number | null | undefined) {
  if (v == null) return "var(--background-subtle)";
  const t = Math.max(0, Math.min(1, v / 100));
  return `color-mix(in oklab, var(--brand) ${Math.round(8 + t * 52)}%, var(--card))`;
}

function OverlapHeatmap({ funds, pairs }: { funds: HeldFundRow[]; pairs: LookThrough["overlap"] }) {
  const withFile = funds.filter((f) => f.file.available);
  const [hover, setHover] = useState<string | null>(null);
  if (withFile.length < 2) {
    return <p className="text-sm text-muted">Overlap needs the holdings files of at least two of your funds.</p>;
  }
  const id = (f: HeldFundRow) => f.code ?? f.name;
  const get = (a: string, b: string) => pairs.find((p) => (p.a === a && p.b === b) || (p.a === b && p.b === a));
  const short = (n: string) => (n.length > 26 ? `${n.slice(0, 24)}…` : n);
  return (
    <div>
      <div tabIndex={0} className="-mx-4 overflow-x-auto px-4 sm:-mx-5 sm:px-5">
        <table className="border-separate border-spacing-[2px] text-xs">
          <thead>
            <tr>
              <th />
              {withFile.map((f) => (
                <th key={id(f)} scope="col" className="max-w-[110px] px-1 pb-1 text-left align-bottom font-medium text-muted" title={f.name}>{short(f.name)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {withFile.map((r) => (
              <tr key={id(r)}>
                <th scope="row" className="max-w-[180px] truncate pr-2 text-left font-medium text-muted" title={r.name}>{short(r.name)}</th>
                {withFile.map((c) => {
                  const same = id(r) === id(c);
                  const p = same ? null : get(id(r), id(c));
                  const key = `${id(r)}|${id(c)}`;
                  const tip = p ? `${r.name} × ${c.name}: ${pct(p.overlap_pct)} overlap, ${p.common} common stocks${p.top[0] ? `; largest: ${p.top[0].name} ${pct(p.top[0].min, 2)}` : ""}` : "";
                  return (
                    <td key={key} onMouseEnter={() => setHover(p ? key : null)} onMouseLeave={() => setHover(null)} title={tip}
                      className={cx("h-11 min-w-[64px] rounded-[4px] text-center num", same ? "text-muted" : "text-foreground", hover === key && "ring-2 ring-foreground/40")}
                      style={{ background: same ? "var(--background-subtle)" : overlapFill(p?.overlap_pct) }}>
                      {same ? "—" : p ? pct(p.overlap_pct, 0) : "n/a"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3 text-[11px] text-muted">
        <span className="inline-flex items-center gap-1.5">0%
          <span className="inline-flex h-2.5 w-28 rounded-sm" style={{ background: "linear-gradient(to right, color-mix(in oklab, var(--brand) 8%, var(--card)), color-mix(in oklab, var(--brand) 60%, var(--card)))" }} />
          100%</span>
        <span>Σ min(weight) over common stocks (SEBI Annexure 1A), equity holdings, % of each fund&apos;s net assets.</span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ hook for the Concentration tab
/** The Concentration tab's funds-as-one-position figures, redone on the looked-through equity (stocks inside funds). */
export function LookthroughConcentration() {
  const { data: d, error } = useApi<LookThrough>("/api/lookthrough?top=5");
  if (error) return null;
  if (!d) return <Skeleton className="h-24" />;
  const c = d.concentration;
  const withFile = d.funds.filter((f) => f.file.available).length;
  return (
    <Card title="Looking through your funds" icon={<Layers className="size-4" />}
      subtitle={withFile ? `${withFile} of ${d.funds.length} funds opened up from their month-end portfolios (${pct(c.fund_coverage_pct, 0)} of fund value)` : "No fund holdings files yet"}
      help={`${c.how} Descriptive: month-end fund portfolios applied to today's values.`}
      actions={<Link href="/portfolio/lookthrough" className="text-xs font-medium text-brand hover:underline">Open look-through →</Link>}>
      {!withFile ? (
        <p className="text-sm text-muted">Add the funds&apos; monthly portfolio files on the look-through page to see stock and sector exposure through them.</p>
      ) : (
        <div className="grid grid-cols-2 gap-3 text-xs lg:grid-cols-4">
          <div><p className="text-muted">Effective no. of stocks</p><p className="num text-base font-semibold">{c.n_effective ?? "—"}</p><p className="text-[11px] text-muted">HHI {c.hhi ?? "—"}</p></div>
          <div><p className="text-muted">Top 5 stocks</p><p className="num text-base font-semibold">{pct(c.top5_pct_equity)}</p><p className="text-[11px] text-muted">of look-through equity</p></div>
          <div><p className="text-muted">Largest stock</p><p className="num text-base font-semibold">{pct(c.largest_pct)}</p><p className="truncate text-[11px] text-muted" title={c.largest ?? ""}>{c.largest ?? "—"} · of the portfolio</p></div>
          <div><p className="text-muted">Largest sector</p><p className="num text-base font-semibold">{pct(c.top_sector_pct)}</p><p className="truncate text-[11px] text-muted" title={c.top_sector ?? ""}>{c.top_sector ?? "—"} · of the portfolio</p></div>
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ the page
export function LookthroughPanel() {
  const lt = useApi<LookThrough>("/api/lookthrough?top=30");
  const sources = useApi<{ sources: Source[] }>("/api/lookthrough/sources");
  const d = lt.data;
  const srcBy = new Map((sources.data?.sources ?? []).map((s) => [s.key, s]));
  const funds = d?.funds ?? [];
  const withFile = funds.filter((f) => f.file.available).length;
  const stale = funds.filter((f) => f.file.stale).length;

  return (
    <div>
      <Link href="/portfolio" className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-muted transition hover:text-brand">
        <ArrowLeft className="size-3.5" /> Portfolio
      </Link>
      <PageHeader
        icon={<Layers className="size-5" />}
        eyebrow="Portfolio"
        title="Look-through"
        description="What you really own: your stocks plus every stock inside your funds, from the fund houses' month-end portfolios."
        actions={<Button size="md" variant="secondary" onClick={lt.reload} icon={<RefreshCw className="size-4" />}>Refresh</Button>}
      />
      <div className="space-y-5">
        {lt.error && <ErrorNote error={`Could not compute the look-through: ${lt.error}`} onRetry={lt.reload} />}
        {d && d.total === 0 && (
          <EmptyState icon={<PieChart className="size-5" />} title="No holdings yet"
            action={<Link href="/portfolio"><Button size="md">Import your portfolio</Button></Link>}>
            Import a CAS or broker tradebook on the portfolio page; the look-through then opens your funds.
          </EmptyState>
        )}
        <div className="grid [&>*]:min-w-0 grid-cols-2 gap-3 lg:grid-cols-4 stagger">
          {!d ? Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />) : (
            <>
              <Stat label="Portfolio value" value={d.total} format={(n) => fmtCompactINR(n)} icon={<PieChart className="size-4" />}
                hint={d.unpriced ? `${d.unpriced} holding(s) without a price are left out` : `as of ${d.as_of}`} />
              <Stat label="Equity, looked through" value={d.equity_pct} format={(n) => `${n.toFixed(1)}%`} icon={<Layers className="size-4" />}
                hint={`${inr(d.equity)} in ${d.stocks_count} stocks`}
                help="Your direct stocks plus each fund's stocks × the value of your units in that fund. Hedged arbitrage positions, bonds, REITs and cash are not counted as equity." />
              <Stat label="Held more than once" value={d.redundancy_pct} format={(n) => `${n.toFixed(1)}%`} icon={<Layers className="size-4" />}
                hint="of equity, via 2+ routes"
                help="Share of your look-through equity in stocks that you own through two or more funds, or through a fund and directly. A description of hidden concentration, not a signal." />
              <Stat label="Funds with a holdings file" value={withFile} format={(n) => `${n} of ${funds.length}`} icon={<FileSpreadsheet className="size-4" />}
                hint={stale ? `${stale} older than 45 days` : funds.length ? "month-end portfolios" : "no funds held"} />
            </>
          )}
        </div>

        <Callout tone="info" title="Personal arithmetic, not advice">
          {d?.disclaimer ?? "Arithmetic on published month-end portfolios."} Numbers use each fund&apos;s latest stored month-end file; see the limits below.
        </Callout>

        <Card title="Holdings files" icon={<FileSpreadsheet className="size-4" />}
          subtitle="SEBI requires every fund house to publish each scheme's month-end portfolio (with ISINs) within 10 days"
          help="SEBI Master Circular for Mutual Funds, para 6.1.1. PPFAS, Nippon India and DSP publish plain links the app can fetch; for Axis paste the file's link; for any other fund house download the xlsx from its website and upload it.">
          {!d ? <Skeleton className="h-24" /> : funds.length === 0 ? (
            <p className="text-sm text-muted">No mutual funds in the portfolio.</p>
          ) : (
            <div className="divide-y divide-border">
              {funds.map((f) => {
                const s = f.source ? srcBy.get(f.source) : undefined;
                return (
                  <div key={f.code ?? f.name} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 lg:flex-row lg:items-start lg:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        {f.code ? <Link href={`/funds/${f.code}`} className="font-medium hover:text-brand">{f.name}</Link> : <span className="font-medium">{f.name}</span>}
                        <FileBadge file={f.file} />
                      </div>
                      <p className="mt-0.5 text-xs text-muted">
                        <span className="num">{inr(f.value)}</span> · {pct(f.pct)} of the portfolio{f.amc ? ` · ${f.amc}` : ""}
                        {f.file.available && <> · equity <span className="num">{pct(f.file.equity_pct)}</span> of the fund</>}
                      </p>
                    </div>
                    <div className="lg:max-w-[460px] lg:flex-1">
                      <HoldingsFileActions code={f.code} auto={s?.mode === "auto"} urlMode={s?.mode === "url"} page={s?.page} onDone={lt.reload} compact />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card title="Your true top holdings" icon={<Layers className="size-4" />}
          subtitle="Direct shares plus the same company inside your funds, largest first"
          help="Exposure to a stock = your direct holding + Σ over funds (value of your units × the stock's % of that fund's net assets). Month-end weights applied to today's fund values.">
          {!d ? <Skeleton className="h-64" /> : d.stocks.length === 0 ? (
            <p className="text-sm text-muted">No equity exposure yet: add a holdings file for your equity funds above.</p>
          ) : (
            <Table label="Your true top holdings">
              <thead>
                <tr><th>Company</th><th>Sector</th><th>Size</th><th className="!text-right">Exposure</th><th className="!text-right">% of portfolio</th><th>Through</th></tr>
              </thead>
              <tbody>
                {d.stocks.map((s) => (
                  <tr key={s.key}>
                    <td className="max-w-[240px] truncate font-medium" title={s.name}>{s.name}</td>
                    <td className="text-xs text-muted">{s.sector ?? "—"}</td>
                    <td className="text-xs">{s.cap}</td>
                    <td className="num text-right">{inr(s.value)}</td>
                    <td className="num text-right">{pct(s.pct, 2)}</td>
                    <td>
                      <div className="flex flex-wrap gap-1">
                        {s.routes.map((r) => (
                          <span key={r.source} className="rounded bg-background-subtle px-1.5 py-0.5 text-[11px] text-muted" title={`${r.source}: ${inr(r.value)}`}>
                            {r.source.length > 22 ? `${r.source.slice(0, 20)}…` : r.source}
                          </span>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
          <Card title="Sectors" icon={<PieChart className="size-4" />} subtitle="% of your look-through equity"
            help="Industry labels from the fund houses' files (SEBI/AMFI industry names); a stock you hold only directly keeps the NSE label.">
            {!d ? <Skeleton className="h-56" /> : d.sectors.length === 0 ? <p className="text-sm text-muted">No equity yet.</p> : (
              <BarsChart layout="vertical" data={d.sectors.slice(0, 12).map((x) => ({ label: x.label.length > 24 ? `${x.label.slice(0, 22)}…` : x.label, pct: x.pct_equity ?? 0 }))} x="label"
                series={[{ key: "pct", label: "% of equity" }]} format={(v) => `${v.toFixed(1)}%`} labelWidth={150}
                height={Math.max(160, Math.min(12, d.sectors.length) * 28 + 20)} />
            )}
          </Card>
          <Card title="Company size" icon={<PieChart className="size-4" />} subtitle="% of your look-through equity, by AMFI's list"
            help={<>Large = top 100 companies by full market cap, mid = 101–250, small = the rest (SEBI). By ISIN from AMFI&apos;s six-monthly list{d?.cap_list?.as_of ? ` (six months ended ${d.cap_list.as_of})` : ""}. The Jun-2026 list could not be downloaded on 30-Sep-2026 [unverified: newer list]. Foreign shares are shown apart.</>}>
            {!d ? <Skeleton className="h-56" /> : d.caps.length === 0 ? <p className="text-sm text-muted">No equity yet.</p> : (
              <BarsChart layout="vertical" data={d.caps.map((x) => ({ label: x.label, pct: x.pct_equity ?? 0 }))} x="label"
                series={[{ key: "pct", label: "% of equity" }]} format={(v) => `${v.toFixed(1)}%`} labelWidth={110}
                height={Math.max(140, d.caps.length * 34 + 20)} />
            )}
            {d && !d.cap_list && <p className="mt-2 text-xs text-warn">AMFI&apos;s market-cap list is not downloaded yet: sizes read &quot;Unclassified&quot;.</p>}
          </Card>
        </div>

        <Card title="Where every rupee sits" icon={<Layers className="size-4" />}
          subtitle="The funds opened up into what they hold; adds up to the portfolio value"
          help="Each fund's value split by its published holdings: equity, hedged arbitrage, REITs, bonds, government securities, other funds' units, and the rest (TREPS, cash, net receivables = 100% minus the listed holdings). Funds without a file stay whole.">
          {!d ? <Skeleton className="h-40" /> : (
            <>
              <BarsChart layout="vertical" data={d.buckets.map((b) => ({ label: b.label, pct: b.pct ?? 0 }))} x="label"
                series={[{ key: "pct", label: "% of portfolio" }]} format={(v) => `${v.toFixed(1)}%`} labelWidth={190}
                height={Math.max(120, d.buckets.length * 30 + 20)} />
              <p className="mt-2 text-xs text-muted">
                Reconciliation: buckets − portfolio value = <span className="num">{inr(d.reconciliation)}</span>
                <InfoTip>Every rupee of every fund is assigned to exactly one bucket, so this is zero by construction; a non-zero value would be a bug.</InfoTip>
              </p>
            </>
          )}
        </Card>

        <Card title="Overlap between your funds" icon={<Layers className="size-4" />}
          subtitle="How much of two funds is the same stocks, at the same weights"
          help="SEBI's method (Master Circular for MFs, Annexure 1A): for each stock both funds hold, take the smaller weight; add them up. 0% = nothing in common, 100% = identical. Overlap does not predict returns; it shows paying twice for the same exposure.">
          {!d ? <Skeleton className="h-40" /> : <OverlapHeatmap funds={d.funds} pairs={d.overlap} />}
        </Card>

        {d && (
          <Card title="Method and limits" icon={<FileSpreadsheet className="size-4" />}>
            <ul className="list-disc space-y-1 pl-5 text-xs text-muted">
              {d.limits.map((l) => <li key={l}>{l}</li>)}
              <li>Rules of thumb (e.g. what counts as &quot;high&quot; overlap) are not applied here: the numbers are shown as they are.</li>
            </ul>
          </Card>
        )}
      </div>
    </div>
  );
}
