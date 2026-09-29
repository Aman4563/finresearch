"use client";

import { FileSearch, X } from "lucide-react";

import { Badge, SkeletonRows } from "@/components/ui";
import { API_URL, type Citation, type Claim, useApi, when } from "@/lib/api";

const CHIP: Record<string, string> = {
  verified: "border-gain/40 bg-gain-soft text-gain hover:border-gain",
  unverified: "border-warn/40 bg-warn-soft text-warn hover:border-warn",
  needs_review: "border-warn/40 bg-warn-soft text-warn hover:border-warn",
  contradicted: "border-loss/40 bg-loss-soft text-loss hover:border-loss",
  unsupported: "border-loss/40 bg-loss-soft text-loss hover:border-loss",
  missing: "border-loss bg-loss-soft text-loss",
};

function host(url: string) {
  try {
    return new URL(url).hostname;
  } catch {
    return url.slice(0, 60); // citations are model output: never let a malformed URL break the page
  }
}

// agents cite computed figures as "fincalc: ..." / "fincalc ipo.x(...)"; anything that is not a web URL is a calculation
const isCalc = (url: string) => !/^https?:\/\//.test(url);

function citeLabel(c: Citation) {
  if (c.url) return isCalc(c.url) ? `computed: ${c.url.replace(/^fincalc:?\s*/, "").slice(0, 72)}` : host(c.url);
  return `${c.document_title ?? "document"} p${c.page ?? "?"} L${c.line_start}${c.line_end && c.line_end !== c.line_start ? `–${c.line_end}` : ""}`;
}

export function CiteChip({ id, claim, onOpen }: { id: number; claim?: Claim; onOpen: (id: number) => void }) {
  const status = claim?.status ?? "missing";
  return (
    <span className="group relative inline-block">
      <button
        type="button"
        onClick={() => onOpen(id)}
        className={`num mx-0.5 rounded border px-1 align-baseline text-[0.68rem] font-medium leading-4 no-underline transition ${CHIP[status] ?? CHIP.unverified}`}
        aria-label={`claim ${id}: ${status}`}
      >
        C{id}
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 hidden w-72 max-w-[80vw] -translate-x-1/2 rounded-lg border border-border bg-card p-2.5 text-left text-xs font-normal leading-snug text-foreground shadow-pop group-hover:block group-hover:animate-scale-in"
      >
        {claim ? (
          <>
            <span className="mb-1 flex items-center gap-1">
              <Badge status={claim.status} /> <span className="text-muted">{claim.stream}</span>
            </span>
            <span className="block">{claim.statement}</span>
            {claim.citations[0] && (
              <span className="mt-1 block text-muted">
                {citeLabel(claim.citations[0])}
                {claim.citations[0].quote && <>: “{claim.citations[0].quote.slice(0, 160)}”</>}
              </span>
            )}
          </>
        ) : (
          <span className="text-loss">C{id} is not in this run&apos;s claim ledger.</span>
        )}
      </span>
    </span>
  );
}

function SourceLines({ c }: { c: Citation }) {
  const start = Math.max(1, (c.line_start ?? 1) - 3);
  const end = (c.line_end ?? c.line_start ?? 1) + 3;
  const { data, error } = useApi<{ lines: string[]; start: number }>(
    c.document_id && c.line_start ? `/api/documents/${c.document_id}/lines?start=${start}&end=${end}` : null,
  );
  if (error) return <p className="text-xs text-loss">{error}</p>;
  if (!data) return null;
  return (
    <pre className="num mt-2 max-h-60 overflow-auto rounded-lg border border-border bg-background-subtle p-2 text-[0.7rem] leading-4">
      {data.lines.map((l, i) => {
        const n = data.start + i;
        const hit = n >= (c.line_start ?? 0) && n <= (c.line_end ?? c.line_start ?? 0);
        return (
          <div key={n} className={hit ? "-mx-2 bg-warn-soft px-2 text-foreground" : ""}>
            <span className="mr-2 select-none text-muted">{n}</span>
            {l.replaceAll("\f", "")}
          </div>
        );
      })}
    </pre>
  );
}

