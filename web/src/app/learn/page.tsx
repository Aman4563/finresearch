import { BookA, CircleHelp, GraduationCap, ShieldAlert } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Callout, PageHeader } from "@/components/ui";

import { GLOSSARY } from "./_lib/glossary";
import { DISCLAIMER } from "./_lib/disclaimer";
import { MODULES } from "./_lib/modules";

export const metadata: Metadata = { title: "Learn F&O" };

export default function Page() {
  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <PageHeader icon={<GraduationCap className="size-5" />} eyebrow="Learn" title="Learn F&O"
        description="A structured, sourced course on Indian futures and options for a retail investor who wants to trade responsibly, or decide not to. Read in order; each module ends with a self-check." />
      <Callout tone="warn" icon={<ShieldAlert className="size-4" />}>{DISCLAIMER}</Callout>
      <ol className="grid gap-4 md:grid-cols-2">
        {MODULES.map((m) => (
          <li key={m.slug}>
            <Link href={`/learn/${m.slug}`} className="block h-full rounded-xl border border-border bg-card p-4 shadow-card transition hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow sm:p-5">
              <p className="text-xs font-medium uppercase tracking-wider text-brand">Module {m.n}</p>
              <h2 className="mt-1 text-base font-semibold tracking-tight">{m.title}</h2>
              <p className="mt-1 text-sm text-muted">{m.summary}</p>
              <p className="mt-3 flex items-center gap-1.5 text-xs text-muted"><CircleHelp className="size-3.5" />{m.sections.length} sections · {m.quiz.length}-question self-check</p>
            </Link>
          </li>
        ))}
        <li>
          <Link href="/learn/glossary" className="block h-full rounded-xl border border-border bg-card p-4 shadow-card transition hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow sm:p-5">
            <p className="text-xs font-medium uppercase tracking-wider text-brand">Reference</p>
            <h2 className="mt-1 flex items-center gap-1.5 text-base font-semibold tracking-tight"><BookA className="size-4" />Glossary</h2>
            <p className="mt-1 text-sm text-muted">{GLOSSARY.length} terms, from assignment to vega, each linked to its lesson.</p>
          </Link>
        </li>
      </ol>
      <p className="text-xs text-muted">
        How the sources work: every factual claim carries a numbered citation. Each source is marked verified (the regulator&apos;s or exchange&apos;s own document,
        read on the date shown), secondary (a broker or education page), reference (a book or peer-reviewed paper) or unverified (not re-read; shown in amber with a “?”).
        Indian rules change often: check the circular date before relying on a number.
      </p>
    </div>
  );
}
