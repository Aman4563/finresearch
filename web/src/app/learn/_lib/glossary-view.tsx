"use client";

import { BookA, Search } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Callout, EmptyState, inputClass, PageHeader, cx } from "@/components/ui";

import { GLOSSARY, slug } from "./glossary";
import { DISCLAIMER } from "./disclaimer";

export function GlossaryView() {
  const [q, setQ] = useState("");
  const shown = useMemo(() => {
    const k = q.trim().toLowerCase();
    return k ? GLOSSARY.filter((e) => `${e.term} ${e.aka ?? ""} ${e.def}`.toLowerCase().includes(k)) : GLOSSARY;
  }, [q]);
  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <PageHeader icon={<BookA className="size-5" />} eyebrow={<Link href="/learn" className="hover:underline">Learn F&amp;O</Link>}
        title="F&O glossary" description="Short definitions; each links to the lesson section that explains it with sources." />
      <Callout tone="warn">{DISCLAIMER}</Callout>
      <div className="relative max-w-md">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" />
        <input aria-label="Search the glossary" placeholder="Search terms, e.g. theta, STT, skew" value={q} onChange={(e) => setQ(e.target.value)} className={cx(inputClass, "w-full pl-9")} />
      </div>
      {shown.length === 0 ? (
        <EmptyState icon={<Search className="size-5" />} title={`No term matches “${q}”`}>Try a shorter word, or browse the <Link href="/learn" className="text-brand underline">modules</Link>.</EmptyState>
      ) : (
        <dl className="grid gap-3 md:grid-cols-2">
          {shown.map((e) => (
            <div key={e.term} id={`term-${slug(e.term)}`} className="scroll-mt-24 rounded-xl border border-border bg-card p-4 shadow-card">
              <dt className="text-sm font-semibold">{e.term}{e.aka && <span className="font-normal text-muted"> · {e.aka}</span>}</dt>
              <dd className="mt-1 text-sm leading-relaxed text-foreground/90">
                {e.def} <Link href={e.see} className="text-brand underline underline-offset-2">Learn more</Link>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
