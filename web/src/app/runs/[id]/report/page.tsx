"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { CiteChip, EvidencePanel } from "@/components/evidence";
import { Card, ErrorNote } from "@/components/ui";
import { API_URL, type Report, useApi } from "@/lib/api";

const CITE = /\[C(\d+)\](?!\()/g;

export default function ReportReader() {
  const { id } = useParams<{ id: string }>();
  const { data, error } = useApi<Report>(`/api/runs/${id}/report`);
  const pack = useApi<{ files: string[] }>(`/api/runs/${id}/pack`);
  const [open, setOpen] = useState<number | null>(null);

  const markdown = useMemo(() => (data ? data.markdown.replace(CITE, "[C$1](#cite-$1)") : ""), [data]);
  const downloads = (pack.data?.files ?? []).filter((f) => f.startsWith("06_Final_Report/") && /\.(pdf|html|xlsx|md)$/.test(f));

  if (error) return <ErrorNote error={error} />;
  if (!data) return <p className="text-sm text-muted">Loading report…</p>;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link className="text-sm underline" href={`/runs/${id}`}>
          ← run #{id}
        </Link>
        <div className="flex flex-wrap gap-3 text-sm">
          {downloads.map((f) => (
            <a key={f} className="underline" href={`${API_URL}/api/runs/${id}/pack/${f}`} target="_blank" rel="noreferrer">
              {f.split("/").pop()}
            </a>
          ))}
        </div>
      </div>

      {!data.published && (
        <Card title="NOT PUBLISHED — the publish gate blocked this report">
          <ul className="list-disc space-y-1 pl-5 text-sm text-rose-700 dark:text-rose-300">
            {data.gate.blocking.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </Card>
      )}
      {data.gate.warnings.length > 0 && (
        <details className="rounded border border-border bg-card p-3 text-sm">
          <summary className="cursor-pointer text-amber-700 dark:text-amber-300">{data.gate.warnings.length} gate warnings</summary>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-muted">
            {data.gate.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </details>
      )}

      <div className={`grid gap-6 ${open ? "lg:grid-cols-[minmax(0,1fr)_26rem]" : ""}`}>
        <article className="report rounded-lg border border-border bg-card p-6">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ href, children }) => {
                const m = href?.match(/^#cite-(\d+)$/);
                if (m) {
                  const cid = Number(m[1]);
                  return <CiteChip id={cid} claim={data.claims[String(cid)]} onOpen={setOpen} />;
                }
                return (
                  <a href={href} className="underline" target="_blank" rel="noreferrer noopener">
                    {children}
                  </a>
                );
              },
            }}
          >
            {markdown}
          </ReactMarkdown>
        </article>
        {open != null && <EvidencePanel id={open} claim={data.claims[String(open)]} onClose={() => setOpen(null)} />}
      </div>
    </div>
  );
}