export function EvidencePanel({ id, claim: given, onClose, embedded }: { id: number; claim?: Claim; onClose: () => void; embedded?: boolean }) {
  // claims cited in a chat answer may not be in the report's map: fetch them from the ledger
  const fetched = useApi<Claim>(given ? null : `/api/claims/${id}`);
  const claim = given ?? (fetched.data && fetched.data.id === id ? fetched.data : undefined);
  if (!given && !fetched.data && !fetched.error) return <div className="space-y-2 rounded-xl border border-border bg-card p-4"><SkeletonRows rows={4} /></div>;
  return (
    <aside className={embedded ? "text-sm" : "sticky top-20 max-h-[calc(100vh-6rem)] overflow-auto rounded-xl border border-border bg-card p-4 text-sm shadow-card animate-scale-in"}>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 font-semibold">
          <FileSearch className="size-4 text-brand" /> Evidence for <span className="num">C{id}</span>
        </h3>
        {!embedded && (
          <button type="button" onClick={onClose} className="text-muted hover:text-foreground" aria-label="close">
            <X className="size-4" />
          </button>
        )}
      </div>
      {!claim ? (
        <p className="text-loss">This citation does not exist in the run&apos;s claim ledger.</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge status={claim.status} />
            <span className="text-muted">
              {claim.stream} · {claim.importance} importance
            </span>
          </div>
          <p>{claim.statement}</p>
          {claim.value && (
            <p className="text-muted">
              {claim.metric}: <span className="num font-medium text-foreground">{claim.value}</span> {claim.unit} {claim.period && `(${claim.period})`}
            </p>
          )}
          {claim.verifier_note && <p className="rounded-lg bg-background-subtle p-2 text-xs"><span className="font-medium">Verifier:</span> {claim.verifier_note}</p>}
          {claim.checks?.source_language === "hi" && (
            <p className="text-xs text-warn">
              Hindi source: the quote is in Hindi and the statement is a translation
              {claim.checks.translation_marked ? "" : " (not marked as translated)"}.
            </p>
          )}
          {claim.corrects_claim_id && <p className="text-xs text-muted">Corrects C{claim.corrects_claim_id}</p>}
          {claim.citations.map((c, i) => (
            <div key={i} className="border-t border-border pt-2">
              {c.url && isCalc(c.url) ? (
                <p className="text-xs">
                  Deterministic calculation <code className="break-all">{c.url.replace(/^fincalc:?\s*/, "")}</code>
                </p>
              ) : c.url ? (
                <p className="text-xs">
                  <a className="break-all text-brand underline underline-offset-2" href={/^https?:\/\//.test(c.url) ? c.url : undefined} target="_blank" rel="noreferrer noopener">
                    {c.url}
                  </a>{" "}
                  <span className="text-muted">accessed {when(c.accessed_at)}</span>
                </p>
              ) : (
                <p className="text-xs">
                  {citeLabel(c)}{" "}
                  {c.quote_found != null && (
                    <Badge status={c.quote_found ? "verified" : "contradicted"}>
                      {c.quote_found ? "quote found" : "quote not found"}
                    </Badge>
                  )}{" "}
                  {c.document_id && (
                    <a className="text-brand underline underline-offset-2" href={`${API_URL}/api/documents/${c.document_id}/file#page=${c.page ?? 1}`} target="_blank" rel="noreferrer">
                      open PDF
                    </a>
                  )}
                </p>
              )}
              {c.quote && <blockquote className="mt-1.5 border-l-2 border-brand pl-2 text-xs italic text-foreground/85">“{c.quote}”</blockquote>}
              {c.document_id && c.line_start && <SourceLines c={c} />}
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
