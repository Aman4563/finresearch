"use client";

import {
  ArrowLeft, BarChart3, BookOpen, Download, FileSearch, FileSpreadsheet, FileText, FileType, FolderOpen, Globe, LayoutDashboard, MessagesSquare,
  ShieldAlert, ShieldCheck, Sparkles, X,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { AskPanel } from "@/components/ask";
import { EvidencePanel } from "@/components/evidence";
import { SuggestionPanel } from "@/components/suggestion";
import { Button, Callout, Card, ErrorNote, Modal, Skeleton, SkeletonRows, cx } from "@/components/ui";
import { plain } from "@/components/workspace/report-parts";
import { ChartsTab } from "@/components/workspace/report/charts-tab";
import { EvidenceTab } from "@/components/workspace/report/evidence-tab";
import { FilesTab, type PackEntry } from "@/components/workspace/report/files-tab";
import { FullReport } from "@/components/workspace/report/full-report";
import { SummaryTab } from "@/components/workspace/report/summary";
import { type Claim, type Report, useApi } from "@/lib/api";
import type { Insights } from "@/lib/insights";
import { fileUrl, viewerHref } from "@/lib/viewer";

const TABS = [
  { id: "summary", label: "Summary", icon: <LayoutDashboard className="size-4" /> },
  { id: "charts", label: "Charts", icon: <BarChart3 className="size-4" /> },
  { id: "report", label: "Full report", icon: <BookOpen className="size-4" /> },
  { id: "evidence", label: "Evidence", icon: <FileSearch className="size-4" /> },
  { id: "files", label: "Files", icon: <FolderOpen className="size-4" /> },
] as const;
type TabId = (typeof TABS)[number]["id"];

/** The tab named by the URL hash; a section anchor (#sec-12) belongs to the full report. */
function tabFromHash(hash: string): TabId {
  const h = hash.replace(/^#/, "");
  if (h.startsWith("sec-")) return "report";
  return (TABS.find((t) => t.id === h)?.id ?? "summary") as TabId;
}

function useWide() {
  const [wide, setWide] = useState(true);
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const on = () => setWide(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

const FILE_ICON: Record<string, ReactNode> = {
  pdf: <FileType className="size-3.5" />, html: <Globe className="size-3.5" />, xlsx: <FileSpreadsheet className="size-3.5" />, md: <FileText className="size-3.5" />,
};

export default function ReportReader() {
  const { id } = useParams<{ id: string }>();
  const { data, error, reload } = useApi<Report>(`/api/runs/${id}/report`);
  const pack = useApi<{ files: string[]; entries?: PackEntry[] }>(`/api/runs/${id}/pack`);
  const insights = useApi<Insights>(`/api/runs/${id}/insights`);
  const ledger = useApi<Claim[]>(`/api/runs/${id}/claims`);
  const [open, setOpen] = useState<number | null>(null);
  const [asking, setAsking] = useState(false);
  const [tab, setTab] = useState<TabId>("summary");
  const wide = useWide();

  // the tab lives in the URL hash so a link or reload lands on the same view
  useEffect(() => {
    const sync = () => setTab(tabFromHash(window.location.hash));
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  const goTab = useCallback((t: string) => {
    const next = tabFromHash(t);
    setTab(next);
    history.replaceState(null, "", `#${next}`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const claims = useMemo(() => {
    const m: Record<string, Claim> = {};
    for (const c of ledger.data ?? []) m[String(c.id)] = c;
    return { ...m, ...(data?.claims ?? {}) };
  }, [ledger.data, data]);
  const cited = useMemo(() => new Set(Object.keys(data?.claims ?? {}).map(Number)), [data]);
  const title = useMemo(() => plain(data?.markdown.match(/^#\s+(.+)$/m)?.[1] ?? `Run #${id} report`), [data, id]);
  const entries = useMemo<PackEntry[] | null>(
    () => (pack.data ? (pack.data.entries ?? pack.data.files.map((f) => ({ path: f, size: null }))) : null),
    [pack.data],
  );
  const downloads = (pack.data?.files ?? []).filter((f) => f.startsWith("06_Final_Report/") && /\.(pdf|html|xlsx|md)$/.test(f));
  const side = wide && (open != null || asking);
  const dupExt = (ext: string) => downloads.filter((f) => f.endsWith(`.${ext}`)).length > 1;
  const openClaim = useCallback((cid: number) => setOpen(cid), []);

  if (error)
    return (
      <div className="space-y-4">
        <Link href={`/runs/${id}`} className="inline-flex items-center gap-1 text-xs text-muted hover:text-foreground">
          <ArrowLeft className="size-3.5" /> Run #{id}
        </Link>
        <ErrorNote error={error} onRetry={reload} />
      </div>
    );
  if (!data)
    return (
      <div className="space-y-6">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-10 rounded-xl" />
        <Skeleton className="h-56 rounded-xl" />
        <div className="grid gap-4 md:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24 rounded-xl" />)}
        </div>
        <div className="space-y-3 rounded-xl border border-border bg-card p-6">
          <SkeletonRows rows={8} />
        </div>
      </div>
    );

  const suggestion = data.kind === "ipo_report" && (
    <Card title="My suggestion" icon={<Sparkles className="size-4" />} subtitle="How much to bid, if at all, for your profile and rules"
      help="A personal, rule-checked suggestion built from this report plus live subscription data. It is saved to your Journal so you can record what you did.">
      <SuggestionPanel runId={id} />
    </Card>
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link href={`/runs/${id}`} className="inline-flex items-center gap-1 text-xs text-muted hover:text-foreground">
          <ArrowLeft className="size-3.5" /> Run #{id} pipeline
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          {downloads.map((f) => {
            const ext = f.split(".").pop() ?? "";
            const file = { run: Number(id), path: f };
            const chip = "inline-flex h-8 items-center gap-1.5 rounded-lg bg-card px-2.5 text-xs font-medium ring-1 ring-inset ring-border transition hover:bg-card-hover hover:ring-border-strong";
            const label = <span className={dupExt(ext) ? "max-w-32 truncate" : "uppercase"}>{dupExt(ext) ? f.split("/").pop() : ext}</span>;
            if (ext === "xlsx")
              return (
                <a key={f} href={fileUrl(file, true)} title={`Download ${f.split("/").pop()}`} className={chip}>
                  {FILE_ICON[ext]}
                  {label}
                </a>
              );
            if (ext !== "pdf")
              return (
                <Link key={f} href={viewerHref(file)} title={`View ${f.split("/").pop()}`} className={chip}>
                  {FILE_ICON[ext] ?? <FileText className="size-3.5" />}
                  {label}
                </Link>
              );
            return (
              <span key={f} className="inline-flex h-8 items-stretch overflow-hidden rounded-lg bg-card text-xs font-medium ring-1 ring-inset ring-border">
                <Link href={viewerHref(file)} title={`View ${f.split("/").pop()} here`} className="inline-flex items-center gap-1.5 px-2.5 transition hover:bg-card-hover">
                  {FILE_ICON.pdf}
                  {dupExt(ext) ? label : <span>View PDF</span>}
                </Link>
                <a href={fileUrl(file, true)} title={`Download ${f.split("/").pop()}`} aria-label={`Download ${f.split("/").pop()}`} className="grid w-8 place-items-center border-l border-border text-muted transition hover:bg-card-hover hover:text-foreground">
                  <Download className="size-3.5" />
                </a>
              </span>
            );
          })}
          <Button variant={asking ? "secondary" : "primary"} icon={asking ? <X className="size-3.5" /> : <MessagesSquare className="size-3.5" />} onClick={() => setAsking((a) => !a)}>
            {asking ? "Hide chat" : "Ask about this report"}
          </Button>
        </div>
      </div>

      {!data.published ? (
        <Callout tone="loss" icon={<ShieldAlert className="size-4" />} title="Not published: the publish gate blocked this report">
          <p className="mb-1 text-xs">Read it with care. The gate found problems it could not fix:</p>
          <ul className="list-disc space-y-0.5 pl-4 text-xs">
            {data.gate.blocking.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </Callout>
      ) : (
        <Callout tone="gain" icon={<ShieldCheck className="size-4" />} title="Passed the publish gate">
          <p className="text-xs">
            Every figure cites a claim in the ledger. Click any{" "}
            <span className="num rounded border border-gain/40 bg-gain-soft px-1 text-[0.68rem] text-gain">C123</span> chip, chart bar or number to see its source and quote.
          </p>
        </Callout>
      )}
      {data.gate.warnings.length > 0 && (
        <details className="group rounded-xl border border-warn/30 bg-warn-soft/40 px-4 py-2.5 text-sm">
          <summary className="cursor-pointer font-medium text-warn">
            {data.gate.warnings.length} gate warning{data.gate.warnings.length === 1 ? "" : "s"}
            <span className="ml-1 font-normal text-muted">(claims kept only with an UNVERIFIED caveat, and similar)</span>
          </summary>
          <ul className="mt-2 max-h-60 list-disc space-y-1 overflow-auto pl-5 text-xs text-muted">
            {data.gate.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </details>
      )}

      {/* tabs: sticky under the app header, scrollable on phones */}
      <div className="sticky top-16 z-20 -mx-4 border-b border-border bg-background/85 px-4 backdrop-blur sm:mx-0 sm:rounded-xl sm:border sm:px-1.5">
        <div role="tablist" aria-label="Report views" className="flex gap-1 overflow-x-auto py-1.5 [scrollbar-width:none]">
          {TABS.map((t) => (
            <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} aria-label={t.label} onClick={() => goTab(t.id)}
              className={cx(
                "inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition",
                tab === t.id ? "bg-card text-foreground shadow-card ring-1 ring-inset ring-border" : "text-muted hover:bg-card-hover hover:text-foreground",
              )}>
              <span className={tab === t.id ? "text-brand" : ""}>{t.icon}</span>
              <span className={tab === t.id ? "" : "hidden sm:inline"}>{t.label}</span>
              {t.id === "evidence" && ledger.data && <span className="num rounded-full bg-background-subtle px-1.5 text-[10px] text-muted">{ledger.data.length}</span>}
              {t.id === "files" && entries && <span className="num rounded-full bg-background-subtle px-1.5 text-[10px] text-muted">{entries.length}</span>}
            </button>
          ))}
        </div>
      </div>

      <div className={cx("grid grid-cols-1 gap-6", side && "lg:grid-cols-[minmax(0,1fr)_24rem]")}>
        <div key={tab} role="tabpanel" className="min-w-0 animate-fade-in">
          {tab === "summary" && (
            <SummaryTab report={data} ins={insights.data} claims={claims} onOpen={openClaim} title={title} goTab={goTab} extra={suggestion} />
          )}
          {tab === "charts" && <ChartsTab ins={insights.data} error={insights.error} reload={insights.reload} claims={claims} onOpen={openClaim} />}
          {tab === "report" && <FullReport markdown={data.markdown} claims={claims} onOpen={openClaim} title={title} />}
          {tab === "evidence" && <EvidenceTab claims={ledger.data} error={ledger.error} reload={ledger.reload} onOpen={openClaim} cited={cited} />}
          {tab === "files" && <FilesTab runId={id} entries={entries} error={pack.error} reload={pack.reload} />}
        </div>

        {side && (
          <div className="space-y-4">
            {open != null && <EvidencePanel id={open} claim={claims[String(open)]} onClose={() => setOpen(null)} />}
            {asking && (
              <section className="sticky top-32 rounded-xl border border-border bg-card p-4 shadow-card animate-scale-in">
                <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold">
                  <MessagesSquare className="size-4 text-brand" /> Ask Claude about this report
                </h3>
                <AskPanel runId={id} claims={data.claims} onOpenClaim={setOpen} />
              </section>
            )}
          </div>
        )}
      </div>

      {/* phones and tablets: evidence and chat open as dialogs */}
      {!wide && (
        <>
          <Modal open={open != null} onClose={() => setOpen(null)} title="Evidence">
            <div className="max-h-[70vh] overflow-auto p-4">
              {open != null && <EvidencePanel id={open} claim={claims[String(open)]} onClose={() => setOpen(null)} embedded />}
            </div>
          </Modal>
          <Modal open={asking && open == null} onClose={() => setAsking(false)} title="Ask Claude about this report" wide>
            <div className="p-4">
              <AskPanel runId={id} claims={data.claims} onOpenClaim={setOpen} />
            </div>
          </Modal>
        </>
      )}
    </div>
  );
}
