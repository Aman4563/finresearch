"use client";

import { Badge } from "@/components/ui";
import { API_URL, type Citation, type Claim, useApi, when } from "@/lib/api";

const CHIP: Record<string, string> = {
  verified: "border-emerald-500 text-emerald-700 dark:text-emerald-300",
  unverified: "border-amber-500 text-amber-700 dark:text-amber-300",
  needs_review: "border-amber-500 text-amber-700 dark:text-amber-300",
  contradicted: "border-rose-500 text-rose-700 dark:text-rose-300",
  unsupported: "border-rose-500 text-rose-700 dark:text-rose-300",
  missing: "border-rose-500 bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
};

function host(url: string) {
  try {
    return new URL(url).hostname;
  } catch {
    return url.slice(0, 60); // citations are model output: never let a malformed URL break the page
  }
}

const isCalc = (url: string) => url.startsWith("fincalc:");

function citeLabel(c: Citation) {
  if (c.url) return isCalc(c.url) ? `computed: ${c.url.slice(8, 80)}` : host(c.url);
  return `${c.document_title ?? "document"} p${c.page ?? "?"} L${c.line_start}${c.line_end && c.line_end !== c.line_start ? `–${c.line_end}` : ""}`;
}

export function CiteChip({ id, claim, onOpen }: { id: number; claim?: Claim; onOpen: (id: number) => void }) {
  const status = claim?.status ?? "missing";
  return (
    <span className="group relative inline-block">
      <button
        type="button"
        onClick={() => onOpen(id)}
        className={`mx-0.5 rounded border px-1 align-baseline text-[0.7rem] font-medium leading-4 ${CHIP[status] ?? CHIP.unverified}`}
        aria-label={`claim ${id}: ${status}`}
      >
        C{id}
      </button>
      <span
        role="tooltip"
        className="pointer-events-none invisible absolute bottom-full left-1/2 z-20 mb-1 w-80 -translate-x-1/2 rounded border border-border bg-card p-2 text-left text-xs font-normal leading-snug opacity-0 shadow-lg transition-opacity group-hover:visible group-hover:opacity-100"
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
          <span className="text-rose-600">C{id} is not in this run&apos;s claim ledger.</span>
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
  if (error) return <p className="text-xs text-rose-600">{error}</p>;
  if (!data) return null;
  return (
    <pre className="mt-1 max-h-60 overflow-auto rounded bg-background p-2 text-[0.7rem] leading-4">
      {data.lines.map((l, i) => {
        const n = data.start + i;
        const hit = n >= (c.line_start ?? 0) && n <= (c.line_end ?? c.line_start ?? 0);
        return (
          <div key={n} className={hit ? "bg-amber-200/60 dark:bg-amber-700/40" : ""}>
            <span className="mr-2 select-none text-muted">{n}</span>
            {l.replaceAll("\f", "")}
          </div>
        );
      })}
    </pre>
  );
}

export function EvidencePanel({ id, claim: given, onClose }: { id: number; claim?: Claim; onClose: () => void }) {
  // claims cited in a chat answer may not be in the report's map: fetch them from the ledger
  const fetched = useApi<Claim>(given ? null : `/api/claims/${id}`);
  const claim = given ?? (fetched.data && fetched.data.id === id ? fetched.data : undefined);
  if (!given && !fetched.data && !fetched.error) return <p className="text-sm text-muted">Loading C{id}…</p>;
  return (
    <aside className="sticky top-4 max-h-[calc(100vh-2rem)] overflow-auto rounded-lg border border-border bg-card p-4 text-sm shadow">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-semibold">Evidence for C{id}</h3>
        <button type="button" onClick={onClose} className="text-muted hover:text-foreground" aria-label="close">
          ✕
        </button>
      </div>
      {!claim ? (
        <p className="text-rose-600">This citation does not exist in the run&apos;s claim ledger.</p>
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
              {claim.metric}: <span className="text-foreground">{claim.value}</span> {claim.unit} {claim.period && `(${claim.period})`}
            </p>
          )}
          {claim.verifier_note && <p className="rounded bg-background p-2 text-xs">Verifier: {claim.verifier_note}</p>}
          {claim.corrects_claim_id && <p className="text-xs text-muted">Corrects C{claim.corrects_claim_id}</p>}
          {claim.citations.map((c, i) => (
            <div key={i} className="border-t border-border pt-2">
              {c.url && isCalc(c.url) ? (
                <p className="text-xs">
                  Deterministic calculation <code className="break-all">{c.url.slice(8)}</code>
                </p>
              ) : c.url ? (
                <p className="text-xs">
                  <a className="underline" href={/^https?:\/\//.test(c.url) ? c.url : undefined} target="_blank" rel="noreferrer noopener">
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
                    <a className="underline" href={`${API_URL}/api/documents/${c.document_id}/file#page=${c.page ?? 1}`} target="_blank" rel="noreferrer">
                      open PDF
                    </a>
                  )}
                </p>
              )}
              {c.quote && <blockquote className="mt-1 border-l-2 border-border pl-2 text-xs italic">“{c.quote}”</blockquote>}
              {c.document_id && c.line_start && <SourceLines c={c} />}
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
