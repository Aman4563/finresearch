"use client";

import {
  BellRing, BookA, BookOpen, ChevronDown, CircleHelp, Command, FileStack, FileText, FlaskConical, Keyboard, LayoutGrid,
  ListChecks, MessageCircleQuestion, Rocket, Scale, Search, ShieldCheck, Sparkles, Wrench, X,
} from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useMemo, useState } from "react";

import {
  FAQ, GETTING_STARTED, GLOSSARY, type GlossaryCategory, matches, PAGE_GUIDES, RESEARCH_STREAMS, slug, TROUBLESHOOTING,
} from "@/components/help/content";
import { openWelcome } from "@/components/help/welcome";
import { NAV } from "@/components/shell";
import { Badge, Button, Callout, cx, EmptyState, PageHeader } from "@/components/ui";

const SECTIONS = [
  { id: "getting-started", label: "Getting started", icon: <Rocket /> },
  { id: "how-it-works", label: "How research works", icon: <FlaskConical /> },
  { id: "pages", label: "Page guide", icon: <LayoutGrid /> },
  { id: "glossary", label: "Glossary", icon: <BookA /> },
  { id: "faq", label: "FAQ", icon: <MessageCircleQuestion /> },
  { id: "shortcuts", label: "Shortcuts", icon: <Keyboard /> },
  { id: "troubleshooting", label: "Troubleshooting", icon: <Wrench /> },
  { id: "disclaimer", label: "Disclaimer", icon: <Scale /> },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

const NAV_ITEMS = NAV.flatMap((g) => g.items);

const SHORTCUTS: { keys: string[]; what: string; group: string }[] = [
  { keys: ["⌘", "K"], what: "Open search and commands (Ctrl+K on Windows/Linux)", group: "Anywhere" },
  { keys: ["/"], what: "Open search", group: "Anywhere" },
  { keys: ["?"], what: "Open these shortcuts", group: "Anywhere" },
  { keys: ["Esc"], what: "Close a dialog or the search", group: "Anywhere" },
  { keys: ["Shift", "B"], what: "Blur rupee amounts for screen sharing (press again to show)", group: "Anywhere" },
  { keys: ["↑", "↓", "↵"], what: "Move through search results and open one", group: "In search" },
  ...NAV_ITEMS.filter((n) => n.keys).map((n) => ({ keys: n.keys!.split(" "), what: `Go to ${n.label}`, group: "Jump (press g, then the letter)" })),
];

/** Render `code` spans inside plain text. */
function rich(text: string) {
  return text.split(/(`[^`]+`)/g).map((part, i) =>
    part.startsWith("`") ? (
      <code key={i} className="num rounded bg-background-subtle px-1 py-0.5 text-[0.85em] text-foreground ring-1 ring-inset ring-border">{part.slice(1, -1)}</code>
    ) : (
      part
    ),
  );
}

function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="num inline-grid min-w-6 place-items-center rounded-md border border-border-strong bg-background-subtle px-1.5 py-0.5 text-[11px] font-medium text-foreground shadow-[0_1px_0_var(--border-strong)]">
      {children}
    </kbd>
  );
}

function Accordion({ title, children, open, onToggle, id }: { title: ReactNode; children: ReactNode; open: boolean; onToggle: () => void; id?: string }) {
  return (
    <div id={id} className={cx("scroll-mt-24 rounded-xl border bg-card transition", open ? "border-border-strong shadow-card" : "border-border hover:border-border-strong")}>
      <button type="button" aria-expanded={open} onClick={onToggle} className="flex w-full items-center gap-3 px-4 py-3 text-left">
        <span className="flex-1 text-sm font-medium">{title}</span>
        <ChevronDown className={cx("size-4 shrink-0 text-muted transition-transform duration-200", open && "rotate-180 text-brand")} />
      </button>
      <div className={cx("grid transition-[grid-template-rows] duration-300 ease-out", open ? "grid-rows-[1fr]" : "grid-rows-[0fr]")}>
        <div className="overflow-hidden">
          <div className="px-4 pb-4 text-sm leading-relaxed text-muted">{children}</div>
        </div>
      </div>
    </div>
  );
}

