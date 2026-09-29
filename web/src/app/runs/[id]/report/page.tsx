"use client";

import {
  ArrowLeft, BookOpen, Download, FileSpreadsheet, FileText, FileType, Globe, ListTree, MessagesSquare, ShieldAlert, ShieldCheck, Sparkles, X,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { AskPanel } from "@/components/ask";
import { CiteChip, EvidencePanel } from "@/components/evidence";
import { SuggestionPanel } from "@/components/suggestion";
import { Badge, Button, Callout, Card, ErrorNote, InfoTip, Modal, Skeleton, SkeletonRows, cx } from "@/components/ui";
import { Toc, extractHeadings, headingId, parseVerdict, plain, useReadingState, verdictTone } from "@/components/workspace/report-parts";
import { KindIcon, kindMeta } from "@/components/workspace/run-meta";
import { API_URL, type Claim, type Report, useApi } from "@/lib/api";

const CITE = /\[C(\d+)\](?!\()/g;

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

function mdComponents(claims: Record<string, Claim>, onOpen: (id: number) => void, withIds: boolean): Components {
  return {
    a: ({ href, children }) => {
      const m = href?.match(/^#cite-(\d+)$/);
      if (m) {
        const cid = Number(m[1]);
        return <CiteChip id={cid} claim={claims[String(cid)]} onOpen={onOpen} />;
      }
      return (
        <a href={href} target="_blank" rel="noreferrer noopener">
          {children}
        </a>
      );
    },
    ...(withIds
      ? {
          h2: ({ node, children }) => (
            <h2 id={headingId(node)} className="scroll-mt-24">
              {children}
            </h2>
          ),
          h3: ({ node, children }) => (
            <h3 id={headingId(node)} className="scroll-mt-24">
              {children}
            </h3>
          ),
        }
      : {}),
  };
}

const TONE_RING: Record<string, string> = {
  gain: "from-gain/15 ring-gain/30 text-gain",
  loss: "from-loss/15 ring-loss/30 text-loss",
  warn: "from-warn/15 ring-warn/30 text-warn",
  neutral: "from-brand/10 ring-border text-foreground",
};

function VerdictHero({ data, onOpen, title }: { data: Report; onOpen: (id: number) => void; title: string }) {
  const v = useMemo(() => parseVerdict(data.markdown), [data.markdown]);
  const claimList = Object.values(data.claims);
  const verified = claimList.filter((c) => c.status === "verified").length;
  const words = data.markdown.split(/\s+/).length;
  const tone = verdictTone(v?.word ?? null);
  const comps = useMemo(() => mdComponents(data.claims, onOpen, false), [data.claims, onOpen]);
  return (
    <Card padded={false} className="relative overflow-hidden animate-fade-up">
      <div className={cx("pointer-events-none absolute inset-0 bg-gradient-to-br to-transparent to-60% opacity-80", TONE_RING[tone].split(" ")[0])} />
      <div className="relative grid gap-5 p-4 sm:p-6 lg:grid-cols-[minmax(0,16rem)_1fr]">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-xs text-muted">
            <KindIcon kind={data.kind} className="size-8" />
            <span>
              {kindMeta(data.kind).label} report · run <span className="num">#{data.run_id}</span>
            </span>
          </div>
          <div>
            <p className="flex items-center gap-1 text-[11px] font-medium uppercase tracking-wider text-muted">
              Verdict
              <InfoTip>The report&apos;s own bottom line, from its verdict box. Read the reasoning and check the cited evidence before you act.</InfoTip>
            </p>
            <p className={cx("mt-1 inline-flex rounded-xl px-3 py-1.5 text-2xl font-bold tracking-tight ring-1 ring-inset", TONE_RING[tone].split(" ").slice(1).join(" "))}>
              {v?.word ?? "See report"}
            </p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {v?.confidence && <Badge tone="info">{v.confidence} confidence</Badge>}
            {data.published ? (
              <Badge tone="gain">
                <ShieldCheck className="size-3" /> gate passed
              </Badge>
            ) : (
              <Badge tone="loss">
                <ShieldAlert className="size-3" /> not published
              </Badge>
            )}
          </div>
          <dl className="grid grid-cols-3 gap-2 text-center">
            {[
              [claimList.length, "claims cited"],
              [claimList.length ? `${Math.round((verified / claimList.length) * 100)}%` : "—", "verified"],
              [`${Math.max(1, Math.round(words / 230))}m`, "read"],
            ].map(([n, l]) => (
              <div key={String(l)} className="rounded-lg bg-background-subtle/70 px-1 py-2">
                <dd className="num text-base font-semibold">{n}</dd>
                <dt className="text-[10px] text-muted">{l}</dt>
              </div>
            ))}
          </dl>
        </div>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold leading-snug tracking-tight sm:text-xl">{title}</h1>
          {v && v.rows.length > 0 ? (
            <dl className="mt-3 grid gap-2.5 sm:grid-cols-2">
              {v.rows.slice(0, 6).map((r, i) => (
                <div key={r.label} className={cx("rounded-lg border border-border bg-card/70 p-3", i >= 3 && "hidden sm:block")}>
                  <dt className="text-[11px] font-medium uppercase tracking-wider text-muted">{r.label}</dt>
                  <dd className="report mt-1 line-clamp-4 text-[13px] leading-relaxed [&_p]:m-0">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={comps}>
                      {r.value.replace(CITE, "[C$1](#cite-$1)")}
                    </ReactMarkdown>
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="mt-2 text-sm text-muted">This report has no verdict box; the full reasoning is below.</p>
          )}
        </div>
      </div>
    </Card>
  );
}

export default function ReportReader() {
  const { id } = useParams<{ id: string }>();
  const { data, error, reload } = useApi<Report>(`/api/runs/${id}/report`);
  const pack = useApi<{ files: string[] }>(`/api/runs/${id}/pack`);
  const [open, setOpen] = useState<number | null>(null);
  const [asking, setAsking] = useState(false);
  const [tocOpen, setTocOpen] = useState(false);
  const [article, setArticle] = useState<HTMLElement | null>(null);
  const wide = useWide();

  const markdown = useMemo(() => (data ? data.markdown.replace(CITE, "[C$1](#cite-$1)") : ""), [data]);
  const headings = useMemo(() => extractHeadings(markdown), [markdown]);
  const ids = useMemo(() => headings.map((h) => h.id), [headings]);
  const { active, progress } = useReadingState(article, ids);
  const title = useMemo(() => plain(markdown.match(/^#\s+(.+)$/m)?.[1] ?? `Run #${id} report`), [markdown, id]);
  const body = useMemo(() => markdown.replace(/^#\s+.+\n/, (m) => "\n".repeat(m.split("\n").length - 1)), [markdown]); // keep line numbers
  const comps = useMemo(() => (data ? mdComponents(data.claims, setOpen, true) : {}), [data]);
  const downloads = (pack.data?.files ?? []).filter((f) => f.startsWith("06_Final_Report/") && /\.(pdf|html|xlsx|md)$/.test(f));
  const side = wide && (open != null || asking);
  const dupExt = (ext: string) => downloads.filter((f) => f.endsWith(`.${ext}`)).length > 1;

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
        <Skeleton className="h-56 rounded-xl" />
        <div className="grid gap-6 xl:grid-cols-[14rem_1fr]">
          <Skeleton className="hidden h-80 rounded-xl xl:block" />
          <div className="space-y-3 rounded-xl border border-border bg-card p-6">
            <SkeletonRows rows={12} />
          </div>
        </div>
      </div>
    );

  return (
    <div className="space-y-5">
      {/* reading progress, under the sticky app header */}
      <div className="pointer-events-none fixed inset-x-0 top-16 z-30 h-0.5">
        <div className="h-full origin-left bg-gradient-to-r from-brand to-accent transition-transform duration-150" style={{ transform: `scaleX(${progress})` }} />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link href={`/runs/${id}`} className="inline-flex items-center gap-1 text-xs text-muted hover:text-foreground">
          <ArrowLeft className="size-3.5" /> Run #{id} pipeline
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          {downloads.map((f) => {
            const ext = f.split(".").pop() ?? "";
            return (
              <a
                key={f}
                href={`${API_URL}/api/runs/${id}/pack/${f}`}
                target="_blank"
                rel="noreferrer"
                title={`Download ${f.split("/").pop()}`}
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-card px-2.5 text-xs font-medium ring-1 ring-inset ring-border transition hover:bg-card-hover hover:ring-border-strong"
              >
                {FILE_ICON[ext] ?? <Download className="size-3.5" />}
                <span className={dupExt(ext) ? "max-w-32 truncate" : "uppercase"}>{dupExt(ext) ? f.split("/").pop() : ext}</span>
              </a>
            );
          })}
          <Button variant={asking ? "secondary" : "primary"} icon={asking ? <X className="size-3.5" /> : <MessagesSquare className="size-3.5" />} onClick={() => setAsking((a) => !a)}>
            {asking ? "Hide chat" : "Ask about this report"}
          </Button>
        </div>
      </div>

      <VerdictHero data={data} onOpen={setOpen} title={title} />

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
            <span className="num rounded border border-gain/40 bg-gain-soft px-1 text-[0.68rem] text-gain">C123</span> chip to see its source and quote.
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

      {data.kind === "ipo_report" && (
        <Card title="My suggestion" icon={<Sparkles className="size-4" />} subtitle="How much to bid, if at all, for your profile and rules"
          help="A personal, rule-checked suggestion built from this report plus live subscription data. It is saved to your Journal so you can record what you did.">
          <SuggestionPanel runId={id} />
        </Card>
      )}

      <div className={cx("grid grid-cols-1 gap-6", side ? "lg:grid-cols-[minmax(0,1fr)_24rem] xl:grid-cols-[13rem_minmax(0,1fr)_24rem]" : "xl:grid-cols-[13rem_minmax(0,1fr)]")}>
        {/* table of contents: sticky column on wide screens */}
        <nav aria-label="Contents" className="hidden xl:block">
          <div className="sticky top-24 max-h-[calc(100vh-7rem)] overflow-auto pb-4">
            <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">
              <ListTree className="size-3.5" /> Contents
            </p>
            <Toc headings={headings} active={active} />
            <p className="num mt-4 text-[11px] text-muted">{Math.round(progress * 100)}% read</p>
          </div>
        </nav>

        <div className="min-w-0 space-y-3">
          {/* compact contents on smaller screens */}
          {headings.length > 0 && (
            <div className="sticky top-[4.25rem] z-20 xl:hidden">
              <button
                type="button"
                onClick={() => setTocOpen((o) => !o)}
                className="flex w-full items-center gap-2 rounded-lg border border-border bg-card/90 px-3 py-2 text-left text-xs shadow-card backdrop-blur"
                aria-expanded={tocOpen}
              >
                <ListTree className="size-3.5 text-brand" />
                <span className="min-w-0 flex-1 truncate font-medium">{headings.find((h) => h.id === active)?.text ?? "Contents"}</span>
                <span className="num text-muted">{Math.round(progress * 100)}%</span>
              </button>
              {tocOpen && (
                <div className="absolute inset-x-0 top-full mt-1 max-h-[60vh] overflow-auto rounded-lg border border-border bg-card p-3 shadow-pop animate-scale-in">
                  <Toc headings={headings} active={active} onPick={() => setTocOpen(false)} />
                </div>
              )}
            </div>
          )}
          <article ref={setArticle} className="report rounded-xl border border-border bg-card px-4 py-5 text-[15px] shadow-card sm:px-8 sm:py-7">
            <p className="mb-4 flex items-center gap-1.5 text-xs text-muted">
              <BookOpen className="size-3.5" /> Full report
            </p>
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={comps}>
              {body}
            </ReactMarkdown>
          </article>
        </div>

        {side && (
          <div className="space-y-4">
            {open != null && <EvidencePanel id={open} claim={data.claims[String(open)]} onClose={() => setOpen(null)} />}
            {asking && (
              <section className="sticky top-20 rounded-xl border border-border bg-card p-4 shadow-card animate-scale-in">
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
              {open != null && <EvidencePanel id={open} claim={data.claims[String(open)]} onClose={() => setOpen(null)} embedded />}
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
