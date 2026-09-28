"use client";

import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { CiteChip } from "@/components/evidence";
import { Button, ErrorNote } from "@/components/ui";
import { api, type Claim, useApi } from "@/lib/api";

type Checks = {
  ok?: boolean;
  unknown_claims?: number[];
  unusable_claims?: number[];
  bad_line_citations?: string[];
  uncited_figure_lines?: string[];
  needs_new_research?: boolean;
};
type Message = { id: number; role: "user" | "assistant"; content: string; checks: Checks; model: string | null; duration_s: number | null };
type Thread = { id: number; title: string; messages: Message[] };
type ThreadSummary = { id: number; title: string; updated_at: string; messages: number };

const CITE = /\[C(\d+)\](?!\()/g;
const LINE = /\[D(\d+):L(\d+)(?:-(\d+))?\]/g;

function CheckNotes({ c }: { c: Checks }) {
  const notes: string[] = [];
  if (c.unknown_claims?.length) notes.push(`cites claims not in this run: ${c.unknown_claims.map((i) => `C${i}`).join(", ")}`);
  if (c.unusable_claims?.length) notes.push(`cites contradicted/unsupported claims: ${c.unusable_claims.map((i) => `C${i}`).join(", ")}`);
  if (c.bad_line_citations?.length) notes.push(`document lines that do not exist: ${c.bad_line_citations.join(", ")}`);
  if (c.uncited_figure_lines?.length) notes.push(`${c.uncited_figure_lines.length} sentence(s) with figures but no citation — treat as UNVERIFIED`);
  if (c.needs_new_research) notes.push("needs new research: the ledger and documents do not answer this");
  if (!notes.length) return <p className="mt-1 text-[0.7rem] text-emerald-600">✓ every figure is cited to the ledger or a document</p>;
  return (
    <ul className="mt-1 list-disc pl-4 text-[0.7rem] text-amber-700 dark:text-amber-300">
      {notes.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

export function AskPanel({ runId, claims, onOpenClaim }: { runId: string; claims: Record<string, Claim>; onOpenClaim: (id: number) => void }) {
  const list = useApi<ThreadSummary[]>(`/api/runs/${runId}/conversations`);
  // answers may cite any claim in the run's ledger, not only the ones the report cites
  const ledger = useApi<Claim[]>(`/api/runs/${runId}/claims`);
  const allClaims = useMemo(
    () => ({ ...Object.fromEntries((ledger.data ?? []).map((c) => [String(c.id), c])), ...claims }),
    [ledger.data, claims],
  );
  const [thread, setThread] = useState<Thread | null>(null);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = async (id: number) => setThread(await api<Thread>(`/api/conversations/${id}`));

  const send = async () => {
    const q = question.trim();
    if (!q) return;
    setBusy(true);
    setError(null);
    const pending: Message = { id: -Date.now(), role: "user", content: q, checks: {}, model: null, duration_s: null };
    setThread((t) => (t ? { ...t, messages: [...t.messages, pending] } : { id: 0, title: q, messages: [pending] }));
    try {
      const r = await api<{ conversation_id: number }>(`/api/runs/${runId}/ask`, {
        method: "POST",
        body: JSON.stringify({ question: q, conversation_id: thread?.id || null }),
      });
      setQuestion("");
      await open(r.conversation_id);
      list.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const render = (text: string) =>
    text.replace(CITE, "[C$1](#cite-$1)").replace(LINE, (_m, d, a, b) => `[D${d}:L${a}${b ? `-${b}` : ""}](#doc-${d}-${a}-${b ?? a})`);

  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="rounded border border-border bg-background px-2 py-1 text-xs"
          value={thread?.id ?? ""}
          onChange={(e) => (e.target.value ? open(Number(e.target.value)) : setThread(null))}
        >
          <option value="">New conversation</option>
          {list.data?.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title.slice(0, 60)} ({c.messages})
            </option>
          ))}
        </select>
      </div>
      <div className="max-h-[60vh] space-y-3 overflow-auto">
        {thread?.messages.map((m) => (
          <div key={m.id} className={m.role === "user" ? "rounded bg-background p-2" : "rounded border border-border p-2"}>
            {m.role === "user" ? (
              <p className="font-medium">{m.content}</p>
            ) : (
              <>
                <div className="report text-sm">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      a: ({ href, children }) => {
                        const c = href?.match(/^#cite-(\d+)$/);
                        if (c) return <CiteChip id={Number(c[1])} claim={allClaims[c[1]]} onOpen={onOpenClaim} />;
                        if (href?.startsWith("#doc-")) return <code className="rounded bg-background px-1 text-[0.7rem]">{children}</code>;
                        return (
                          <a href={href} className="underline" target="_blank" rel="noreferrer noopener">
                            {children}
                          </a>
                        );
                      },
                    }}
                  >
                    {render(m.content)}
                  </ReactMarkdown>
                </div>
                <CheckNotes c={m.checks} />
                <p className="mt-1 text-[0.65rem] text-muted">
                  {m.model} {m.duration_s ? `· ${Math.round(m.duration_s)}s` : ""}
                </p>
              </>
            )}
          </div>
        ))}
        {busy && <p className="text-xs text-muted">Claude is reading the ledger and documents…</p>}
      </div>
      <ErrorNote error={error ?? list.error} />
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          className="flex-1 rounded border border-border bg-background px-2 py-1"
          placeholder="Ask about this report…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          disabled={busy}
        />
        <Button type="submit" disabled={busy || !question.trim()}>
          Ask
        </Button>
      </form>
    </div>
  );
}
