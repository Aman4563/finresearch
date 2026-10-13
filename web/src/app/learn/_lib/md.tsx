"use client";

import Link from "next/link";
import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cx } from "@/components/ui";

import { SOURCES } from "./sources";

const CITE = /\[\[([a-z0-9-]+)\]\]/g;

/** Source ids in the order they are first cited in `texts`; an unknown id fails e2e/learn.spec.ts. */
export function citeOrder(texts: string[]) {
  const seen: string[] = [];
  for (const t of texts) for (const m of t.matchAll(CITE)) if (!seen.includes(m[1])) seen.push(m[1]);
  return seen;
}

/** Lesson Markdown: GFM tables and lists, [[source-id]] as a numbered link to the page's source list. */
export function Md({ text, order, className }: { text: string; order: string[]; className?: string }) {
  const linked = useMemo(
    () => text.replace(CITE, (_, id: string) => `[${order.indexOf(id) + 1}](#src-${id})`),
    [text, order],
  );
  const comps = useMemo<Components>(() => ({
    a: ({ href = "", children }) => {
      if (href.startsWith("#src-")) {
        const s = SOURCES[href.slice(5)];
        const flag = s?.status === "unverified" ? " (unverified)" : s?.status === "secondary" ? " (secondary source)" : "";
        return (
          <a href={href} title={`${s?.label ?? "unknown source"}${flag}`}
            className={cx("num ml-0.5 align-super text-[0.7em] font-medium no-underline hover:underline", s?.status === "unverified" ? "text-warn" : "text-brand")}>
            [{children}{s?.status === "unverified" ? "?" : ""}]
          </a>
        );
      }
      if (href.startsWith("/")) return <Link href={href} className="text-brand underline underline-offset-2">{children}</Link>;
      if (href.startsWith("#")) return <a href={href} className="text-brand underline underline-offset-2">{children}</a>;
      return <a href={href} target="_blank" rel="noreferrer noopener" className="text-brand underline underline-offset-2">{children}</a>;
    },
    table: ({ children }) => (
      <div className="my-3 max-w-full overflow-x-auto rounded-lg border border-border" tabIndex={0} role="region" aria-label="Table">
        <table className="w-full text-left text-xs [overflow-wrap:normal]">{children}</table>
      </div>
    ),
    th: ({ children }) => <th className="border-b border-border bg-background-subtle px-2.5 py-1.5 font-medium whitespace-nowrap">{children}</th>,
    td: ({ children }) => <td className="border-b border-border/60 px-2.5 py-1.5 align-top">{children}</td>,
    h3: ({ children }) => <h3 className="mt-4 mb-1 text-sm font-semibold text-foreground">{children}</h3>,
    blockquote: ({ children }) => <blockquote className="my-3 rounded-lg border-l-2 border-brand bg-brand-soft/40 px-3 py-1">{children}</blockquote>,
    code: ({ children }) => <code className="num rounded bg-background-subtle px-1 py-0.5 text-[0.85em] ring-1 ring-inset ring-border">{children}</code>,
  }), []);
  return (
    <div className={cx("text-sm leading-relaxed text-foreground/90 [&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_strong]:font-semibold [&_strong]:text-foreground [&_ul]:list-disc [&_ul]:pl-5", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={comps}>{linked}</ReactMarkdown>
    </div>
  );
}
