"use client";

// The full report as written: collapsible sections, a scroll-spy table of contents, glossary terms explained on
// their first use in each section, tables with a sticky header row and right-aligned numbers, and evidence chips.

import { BookOpen, ChevronDown, ChevronsDownUp, ChevronsUpDown, ListTree } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CiteChip } from "@/components/evidence";
import { cx } from "@/components/ui";
import { Toc, extractHeadings, headingId, plain, useReadingState } from "@/components/workspace/report-parts";
import { type ClaimMap, GLOSS_BY_SLUG, GLOSS_PATTERNS, GlossTerm, type OpenClaim, glossSlug } from "@/components/workspace/report/shared";

type Section = { id: string | null; title: string | null; start: number; body: string };

/** Split at `## ` headings (outside code fences), keeping each body's absolute line position. */
function sections(md: string): Section[] {
  const lines = md.split("\n");
  const out: Section[] = [{ id: null, title: null, start: 1, body: "" }];
  let fence = false;
  let buf: string[] = [];
  lines.forEach((line, i) => {
    if (/^\s*```/.test(line)) fence = !fence;
    const m = !fence && line.match(/^##\s+(.+)$/);
    if (m) {
      out[out.length - 1].body = buf.join("\n");
      out.push({ id: `sec-${i + 1}`, title: m[1], start: i + 2, body: "" });
      buf = [];
    } else buf.push(line);
  });
  out[out.length - 1].body = buf.join("\n");
  return out.filter((s) => s.title || s.body.trim());
}

// spans the glossary never rewrites: links (incl. citation chips), inline code, URLs
const PROTECTED = /\[[^\]]*\]\([^)]*\)|`[^`]*`|https?:\/\/\S+/g;

/** Link the first use of each glossary term in a section to its definition. */
export function linkGlossary(body: string): string {
  const used = new Set<string>();
  let fence = false;
  return body
    .split("\n")
    .map((line) => {
      if (/^\s*```/.test(line)) fence = !fence;
      if (fence || /^\s*#/.test(line) || /^\s*\|?\s*:?-{3,}/.test(line)) return line;
      for (const { re, term } of GLOSS_PATTERNS) {
        const slug = glossSlug(term);
        if (used.has(slug)) continue;
        let done = false;
        const parts: string[] = [];
        let last = 0;
        for (const m of line.matchAll(PROTECTED)) {
          parts.push(line.slice(last, m.index), m[0]);
          last = (m.index ?? 0) + m[0].length;
        }
        parts.push(line.slice(last));
        const next = parts.map((part, k) => {
          if (done || k % 2 === 1) return part;
          const hit = part.match(re);
          if (!hit || hit.index == null) return part;
          done = true;
          return `${part.slice(0, hit.index)}[${hit[0]}](#gloss-${slug})${part.slice(hit.index + hit[0].length)}`;
        });
        if (done) {
          used.add(slug);
          line = next.join("");
        }
      }
      return line;
    })
    .join("\n");
}

type HNode = { type?: string; value?: string; tagName?: string; children?: HNode[] };
const textOf = (n: HNode | undefined): string => (!n ? "" : n.type === "text" ? n.value ?? "" : (n.children ?? []).map(textOf).join(""));

/** A table cell that is a number (₹1,234.5 / 8.32x / -9.93% / 258–272), ignoring citation chips. */
export function isNumericCell(text: string): boolean {
  const t = text.replace(/\bC\d+\b/g, "").replace(/\s+/g, " ").trim();
  if (!t || !/\d/.test(t) || t.length > 40) return false;
  return /^[-−+~≈(]*\s*(₹|Rs\.?|US\$|\$)?\s*[\d,.]+\s*(%|x|×|m|mn|cr|crore|bn|days|years)?\)?(\s*(–|-|to)\s*(₹)?\s*[\d,.]+\s*(%|x|×|m|mn|cr|crore)?)?$/i.test(t);
}

function mdComponents(claims: ClaimMap, onOpen: OpenClaim): Components {
  return {
    a: ({ href, children }) => {
      const m = href?.match(/^#cite-(\d+)$/);
      if (m) return <CiteChip id={Number(m[1])} claim={claims[m[1]]} onOpen={onOpen} />;
      const g = href?.match(/^#gloss-(.+)$/);
      const term = g && GLOSS_BY_SLUG.get(g[1]);
      if (term) return <GlossTerm term={term}>{children}</GlossTerm>;
      return (
        <a href={href} target="_blank" rel="noreferrer noopener">
          {children}
        </a>
      );
    },
    h3: ({ node, children }) => (
      <h3 id={headingId(node)} className="scroll-mt-28">
        {children}
      </h3>
    ),
    table: ({ children }) => (
      <div className="report-table">
        <table>{children}</table>
      </div>
    ),
    td: ({ node, children }) => <td className={isNumericCell(textOf(node as HNode)) ? "is-num" : undefined}>{children}</td>,
    p: ({ node, children }) => {
      const first = (node as HNode | undefined)?.children?.[0];
      const explain = first?.tagName === "strong" && /^explained simply/i.test(textOf(first));
      return <p className={explain ? "explain" : undefined}>{children}</p>;
    },
  };
}

export function FullReport({ markdown, claims, onOpen, title }: { markdown: string; claims: ClaimMap; onOpen: OpenClaim; title: string }) {
  const [tocOpen, setTocOpen] = useState(false);
  const [article, setArticle] = useState<HTMLElement | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const linked = useMemo(() => markdown.replace(/\[C(\d+)\](?!\()/g, "[C$1](#cite-$1)"), [markdown]);
  const headings = useMemo(() => extractHeadings(linked), [linked]);
  const ids = useMemo(() => headings.map((h) => h.id), [headings]);
  const { active, progress } = useReadingState(article, ids);
  // keep line numbers: the title line becomes blank lines so heading ids match the table of contents
  const body = useMemo(() => linked.replace(/^#\s+.+\n/, (m) => "\n".repeat(m.split("\n").length - 1)), [linked]);
  const secs = useMemo(() => sections(body).map((s) => ({ ...s, body: linkGlossary(s.body) })), [body]);
  const comps = useMemo(() => mdComponents(claims, onOpen), [claims, onOpen]);
  const owner = useMemo(() => {
    // which h2 section each heading id lives in, to open a collapsed section when its sub-heading is picked
    const map = new Map<string, string>();
    let cur: string | null = null;
    for (const h of headings) {
      if (h.level === 2) cur = h.id;
      if (cur) map.set(h.id, cur);
    }
    return map;
  }, [headings]);

  const reveal = (id: string) => {
    const sec = owner.get(id);
    if (sec && collapsed.has(sec)) {
      setCollapsed((c) => {
        const n = new Set(c);
        n.delete(sec);
        return n;
      });
      requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  };
  // a #sec-N link (a shared URL) scrolls to its heading; every section starts open
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (id.startsWith("sec-")) requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({ block: "start" }));
  }, []);

  const toggle = (id: string) =>
    setCollapsed((c) => {
      const n = new Set(c);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const allIds = secs.map((s) => s.id).filter((x): x is string => !!x);
  const allClosed = allIds.length > 0 && allIds.every((i) => collapsed.has(i));

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[13rem_minmax(0,1fr)]">
      <div className="pointer-events-none fixed inset-x-0 top-16 z-30 h-0.5">
        <div className="h-full origin-left bg-gradient-to-r from-brand to-accent transition-transform duration-150" style={{ transform: `scaleX(${progress})` }} />
      </div>
      <nav aria-label="Contents" className="hidden xl:block">
        <div className="sticky top-32 max-h-[calc(100vh-9rem)] overflow-auto pb-4">
          <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">
            <ListTree className="size-3.5" /> Contents
          </p>
          <Toc headings={headings} active={active} onPick={reveal} />
          <p className="num mt-4 text-[11px] text-muted">{Math.round(progress * 100)}% read</p>
        </div>
      </nav>

      <div className="min-w-0 space-y-3">
        {headings.length > 0 && (
          <div className="sticky top-[7.25rem] z-20 xl:hidden">
            <button type="button" onClick={() => setTocOpen((o) => !o)} aria-expanded={tocOpen}
              className="flex w-full items-center gap-2 rounded-lg border border-border bg-card/90 px-3 py-2 text-left text-xs shadow-card backdrop-blur">
              <ListTree className="size-3.5 text-brand" />
              <span className="min-w-0 flex-1 truncate font-medium">{headings.find((h) => h.id === active)?.text ?? "Contents"}</span>
              <span className="num text-muted">{Math.round(progress * 100)}%</span>
            </button>
            {tocOpen && (
              <div className="absolute inset-x-0 top-full mt-1 max-h-[60vh] overflow-auto rounded-lg border border-border bg-card p-3 shadow-pop animate-scale-in">
                <Toc headings={headings} active={active} onPick={(id) => { setTocOpen(false); reveal(id); }} />
              </div>
            )}
          </div>
        )}
        <article ref={setArticle} className="report rounded-xl border border-border bg-card px-4 py-5 text-[15px] shadow-card sm:px-8 sm:py-7">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
            <p className="flex items-center gap-1.5">
              <BookOpen className="size-3.5" /> Full report · <span className="truncate">{title}</span>
            </p>
            <div className="flex items-center gap-3">
              <span className="hidden items-center gap-1 sm:flex">
                <span className="border-b border-dotted border-brand/60">dotted terms</span> explain themselves
              </span>
              {allIds.length > 1 && (
                <button type="button" onClick={() => setCollapsed(allClosed ? new Set() : new Set(allIds))}
                  className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-medium text-foreground/80 ring-1 ring-inset ring-border transition hover:bg-card-hover">
                  {allClosed ? <ChevronsUpDown className="size-3.5" /> : <ChevronsDownUp className="size-3.5" />}
                  {allClosed ? "Expand all" : "Collapse all"}
                </button>
              )}
            </div>
          </div>
          {secs.map((s) => (
            <ReportSection key={s.id ?? "intro"} s={s} open={!s.id || !collapsed.has(s.id)} onToggle={() => s.id && toggle(s.id)} comps={comps} />
          ))}
        </article>
      </div>
    </div>
  );
}

function ReportSection({ s, open, onToggle, comps }: { s: Section; open: boolean; onToggle: () => void; comps: Components }): ReactNode {
  const content = (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={comps}>
      {"\n".repeat(Math.max(0, s.start - 1)) + s.body}
    </ReactMarkdown>
  );
  if (!s.id) return <div>{content}</div>;
  return (
    <section>
      <h2 id={s.id} className="scroll-mt-28 !mb-0">
        <button type="button" onClick={onToggle} aria-expanded={open} className="group flex w-full items-center gap-2 text-left">
          <ChevronDown className={cx("size-4 shrink-0 text-muted transition-transform group-hover:text-brand", !open && "-rotate-90")} />
          <span className="min-w-0 flex-1">{plain(s.title ?? "")}</span>
        </button>
      </h2>
      {open ? <div className="animate-fade-in pt-1">{content}</div> : <p className="mt-1 text-xs text-muted">Collapsed. Click the heading to open.</p>}
    </section>
  );
}