function Section({ id, title, icon, description, children, count }: {
  id: SectionId; title: string; icon: ReactNode; description?: ReactNode; children: ReactNode; count?: number;
}) {
  return (
    <section id={id} className="scroll-mt-24 animate-fade-up" aria-labelledby={`${id}-title`}>
      <div className="mb-4 flex items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-brand-soft text-brand [&_svg]:size-[18px]">{icon}</span>
        <div className="min-w-0 flex-1">
          <h2 id={`${id}-title`} className="group flex items-center gap-2 text-lg font-semibold tracking-tight">
            {title}
            {count != null && <Badge tone="neutral">{count}</Badge>}
            <a href={`#${id}`} aria-label={`Link to ${title}`} className="text-muted/0 transition group-hover:text-muted hover:!text-brand">#</a>
          </h2>
          {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
        </div>
      </div>
      {children}
    </section>
  );
}

export function HelpPage() {
  const [q, setQ] = useState("");
  const [cat, setCat] = useState<GlossaryCategory | "All">("All");
  const [openFaq, setOpenFaq] = useState<string | null>(FAQ[0].q);
  const [openTrouble, setOpenTrouble] = useState<string | null>(null);
  const [active, setActive] = useState<SectionId>("getting-started");
  const [flash, setFlash] = useState<string | null>(null);

  // deep links: on load and on same-page hash changes (e.g. pressing ? while already here)
  useEffect(() => {
    const go = () => {
      const id = decodeURIComponent(window.location.hash.slice(1));
      if (!id) return;
      setQ("");
      const faq = FAQ.find((f) => `faq-${slug(f.q)}` === id);
      if (faq) setOpenFaq(faq.q);
      if (id.startsWith("term-")) setCat("All");
      setFlash(id);
      setTimeout(() => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
      setTimeout(() => setFlash(null), 1800);
    };
    const initial = new URLSearchParams(window.location.search).get("q");
    // eslint-disable-next-line react-hooks/set-state-in-effect -- shareable searches (/help?q=gmp), read after hydration
    if (initial && !window.location.hash) setQ(initial);
    go();
    window.addEventListener("hashchange", go);
    return () => window.removeEventListener("hashchange", go);
  }, []);

  // scroll spy for the table of contents
  useEffect(() => {
    const els = SECTIONS.map((s) => document.getElementById(s.id)).filter(Boolean) as HTMLElement[];
    const io = new IntersectionObserver(
      (entries) => {
        const vis = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (vis) setActive(vis.target.id as SectionId);
      },
      { rootMargin: "-80px 0px -60% 0px" },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [q]);

  const steps = useMemo(() => GETTING_STARTED.filter((s) => matches(q, s.title, s.text)), [q]);
  const pages = useMemo(() => NAV_ITEMS.filter((n) => matches(q, n.label, n.description, ...(PAGE_GUIDES[n.href] ?? []))), [q]);
  const terms = useMemo(() => GLOSSARY.filter((t) => (cat === "All" || t.category === cat) && matches(q, t.term, t.aka, t.def, t.category)), [q, cat]);
  const faqs = useMemo(() => FAQ.filter((f) => matches(q, f.q, f.a)), [q]);
  const keys = useMemo(() => SHORTCUTS.filter((s) => matches(q, s.what, s.group, s.keys.join(" "))), [q]);
  const troubles = useMemo(() => TROUBLESHOOTING.filter((t) => matches(q, t.problem, t.fix)), [q]);
  const how = matches(q, "how research works documents streams claim ledger citations verification gate report monitor suggestions rules", ...RESEARCH_STREAMS.map((s) => s.label));
  const disclaimer = matches(q, "disclaimer investment advice sebi registered risk grey market interim");
  const searching = q.trim().length > 0;
  const total = steps.length + pages.length + terms.length + faqs.length + keys.length + troubles.length + (how ? 1 : 0) + (disclaimer ? 1 : 0);

  const categories: (GlossaryCategory | "All")[] = ["All", "IPO", "Stocks", "Mutual funds", "Bonds", "F&O", "FinResearch"];
  const show = (id: SectionId) =>
    !searching || { "getting-started": steps.length, "how-it-works": how ? 1 : 0, pages: pages.length, glossary: terms.length,
      faq: faqs.length, shortcuts: keys.length, troubleshooting: troubles.length, disclaimer: disclaimer ? 1 : 0 }[id] > 0;

  return (
    <div>
      <PageHeader
        icon={<CircleHelp className="size-5" />}
        title="Help centre"
        description="How FinResearch works, what every page does, the finance terms it uses, and what to do when something goes wrong."
        actions={<Button variant="secondary" icon={<Sparkles className="size-3.5" />} onClick={openWelcome}>Replay the welcome tour</Button>}
      />

      {/* search hero */}
      <div className="relative mb-6 overflow-hidden rounded-2xl border border-border bg-card p-4 shadow-card animate-fade-up sm:p-6">
        <div className="pointer-events-none absolute -top-16 -right-16 size-56 rounded-full bg-brand/10 blur-3xl" />
        <div className="pointer-events-none absolute -bottom-20 left-10 size-56 rounded-full bg-accent/10 blur-3xl" />
        <p className="relative mb-3 text-sm font-medium">What do you need help with?</p>
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setQ("")}
            placeholder="Search terms, questions and guides, e.g. “QIB”, “gate”, “plan”…"
            aria-label="Search help"
            className="h-12 w-full rounded-xl border border-border bg-background pr-10 pl-10 text-sm shadow-sm transition placeholder:text-muted/70 focus:border-brand focus:outline-none focus:ring-4 focus:ring-brand/15"
          />
          {q && (
            <button type="button" aria-label="Clear search" onClick={() => setQ("")}
              className="absolute top-1/2 right-3 grid size-6 -translate-y-1/2 place-items-center rounded-md text-muted hover:bg-background-subtle hover:text-foreground">
              <X className="size-4" />
            </button>
          )}
        </div>
        <div className="relative mt-3 flex flex-wrap items-center gap-1.5 text-xs">
          {searching ? (
            <span className="text-muted"><span className="num font-medium text-foreground">{total}</span> result{total === 1 ? "" : "s"} for “{q.trim()}”</span>
          ) : (
            <>
              <span className="text-muted">Popular:</span>
              {["QIB", "GMP", "gate", "unverified", "plan", "YTM", "greeks"].map((t) => (
                <button key={t} type="button" onClick={() => setQ(t)}
                  className="rounded-full px-2.5 py-0.5 text-muted ring-1 ring-inset ring-border transition hover:bg-brand-soft hover:text-brand hover:ring-brand/30">
                  {t}
                </button>
              ))}
            </>
          )}
        </div>
      </div>

      {/* mobile section chips */}
      <nav aria-label="Help sections" className="-mx-4 mb-6 flex gap-2 overflow-x-auto px-4 pb-1 lg:hidden">
        {SECTIONS.filter((s) => show(s.id)).map((s) => (
          <a key={s.id} href={`#${s.id}`}
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted [&_svg]:size-3.5">
            {s.icon}{s.label}
          </a>
        ))}
      </nav>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-8 lg:grid-cols-[200px_minmax(0,1fr)]">
        <aside className="hidden lg:block">
          <nav aria-label="On this page" className="sticky top-24 space-y-0.5">
            <p className="mb-2 px-2.5 text-[10px] font-semibold uppercase tracking-widest text-muted/80">On this page</p>
            {SECTIONS.map((s) => (
              <a key={s.id} href={`#${s.id}`}
                className={cx("relative flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm transition [&_svg]:size-4",
                  !show(s.id) && "pointer-events-none opacity-40",
                  active === s.id ? "bg-brand-soft font-medium text-foreground" : "text-muted hover:bg-background-subtle hover:text-foreground")}>
                {active === s.id && <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-brand" />}
                <span className={active === s.id ? "text-brand" : ""}>{s.icon}</span>
                {s.label}
              </a>
            ))}
          </nav>
        </aside>

        <div className="min-w-0 space-y-12">
          {searching && total === 0 && (
            <EmptyState icon={<Search className="size-5" />} title={`Nothing matches “${q.trim()}”`}
              action={<Button variant="secondary" onClick={() => setQ("")}>Clear search</Button>}>
              Try a shorter word (e.g. “QIB” instead of “QIB subscription ratio”), or browse the sections below after clearing the search.
            </EmptyState>
          )}

          {show("getting-started") && (
            <Section id="getting-started" title="Getting started" icon={<Rocket />} description="Five steps from a fresh install to a tracked decision.">
              <ol className="stagger grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                {steps.map((s) => {
                  const n = GETTING_STARTED.indexOf(s) + 1;
                  return (
                    <li key={s.title} className="group relative flex flex-col rounded-xl border border-border bg-card p-4 shadow-card transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow">
                      <span className="num mb-3 grid size-8 place-items-center rounded-full bg-gradient-to-br from-brand to-accent text-sm font-semibold text-white">{n}</span>
                      <p className="text-sm font-semibold">{s.title}</p>
                      <p className="mt-1 flex-1 text-xs leading-relaxed text-muted">{s.text}</p>
                      <Link href={s.link.href} className="mt-3 text-xs font-medium text-brand after:absolute after:inset-0 group-hover:underline">
                        {s.link.label} →
                      </Link>
                    </li>
                  );
                })}
              </ol>
            </Section>
          )}

          {show("how-it-works") && (
            <Section id="how-it-works" title="How research works" icon={<FlaskConical />}
              description="The AI reads and writes; Python computes and verifies. Nothing reaches a report without a source.">
              <ResearchFlow />
            </Section>
          )}

          {show("pages") && (
            <Section id="pages" title="Page guide" icon={<LayoutGrid />} description="What each page is for. The key combo on each card jumps there from anywhere." count={searching ? pages.length : undefined}>
              <div className="stagger grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {pages.map((n) => (
                  <Link key={n.href} href={n.href}
                    className="group rounded-xl border border-border bg-card p-4 shadow-card transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow">
                    <div className="flex items-center gap-2.5">
                      <span className="grid size-8 place-items-center rounded-lg bg-brand-soft text-brand transition group-hover:scale-110 [&_svg]:size-4">{n.icon}</span>
                      <span className="flex-1 text-sm font-semibold">{n.label}</span>
                      {n.keys && <span className="flex gap-1">{n.keys.split(" ").map((k, i) => <Kbd key={i}>{k}</Kbd>)}</span>}
                    </div>
                    <p className="mt-2 text-xs text-muted">{n.description}</p>
                    <ul className="mt-2 space-y-1">
                      {(PAGE_GUIDES[n.href] ?? []).map((g) => (
                        <li key={g} className="flex items-start gap-2 text-xs text-foreground/85">
                          <span className="mt-1.5 size-1 shrink-0 rounded-full bg-brand" />{g}
                        </li>
                      ))}
                    </ul>
                  </Link>
                ))}
              </div>
            </Section>
          )}

          {show("glossary") && (
            <Section id="glossary" title="Glossary" icon={<BookA />} count={terms.length}
              description="Indian market terms in plain English. Pages explain them inline with the (?) icon too.">
              <div className="-mx-4 mb-4 flex gap-1.5 overflow-x-auto px-4 pb-1 sm:mx-0 sm:flex-wrap sm:px-0">
                {categories.map((c) => (
                  <button key={c} type="button" onClick={() => setCat(c)} aria-pressed={cat === c}
                    className={cx("shrink-0 rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset transition",
                      cat === c ? "bg-brand text-brand-fg ring-brand" : "text-muted ring-border hover:text-foreground")}>
                    {c}
                  </button>
                ))}
              </div>
              {terms.length ? (
                <dl className="grid gap-3 md:grid-cols-2">
                  {terms.map((t) => {
                    const id = `term-${slug(t.term)}`;
                    return (
                      <div key={t.term} id={id}
                        className={cx("scroll-mt-24 rounded-xl border bg-card p-4 transition duration-500",
                          flash === id ? "border-brand shadow-glow" : "border-border")}>
                        <dt className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                          <span className="font-semibold">{t.term}</span>
                          {t.aka && <span className="text-xs text-muted">{t.aka}</span>}
                          <span className="ml-auto"><Badge tone={t.category === "FinResearch" ? "accent" : "neutral"}>{t.category}</Badge></span>
                        </dt>
                        <dd className="mt-1.5 text-sm leading-relaxed text-muted">
                          {t.def}
                          {t.link && <> <Link href={t.link.href} className="font-medium text-brand hover:underline">{t.link.label} →</Link></>}
                        </dd>
                      </div>
                    );
                  })}
                </dl>
              ) : (
                <EmptyState icon={<BookA className="size-5" />} title="No terms match">
                  {cat !== "All" ? <>Nothing in “{cat}”. Try “All” categories.</> : "Try a different word."}
                </EmptyState>
              )}
            </Section>
          )}

          {show("faq") && (
            <Section id="faq" title="Frequently asked questions" icon={<MessageCircleQuestion />} count={searching ? faqs.length : undefined}>
              <div className="space-y-2">
                {faqs.map((f) => {
                  const id = `faq-${slug(f.q)}`;
                  const open = searching || openFaq === f.q;
                  return (
                    <Accordion key={f.q} id={id} title={f.q} open={open} onToggle={() => setOpenFaq(openFaq === f.q ? null : f.q)}>
                      <p>{rich(f.a)}</p>
                      {f.link && <Link href={f.link.href} className="mt-2 inline-block text-xs font-medium text-brand hover:underline">{f.link.label} →</Link>}
                    </Accordion>
                  );
                })}
              </div>
            </Section>
          )}

          {show("shortcuts") && (
            <Section id="shortcuts" title="Keyboard shortcuts" icon={<Keyboard />}
              description={<>Shortcuts work anywhere except while you type in a box. Press <Kbd>?</Kbd> to come back here.</>}>
              <div className={cx("grid gap-4 md:grid-cols-2 rounded-xl transition duration-500", flash === "shortcuts" && "shadow-glow")}>
                {[...new Set(keys.map((k) => k.group))].map((g) => (
                  <div key={g} className="rounded-xl border border-border bg-card p-4 shadow-card">
                    <p className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                      {g.startsWith("Jump") ? <Command className="size-3.5" /> : <Keyboard className="size-3.5" />}{g}
                    </p>
                    <ul className="divide-y divide-border/70">
                      {keys.filter((k) => k.group === g).map((k) => (
                        <li key={k.what} className="flex items-center justify-between gap-3 py-2 text-sm">
                          <span>{k.what}</span>
                          <span className="flex shrink-0 items-center gap-1">
                            {k.keys.map((key, i) => (
                              <span key={i} className="flex items-center gap-1">
                                {i > 0 && g.startsWith("Jump") && <span className="text-[10px] text-muted">then</span>}
                                <Kbd>{key}</Kbd>
                              </span>
                            ))}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {show("troubleshooting") && (
            <Section id="troubleshooting" title="Troubleshooting" icon={<Wrench />} count={searching ? troubles.length : undefined}>
              <div className="space-y-2">
                {troubles.map((t) => (
                  <Accordion key={t.problem} title={t.problem} open={searching || openTrouble === t.problem}
                    onToggle={() => setOpenTrouble(openTrouble === t.problem ? null : t.problem)}>
                    <p>{rich(t.fix)}</p>
                    {t.link && <Link href={t.link.href} className="mt-2 inline-block text-xs font-medium text-brand hover:underline">{t.link.label} →</Link>}
                  </Accordion>
                ))}
              </div>
            </Section>
          )}

          {show("disclaimer") && (
            <Section id="disclaimer" title="Disclaimer" icon={<Scale />}>
              <div className={cx("rounded-xl transition duration-500", flash === "disclaimer" && "shadow-glow")}>
                <Callout tone="warn" icon={<ShieldCheck className="size-4" />} title="Personal research tool, not investment advice">
                  <div className="space-y-2">
                    <p>
                      FinResearch is a personal research tool. Its output is not investment advice and not research published by a
                      SEBI-registered investment adviser or research analyst.
                    </p>
                    <p>
                      AI can be wrong. Every figure is cited so you can check it; unverified claims are marked. Grey-market data is
                      unofficial, and subscription figures are interim while an issue is open. Past returns do not predict future returns.
                    </p>
                    <p>Investments in securities markets are subject to market risks. Read all offer documents carefully and consider your own circumstances before you invest.</p>
                  </div>
                </Callout>
              </div>
            </Section>
          )}

          <div className="flex flex-col items-center gap-2 rounded-2xl border border-dashed border-border px-6 py-8 text-center">
            <BookOpen className="size-5 text-brand" />
            <p className="text-sm font-medium">Still stuck?</p>
            <p className="max-w-md text-xs text-muted">
              Press <Kbd>⌘</Kbd> <Kbd>K</Kbd> to search pages and companies, or open a report and use “Ask about this report” for questions about a specific company.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ "how research works" diagram

function ResearchFlow() {
  const stages: { icon: ReactNode; title: string; text: string; tone: string; extra?: ReactNode }[] = [
    { icon: <FileStack />, title: "Documents & data", text: "RHP, DRHP, annual reports and filings (OCR where scanned) plus live NSE/BSE, SEBI and AMFI data.", tone: "from-info to-brand" },
    { icon: <FlaskConical />, title: "7 research streams", text: "Specialist AI researchers work in parallel, each citing where every finding comes from.", tone: "from-brand to-accent",
      extra: (
        <div className="mt-2 flex flex-wrap gap-1">
          {RESEARCH_STREAMS.map((s) => (
            <span key={s.key} className="rounded-md bg-background-subtle px-1.5 py-0.5 text-[10px] font-medium text-foreground/80 ring-1 ring-inset ring-border">{s.label}</span>
          ))}
        </div>
      ) },
    { icon: <BookOpen />, title: "Claim ledger", text: "Every finding is a claim with citations: document page and lines, or a URL with a time.", tone: "from-accent to-info" },
    { icon: <ShieldCheck />, title: "Verification gate", text: "Plain Python checks quotes, recomputes numbers, catches conflicts and stale live figures. Verifiers double-check what matters.", tone: "from-gain to-brand" },
    { icon: <FileText />, title: "Report", text: "Bull and bear cases and a verdict, published only if every citation is sound. Click any figure for its evidence.", tone: "from-brand to-gain" },
  ];
  return (
    <div className="space-y-4">
      <ol className="relative grid gap-3 lg:grid-cols-5">
        {/* connector line */}
        <span aria-hidden className="absolute top-5 bottom-5 left-[19px] w-px bg-gradient-to-b from-brand/50 via-accent/40 to-gain/50 lg:top-[19px] lg:right-[10%] lg:bottom-auto lg:left-[10%] lg:h-px lg:w-auto lg:bg-gradient-to-r" />
        {stages.map((s, i) => (
          <li key={s.title} className="relative flex gap-3 animate-fade-up lg:flex-col lg:gap-0" style={{ animationDelay: `${i * 90}ms` }}>
            <span className={cx("relative z-10 grid size-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br text-white shadow-glow ring-4 ring-background lg:mx-auto [&_svg]:size-[18px]", s.tone)}>
              {s.icon}
            </span>
            <div className="min-w-0 flex-1 rounded-xl border border-border bg-card p-3.5 shadow-card lg:mt-3">
              <p className="text-sm font-semibold"><span className="num mr-1 text-brand">{i + 1}</span>{s.title}</p>
              <p className="mt-1 text-xs leading-relaxed text-muted">{s.text}</p>
              {s.extra}
            </div>
          </li>
        ))}
      </ol>
      <div className="grid gap-3 md:grid-cols-2">
        <div className="flex gap-3 rounded-xl border border-border bg-card p-4 shadow-card">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-info-soft text-info"><BellRing className="size-4" /></span>
          <div>
            <p className="text-sm font-semibold">Then: Monitor</p>
            <p className="mt-0.5 text-xs leading-relaxed text-muted">
              Watch an IPO and the app checks subscription to the close, allotment, listing and anchor lock-ins on a schedule, and raises alerts.
            </p>
            <Link href="/monitor" className="mt-1.5 inline-block text-xs font-medium text-brand hover:underline">Open Monitor →</Link>
          </div>
        </div>
        <div className="flex gap-3 rounded-xl border border-border bg-card p-4 shadow-card">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-warn-soft text-warn"><ListChecks className="size-4" /></span>
          <div>
            <p className="text-sm font-semibold">Then: suggestions obey your rules</p>
            <p className="mt-0.5 text-xs leading-relaxed text-muted">
              A personal suggestion reads your profile; then Python applies your rules and lot limits on live data. A fired skip rule always means SKIP.
            </p>
            <Link href="/rules" className="mt-1.5 inline-block text-xs font-medium text-brand hover:underline">Your rules →</Link>
          </div>
        </div>
      </div>
      <p className="text-xs text-muted">
        Stocks, funds and bonds use the same pipeline with their own streams (e.g. fund performance, risk, portfolio and costs).
      </p>
    </div>
  );
}
