"use client";

// Files tab: the run's research pack, folder by folder, with readable names, sizes and type icons. Each file opens in
// the in-app viewer and can be downloaded.

import { Download, Eye, FileArchive, FileImage, FileSpreadsheet, FileText, FileType, Folder, Globe } from "lucide-react";
import type { ReactNode } from "react";

import { Card, EmptyState, ErrorNote, SkeletonRows } from "@/components/ui";
import { fileUrl, viewerHref } from "@/lib/viewer";

export type PackEntry = { path: string; size: number | null };

const ICON: Record<string, ReactNode> = {
  pdf: <FileType className="size-4 text-loss" />, html: <Globe className="size-4 text-info" />, md: <FileText className="size-4 text-brand" />,
  csv: <FileSpreadsheet className="size-4 text-gain" />, xlsx: <FileSpreadsheet className="size-4 text-gain" />, png: <FileImage className="size-4 text-accent" />,
};

const DOC_KIND: Record<string, string> = {
  RHP: "Red Herring Prospectus", DRHP: "Draft Red Herring Prospectus", ADDENDUM: "Addendum", ABRIDGED_PROSPECTUS: "Abridged prospectus",
  PRICE_BAND_AD: "Price band ad", ANCHOR_ALLOCATION: "Anchor allocation", INDUSTRY_REPORT: "Industry report", ANNUAL_REPORT: "Annual report",
  FINANCIAL_STATEMENTS: "Financial statements", OTHER: "",
};
const KNOWN: Record<string, string> = {
  "report.pdf": "Final report (PDF)", "report.html": "Final report (web page)", "report.md": "Final report (Markdown)",
  "claims.csv": "Claim ledger (CSV)", "claims.xlsx": "Claim ledger (Excel)", "fact_check_log.md": "Fact-check log",
  "financials.csv": "Financials table (CSV)", "financials.xlsx": "Financials table (Excel)", "sources.md": "Web sources cited",
  "README.md": "About this pack", "report_NOT_PUBLISHED.md": "Blocked draft (not published)",
};

/** "RHP__Orient_Cables_RHP_Sep_2026.pdf" → "Red Herring Prospectus · Orient Cables RHP Sep 2026" */
export function readableName(file: string): string {
  if (KNOWN[file]) return KNOWN[file];
  const base = file.replace(/\.[a-z0-9]+$/i, "");
  const m = base.match(/^([A-Z_]+)__(.+)$/);
  const tidy = (s: string) => s.replace(/_section$/, " section").replace(/_/g, " ").replace(/\s+/g, " ").trim();
  if (m) {
    const kind = DOC_KIND[m[1]] ?? tidy(m[1]).toLowerCase();
    return kind ? `${kind} · ${tidy(m[2])}` : tidy(m[2]);
  }
  const t = tidy(base);
  return t.charAt(0).toUpperCase() + t.slice(1);
}

const folderName = (f: string) => (f === "" ? "Pack" : f.replace(/^\d+_/, "").replace(/_/g, " "));
export const fileSize = (n: number | null) =>
  n == null ? "" : n >= 1_048_576 ? `${(n / 1_048_576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`;
export function FilesTab({ runId, entries, error, reload }: { runId: string; entries: PackEntry[] | null; error: string | null; reload: () => void }) {
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!entries) return <Card><SkeletonRows rows={8} /></Card>;
  if (!entries.length)
    return (
      <EmptyState icon={<FileArchive className="size-5" />} title="No research pack yet">
        The pack is written when the run finishes (<code>finresearch ipo render</code>). Re-render it from the run page if it is missing.
      </EmptyState>
    );
  const groups = new Map<string, PackEntry[]>();
  for (const e of entries) {
    const i = e.path.indexOf("/");
    const folder = i < 0 ? "" : e.path.slice(0, i);
    groups.set(folder, [...(groups.get(folder) ?? []), e]);
  }
  const ordered = [...groups.entries()].sort((a, b) => (a[0] === "" ? 1 : b[0] === "" ? -1 : a[0].localeCompare(b[0])));
  const total = entries.reduce((a, e) => a + (e.size ?? 0), 0);
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        <span className="num text-foreground">{entries.length}</span> files · <span className="num">{fileSize(total)}</span>. Open reads a file in the app; the download button saves it.
      </p>
      <div className="grid gap-4 lg:grid-cols-2">
        {ordered.map(([folder, list]) => (
          <Card key={folder || "root"} title={folderName(folder)} icon={<Folder className="size-4" />} subtitle={`${list.length} file${list.length === 1 ? "" : "s"}${folder ? ` · ${folder}` : ""}`} className="animate-fade-up">
            <ul className="-mx-1 divide-y divide-border/60">
              {list.map((e) => {
                const rel = e.path.slice(folder ? folder.length + 1 : 0);
                const ext = rel.split(".").pop()?.toLowerCase() ?? "";
                return (
                  <li key={e.path} className="flex items-center gap-2.5 px-1 py-2">
                    <span className="shrink-0">{ICON[ext] ?? <FileText className="size-4 text-muted" />}</span>
                    <a href={viewerHref({ run: Number(runId), path: e.path })} className="min-w-0 flex-1 hover:text-brand" title={e.path}>
                      <span className="block truncate text-[13px] font-medium">{readableName(rel.split("/").pop() ?? rel)}</span>
                      <span className="block truncate text-[11px] text-muted">
                        <span className="uppercase">{ext}</span> · <span className="num">{fileSize(e.size)}</span> · {rel}
                      </span>
                    </a>
                    <a href={viewerHref({ run: Number(runId), path: e.path })} className="grid size-8 shrink-0 place-items-center rounded-lg text-muted ring-1 ring-inset ring-border transition hover:text-brand hover:ring-border-strong" aria-label={`Open ${rel}`} title="Open">
                      <Eye className="size-3.5" />
                    </a>
                    <a href={fileUrl({ run: Number(runId), path: e.path }, true)} className="grid size-8 shrink-0 place-items-center rounded-lg text-muted ring-1 ring-inset ring-border transition hover:text-brand hover:ring-border-strong" aria-label={`Download ${rel}`} title="Download">
                      <Download className="size-3.5" />
                    </a>
                  </li>
                );
              })}
            </ul>
          </Card>
        ))}
      </div>
    </div>
  );
}
