"use client";

/* Non-PDF pack files in the viewer: Markdown rendered like the report, CSV as a table, images inline, HTML in a
   sandboxed frame, and anything else (xlsx) as a download. */

import { Download, FileSpreadsheet } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { EmptyState, ErrorNote, SkeletonRows, Table } from "@/components/ui";

const MAX_ROWS = 2000;

function useText(url: string, attempt: number) {
  const [state, setState] = useState<{ key: string; text?: string; error?: string }>({ key: "" });
  const key = `${url}#${attempt}`;
  useEffect(() => {
    let dead = false;
    fetch(url, { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error(r.status === 404 ? "The file was not found (404)." : `The API answered ${r.status}.`);
        return r.text();
      })
      .then((text) => !dead && setState({ key, text }))
      .catch((e: Error) => !dead && setState({ key, error: /fetch/i.test(e.message) ? "Could not reach the FinResearch API." : e.message }));
    return () => {
      dead = true;
    };
  }, [url, key]);
  return state.key === key ? state : { key };
}

/** RFC 4180 CSV: quoted fields, doubled quotes, commas and newlines inside quotes. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"';
        i++;
      } else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function TextFile({ url, render }: { url: string; render: (text: string) => React.ReactNode }) {
  const [attempt, setAttempt] = useState(0);
  const s = useText(url, attempt);
  if (s.error)
    return (
      <div className="p-4 sm:p-6">
        <ErrorNote error={s.error} onRetry={() => setAttempt((a) => a + 1)} />
      </div>
    );
  if (s.text == null)
    return (
      <div className="p-4 sm:p-6">
        <SkeletonRows rows={10} />
      </div>
    );
  return <>{render(s.text)}</>;
}

function CsvTable({ text }: { text: string }) {
  const rows = useMemo(() => parseCsv(text).filter((r) => r.some((c) => c !== "")), [text]);
  const [head, ...body] = rows;
  if (!head) return <EmptyState title="This CSV is empty" />;
  const numeric = head.map((_, j) => body.slice(0, 50).every((r) => !r[j] || /^-?[\d,.]+%?$/.test(r[j].trim())));
  return (
    <div className="px-4 py-3 sm:px-5">
      <p className="num mb-2 text-xs text-muted">
        {body.length.toLocaleString("en-IN")} rows · {head.length} columns
        {body.length > MAX_ROWS && ` · showing the first ${MAX_ROWS.toLocaleString("en-IN")} (download for all)`}
      </p>
      <Table label="CSV file contents">
        <thead>
          <tr>{head.map((h, j) => <th key={j} className={numeric[j] ? "!text-right" : undefined}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {body.slice(0, MAX_ROWS).map((r, i) => (
            <tr key={i}>
              {head.map((_, j) => (
                <td key={j} className={numeric[j] ? "num text-right align-top" : "max-w-[32rem] whitespace-pre-wrap break-words align-top"}>{r[j] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

export function FileView({ url, ext, name, downloadUrl }: { url: string; ext: string; name: string; downloadUrl: string }) {
  if (ext === "md" || ext === "markdown" || ext === "txt")
    return (
      <div className="flex-1 overflow-auto">
        <TextFile
          url={url}
          render={(t) =>
            ext === "txt" ? (
              <pre className="whitespace-pre-wrap break-words p-4 text-xs sm:p-6">{t}</pre>
            ) : (
              <article className="report mx-auto max-w-3xl px-4 py-4 text-sm sm:px-8 sm:py-6">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{t}</ReactMarkdown>
              </article>
            )
          }
        />
      </div>
    );
  if (ext === "csv")
    return (
      <div className="flex-1 overflow-auto">
        <TextFile url={url} render={(t) => <CsvTable text={t} />} />
      </div>
    );
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext))
    return (
      <div className="grid flex-1 place-items-center overflow-auto bg-background-subtle p-4">
        {/* eslint-disable-next-line @next/next/no-img-element -- an API file of unknown size, not a static asset */}
        <img src={url} alt={name} className="max-w-full rounded-md bg-white shadow-card" />
      </div>
    );
  if (ext === "html" || ext === "htm")
    return (
      <iframe
        src={url}
        title={name}
        // no scripts, no same-origin access: the page is shown, never run
        sandbox="allow-popups allow-popups-to-escape-sandbox"
        className="min-h-0 w-full flex-1 bg-white"
      />
    );
  return (
    <div className="grid flex-1 place-items-center p-6">
      <EmptyState
        icon={<FileSpreadsheet className="size-5" />}
        title={`${name} can't be previewed here`}
        action={
          <a href={downloadUrl} className="inline-flex h-10 items-center gap-1.5 rounded-lg bg-brand px-4 text-sm font-medium text-brand-fg shadow-sm transition hover:bg-brand-strong">
            <Download className="size-4" /> Download {ext.toUpperCase()}
          </a>
        }
      >
        {ext === "xlsx" ? "Spreadsheets open in Excel, Numbers or Google Sheets. The same data is in the pack's CSV files, which open here." : "Download it to open it with the right app."}
      </EmptyState>
    </div>
  );
}
