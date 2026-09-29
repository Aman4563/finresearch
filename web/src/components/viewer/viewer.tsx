"use client";

import { ArrowLeft, Download, ExternalLink, FileText, FileType } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { EmptyState, cx } from "@/components/ui";
import { FileView } from "@/components/viewer/files";
import { PdfViewer } from "@/components/viewer/pdf";
import { type RunSummary, useApi } from "@/lib/api";
import { fileUrl } from "@/lib/viewer";

type DocMeta = {
  id: number; kind: string; title: string; pages: number | null; bytes: number; filename: string;
  source_url: string | null; company: { slug: string; name: string } | null;
};

export type ViewerTarget = { run: number; path: string } | { doc: number; page: number | null } | null;

const linkBtn =
  "inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-medium transition duration-150 active:scale-[0.97]";

export function Viewer({ target }: { target: ViewerTarget }) {
  const docMeta = useApi<DocMeta>(target && "doc" in target ? `/api/documents/${target.doc}` : null);
  const run = useApi<RunSummary>(target && "run" in target ? `/api/runs/${target.run}` : null);
  const router = useRouter();
  const [pages, setPages] = useState<number | null>(null);
  const onPages = useCallback((n: number) => setPages(n), []);

  const name = !target
    ? ""
    : "run" in target
      ? (target.path.split("/").pop() ?? target.path)
      : (docMeta.data?.title ?? `Document ${target.doc}`);

  useEffect(() => {
    if (name) document.title = `${name} · FinResearch`;
  }, [name]);

  if (!target)
    return (
      <EmptyState icon={<FileText className="size-5" />} title="Nothing to show" action={<Link className="text-sm text-brand underline" href="/runs">Go to research runs</Link>}>
        Open a file from a report (its PDF, HTML or source links) to view it here. The viewer takes <code>?run=&lt;id&gt;&amp;path=&lt;file&gt;</code> or{" "}
        <code>?doc=&lt;id&gt;&amp;page=&lt;n&gt;</code>.
      </EmptyState>
    );

  const inline = fileUrl("run" in target ? target : { doc: target.doc });
  const download = fileUrl("run" in target ? target : { doc: target.doc }, true);
  const ext = "run" in target ? (target.path.split(".").pop() ?? "").toLowerCase() : "pdf";
  const back =
    "run" in target
      ? { href: target.path.startsWith("06_Final_Report/") ? `/runs/${target.run}/report` : `/runs/${target.run}`, label: `Run #${target.run}${target.path.startsWith("06_Final_Report/") ? " report" : ""}` }
      : null;

  const meta = [
    "run" in target ? run.data?.company_name : docMeta.data?.company?.name,
    "run" in target ? target.path.split("/").slice(0, -1).join(" / ").replaceAll("_", " ") : docMeta.data?.kind?.replaceAll("_", " "),
    pages ? `${pages} pages` : null,
    "doc" in target && docMeta.data?.bytes ? `${(docMeta.data.bytes / (1 << 20)).toFixed(1)} MB` : null,
  ].filter(Boolean);

  return (
    <div className="-mx-4 -my-6 flex h-[calc(100dvh-4rem)] min-h-[30rem] flex-col sm:-mx-6 lg:-my-8">
      <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border bg-card px-4 py-3 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <span className="hidden size-9 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand sm:grid">
            {ext === "pdf" ? <FileType className="size-4.5" /> : <FileText className="size-4.5" />}
          </span>
          <div className="min-w-0">
            {back ? (
              <Link href={back.href} className="mb-0.5 inline-flex items-center gap-1 text-[11px] text-muted hover:text-foreground">
                <ArrowLeft className="size-3" /> {back.label}
              </Link>
            ) : (
              <button type="button" onClick={() => (window.history.length > 1 ? router.back() : router.push("/runs"))} className="mb-0.5 inline-flex items-center gap-1 text-[11px] text-muted hover:text-foreground">
                <ArrowLeft className="size-3" /> Back
              </button>
            )}
            <h1 className="truncate text-base font-semibold tracking-tight sm:text-lg" title={name}>{name}</h1>
            {meta.length > 0 && <p className="truncate text-xs text-muted">{meta.join(" · ")}</p>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <a href={inline} target="_blank" rel="noreferrer" className={cx(linkBtn, "bg-card text-foreground ring-1 ring-inset ring-border hover:bg-card-hover hover:ring-border-strong")} title="Open the file in a new browser tab">
            <ExternalLink className="size-3.5" /> <span>Open in new tab</span>
          </a>
          <a href={download} className={cx(linkBtn, "bg-brand text-brand-fg shadow-sm hover:bg-brand-strong hover:shadow-glow")} title="Save the file">
            <Download className="size-3.5" /> <span>Download</span>
          </a>
        </div>
      </header>
      <div className="flex min-h-0 flex-1 flex-col bg-card">
        {ext === "pdf" ? (
          <PdfViewer url={inline} name={name} initialPage={"doc" in target ? (target.page ?? 1) : 1} onPages={onPages} />
        ) : (
          <FileView url={inline} ext={ext} name={name} downloadUrl={download} />
        )}
      </div>
    </div>
  );
}
