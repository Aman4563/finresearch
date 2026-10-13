"use client";

import { ArrowLeft, ArrowRight, BookOpen, GraduationCap, Library, ShieldAlert } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { Badge, Callout, Card, PageHeader } from "@/components/ui";

import { citeOrder, Md } from "./md";
import { MODULES } from "./modules";
import { Quiz } from "./quiz";
import { StrategySketch } from "./sketch";
import { SOURCES, type SourceStatus } from "./sources";
import type { Module } from "./types";

const TONE: Record<SourceStatus, "gain" | "neutral" | "info" | "warn"> = { verified: "gain", secondary: "neutral", reference: "info", unverified: "warn" };

export const DISCLAIMER =
  "Education, not advice. FinResearch is not a SEBI-registered investment adviser or research analyst; it places no orders and makes no promise of profit. Most individual F&O traders lose money: read the SEBI study cited in module 2 before you trade.";

export function SourceList({ ids }: { ids: string[] }) {
  return (
    <ol className="space-y-2 text-xs">
      {ids.map((id, i) => {
        const s = SOURCES[id];
        return (
          <li key={id} id={`src-${id}`} className="scroll-mt-24 flex gap-2">
            <span className="num w-6 shrink-0 text-right text-muted">{i + 1}.</span>
            <span className="min-w-0">
              {s ? (
                <>
                  {s.url ? <a href={s.url} target="_blank" rel="noreferrer noopener" className="text-brand underline underline-offset-2">{s.label}</a> : s.label}{" "}
                  <Badge tone={TONE[s.status]}>{s.status}{s.checked && s.status !== "unverified" ? ` ${s.checked}` : ""}</Badge>
                  {s.note && <span className="block text-muted">{s.note}</span>}
                </>
              ) : <span className="text-loss">unknown source “{id}”</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function Lesson({ m }: { m: Module }) {
  const order = useMemo(() => citeOrder(m.sections.map((s) => s.body)), [m]);
  const i = MODULES.findIndex((x) => x.slug === m.slug);
  const prev = MODULES[i - 1], next = MODULES[i + 1];
  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <PageHeader icon={<GraduationCap className="size-5" />} eyebrow={<Link href="/learn" className="hover:underline">Learn F&amp;O · module {m.n} of {MODULES.length}</Link>}
        title={m.title} description={m.summary} />
      <Callout tone="warn" icon={<ShieldAlert className="size-4" />}>{DISCLAIMER}</Callout>
      <div className="grid gap-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
        <nav aria-label="On this page" className="lg:sticky lg:top-20 lg:self-start">
          <p className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">On this page</p>
          <ul className="flex flex-wrap gap-1.5 text-xs lg:flex-col lg:gap-1">
            {m.sections.map((s) => (
              <li key={s.id}><a href={`#${s.id}`} className="block rounded-md px-2 py-1 text-muted ring-1 ring-border hover:text-foreground lg:ring-0">{s.title}</a></li>
            ))}
            <li><a href="#quiz" className="block rounded-md px-2 py-1 text-muted ring-1 ring-border hover:text-foreground lg:ring-0">Self-check</a></li>
            <li><a href="#sources" className="block rounded-md px-2 py-1 text-muted ring-1 ring-border hover:text-foreground lg:ring-0">Sources</a></li>
          </ul>
        </nav>
        <div className="min-w-0 space-y-5">
          {m.sections.map((s) => (
            <section key={s.id} id={s.id} aria-labelledby={`${s.id}-h`} className="scroll-mt-24 rounded-xl border border-border bg-card p-4 shadow-card sm:p-5">
              <h2 id={`${s.id}-h`} className="group flex items-center gap-2 text-base font-semibold tracking-tight">
                {s.title}
                <a href={`#${s.id}`} aria-label={`Link to ${s.title}`} className="text-muted/0 transition group-hover:text-muted hover:!text-brand">#</a>
              </h2>
              <Md text={s.body} order={order} />
              {s.sketch && <StrategySketch sketch={s.sketch} card={s.card} />}
            </section>
          ))}
          <div id="quiz" className="scroll-mt-24"><Quiz questions={m.quiz} /></div>
          <div id="sources" className="scroll-mt-24">
            <Card title="Sources" icon={<Library className="size-4" />}
              subtitle="verified = primary document read on the date shown; secondary = broker or education page; reference = book or peer-reviewed paper; unverified = not re-read, treat with care (marked [n?] in the text).">
              <SourceList ids={order} />
            </Card>
          </div>
          <nav aria-label="Modules" className="flex flex-wrap justify-between gap-3 text-sm">
            {prev ? <Link href={`/learn/${prev.slug}`} className="inline-flex items-center gap-1.5 text-brand hover:underline"><ArrowLeft className="size-4" />{prev.n}. {prev.title}</Link> : <Link href="/learn" className="inline-flex items-center gap-1.5 text-brand hover:underline"><BookOpen className="size-4" />All modules</Link>}
            {next ? <Link href={`/learn/${next.slug}`} className="inline-flex items-center gap-1.5 text-brand hover:underline">{next.n}. {next.title}<ArrowRight className="size-4" /></Link> : <Link href="/learn/glossary" className="inline-flex items-center gap-1.5 text-brand hover:underline">Glossary<ArrowRight className="size-4" /></Link>}
          </nav>
        </div>
      </div>
    </div>
  );
}
