"use client";

import {
  Activity, BadgeIndianRupee, BellRing, BookOpenCheck, ChartCandlestick, ChevronsLeft, CircleHelp, Command, FlaskConical,
  Gauge as GaugeIcon, Landmark, LayoutDashboard, ListChecks, Menu, Monitor, Moon, NotebookPen, PieChart, Radar, Rocket,
  Search, Sun, UserRound, X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AlertBadge } from "@/components/alerts";
import { WelcomeTour } from "@/components/help/welcome";
import { AVATAR_GRADIENT, PreferenceEffects } from "@/components/profile/common";
import { cx } from "@/components/ui";
import { API_URL, type Company, type Profile, useApi } from "@/lib/api";

// ------------------------------------------------------------------ navigation

export type NavItem = { href: string; label: string; icon: ReactNode; keys?: string; description: string };

export const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Overview",
    items: [{ href: "/", label: "Dashboard", icon: <LayoutDashboard />, keys: "g d", description: "Everything at a glance" }],
  },
  {
    group: "Research",
    items: [
      { href: "/ipos", label: "IPOs", icon: <Rocket />, keys: "g i", description: "Open and upcoming NSE and BSE SME issues" },
      { href: "/stocks", label: "Stocks", icon: <ChartCandlestick />, keys: "g s", description: "Listed companies: price, results, research" },
      { href: "/funds", label: "Mutual funds", icon: <PieChart />, keys: "g f", description: "AMFI NAVs, returns and fund research" },
      { href: "/bonds", label: "Bonds", icon: <Landmark />, keys: "g b", description: "Listed bonds and NCDs, yields" },
      { href: "/fno", label: "F&O", icon: <Activity />, keys: "g o", description: "Option chains, greeks, strategy payoffs" },
      { href: "/signals", label: "Signals", icon: <Radar />, keys: "g g", description: "Buy/sell signals and their track record" },
    ],
  },
  {
    group: "Workspace",
    items: [
      { href: "/runs", label: "Research runs", icon: <FlaskConical />, keys: "g r", description: "Agent runs, live progress and reports" },
      { href: "/monitor", label: "Monitor", icon: <BellRing />, keys: "g m", description: "Watches, scheduled checks and alerts" },
      { href: "/journal", label: "Journal", icon: <NotebookPen />, keys: "g j", description: "Your decisions and their outcomes" },
    ],
  },
  {
    group: "You",
    items: [
      { href: "/profile", label: "Profile", icon: <UserRound />, keys: "g p", description: "You, your investor profile and preferences" },
      { href: "/rules", label: "Rules", icon: <ListChecks />, keys: "g u", description: "Personal rules that gate suggestions" },
      { href: "/usage", label: "Plan usage", icon: <GaugeIcon />, keys: "g l", description: "Claude plan window and limits" },
      { href: "/help", label: "Help", icon: <CircleHelp />, keys: "g h", description: "Guides, glossary, FAQ and shortcuts" },
    ],
  },
];

const ALL_NAV = NAV.flatMap((g) => g.items);

export function navFor(path: string): NavItem | undefined {
  if (path === "/") return ALL_NAV[0];
  return ALL_NAV.filter((n) => n.href !== "/" && path.startsWith(n.href)).sort((a, b) => b.href.length - a.href.length)[0];
}

// ------------------------------------------------------------------ theme

export type Theme = "light" | "dark" | "system";
const THEME_KEY = "finresearch.theme";

/** Runs before paint (inlined in <head>) so the page never flashes the wrong theme. `?theme=light|dark` forces one
 * for that page view (screenshots), without saving it. */
export const THEME_SCRIPT = `try{var q=new URLSearchParams(location.search).get("theme");var t=(q==="light"||q==="dark")?q:(localStorage.getItem("${THEME_KEY}")||"system");document.documentElement.dataset.theme=t==="system"?(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"):t;document.documentElement.dataset.themePref=t}catch(e){}`;

export function useTheme() {
  const [pref, setPref] = useState<Theme>("system");
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read the saved preference after hydration
    setPref((document.documentElement.dataset.themePref as Theme) || "system");
  }, []);
  const apply = useCallback((t: Theme) => {
    const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.themePref = t;
    try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode: the choice lasts for this page */ }
    setPref(t);
  }, []);
  useEffect(() => {
    if (pref !== "system") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => apply("system");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [pref, apply]);
  return { theme: pref, setTheme: apply };
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next: Record<Theme, Theme> = { light: "dark", dark: "system", system: "light" };
  const icon = { light: <Sun className="size-4" />, dark: <Moon className="size-4" />, system: <Monitor className="size-4" /> }[theme];
  return (
    <button type="button" onClick={() => setTheme(next[theme])} title={`Theme: ${theme} (click to change)`} aria-label={`Theme: ${theme}`}
      className="grid size-9 place-items-center rounded-lg text-muted transition hover:bg-background-subtle hover:text-foreground">
      <span key={theme} className="animate-scale-in">{icon}</span>
    </button>
  );
}

// ------------------------------------------------------------------ small pieces

function Logo({ collapsed }: { collapsed?: boolean }) {
  return (
    <Link href="/" className="group flex items-center gap-2.5">
      <span className="relative grid size-8 place-items-center overflow-hidden rounded-lg bg-gradient-to-br from-brand to-accent text-white shadow-glow">
        <svg viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 17l5-5 4 3 7-8" className="[stroke-dasharray:30] [stroke-dashoffset:0] transition-all duration-700 group-hover:[stroke-dashoffset:30]" />
          <path d="M15 7h4v4" />
        </svg>
      </span>
      {!collapsed && (
        <span className="leading-tight">
          <span className="block text-[15px] font-semibold tracking-tight">FinResearch</span>
          <span className="block text-[10px] font-medium uppercase tracking-widest text-muted">fact-checked</span>
        </span>
      )}
    </Link>
  );
}

function ApiStatus() {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    const check = () =>
      fetch(`${API_URL}/api/health`).then((r) => alive && setOk(r.ok)).catch(() => alive && setOk(false));
    check();
    const t = setInterval(check, 30000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const label = ok == null ? "Checking the API…" : ok ? "API online" : "API offline: run `finresearch serve`";
  return (
    <span title={label} className="hidden items-center gap-1.5 rounded-full px-2 py-1 text-[11px] text-muted ring-1 ring-inset ring-border md:inline-flex">
      <span className={cx("size-1.5 rounded-full", ok == null ? "bg-muted" : ok ? "bg-gain animate-pulse-ring text-gain" : "bg-loss")} />
      {ok == null ? "…" : ok ? "Live" : "Offline"}
    </span>
  );
}

export function initials(name: string | null | undefined) {
  const parts = (name ?? "").trim().split(/\s+/).filter(Boolean);
  return (parts.length ? parts.slice(0, 2).map((p) => p[0]).join("") : "FR").toUpperCase();
}

type ProfileWithName = Profile & { display_name?: string | null };

function ProfileMenu() {
  const { data } = useApi<ProfileWithName>("/api/profile");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  const name = data?.display_name || "Investor";
  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => setOpen((o) => !o)} aria-haspopup="menu" aria-expanded={open}
        className="flex items-center gap-2 rounded-full p-0.5 pr-2 transition hover:bg-background-subtle">
        <span className={cx("grid size-8 place-items-center rounded-full bg-gradient-to-br text-xs font-semibold text-white", AVATAR_GRADIENT[data?.avatar_color ?? "brand"])}>
          {initials(name)}
        </span>
        <span className="hidden text-sm font-medium lg:inline">{name}</span>
      </button>
      {open && (
        <div role="menu" className="absolute right-0 z-50 mt-2 w-64 overflow-hidden rounded-xl border border-border bg-card shadow-pop animate-scale-in">
          <div className="border-b border-border px-4 py-3">
            <p className="text-sm font-semibold">{name}</p>
            {data && (
              <p className="mt-0.5 text-xs text-muted">
                {data.category.toUpperCase()} · {data.risk_appetite} risk · ₹{Number(data.capital_per_ipo_inr).toLocaleString("en-IN")} per IPO
              </p>
            )}
          </div>
          {[
            { href: "/profile", label: "Profile & preferences", icon: <UserRound className="size-4" /> },
            { href: "/rules", label: "My rules", icon: <ListChecks className="size-4" /> },
            { href: "/journal", label: "Decision journal", icon: <BookOpenCheck className="size-4" /> },
            { href: "/usage", label: "Plan usage", icon: <GaugeIcon className="size-4" /> },
            { href: "/help", label: "Help centre", icon: <CircleHelp className="size-4" /> },
          ].map((i) => (
            <Link key={i.href} href={i.href} role="menuitem" onClick={() => setOpen(false)}
              className="flex items-center gap-2.5 px-4 py-2 text-sm text-muted transition hover:bg-background-subtle hover:text-foreground">
              {i.icon}
              {i.label}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ command palette

type Cmd = { id: string; label: string; hint?: string; group: string; icon?: ReactNode; run: () => void };

function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const { setTheme } = useTheme();
  const { data: companies } = useApi<Company[]>(open ? "/api/companies" : null);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- a fresh search every time the palette opens
      setQ(""); setSel(0);
      setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  const cmds = useMemo<Cmd[]>(() => {
    const go = (href: string) => () => { router.push(href); onClose(); };
    const pages: Cmd[] = ALL_NAV.map((n) => ({ id: n.href, label: n.label, hint: n.description, group: "Go to", icon: n.icon, run: go(n.href) }));
    const actions: Cmd[] = [
      { id: "t-light", label: "Light theme", group: "Appearance", icon: <Sun />, run: () => { setTheme("light"); onClose(); } },
      { id: "t-dark", label: "Dark theme", group: "Appearance", icon: <Moon />, run: () => { setTheme("dark"); onClose(); } },
      { id: "t-system", label: "Match system theme", group: "Appearance", icon: <Monitor />, run: () => { setTheme("system"); onClose(); } },
      { id: "shortcuts", label: "Keyboard shortcuts", group: "Help", icon: <Command />, run: go("/help#shortcuts") },
      { id: "glossary", label: "Glossary: QIB, NII, GMP, YTM…", group: "Help", icon: <CircleHelp />, run: go("/help#glossary") },
    ];
    const cos: Cmd[] = (companies ?? []).map((c) => ({
      id: `co-${c.slug}`, label: c.name, hint: c.nse_symbol ?? c.slug, group: "Companies", icon: <BadgeIndianRupee />,
      run: go(c.latest_run ? `/runs/${c.latest_run}` : `/ipos`),
    }));
    return [...pages, ...actions, ...cos];
  }, [companies, onClose, router, setTheme]);

  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    const hits = t ? cmds.filter((c) => `${c.label} ${c.hint ?? ""} ${c.group}`.toLowerCase().includes(t)) : cmds.filter((c) => c.group !== "Companies");
    return hits.slice(0, 40);
  }, [cmds, q]);

  if (!open) return null;
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(shown.length - 1, s + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); shown[sel]?.run(); }
    else if (e.key === "Escape") onClose();
  };
  let lastGroup = "";
  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/40 p-4 pt-[14vh] backdrop-blur-sm animate-fade-in" onClick={onClose}>
      <div className="w-full max-w-xl overflow-hidden rounded-xl border border-border bg-card shadow-pop animate-scale-in" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border px-4">
          <Search className="size-4 text-muted" />
          <input ref={inputRef} value={q} onChange={(e) => { setQ(e.target.value); setSel(0); }} onKeyDown={onKey}
            placeholder="Search pages, companies and actions…" aria-label="Command search"
            className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted" />
          <kbd className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted">Esc</kbd>
        </div>
        <ul className="max-h-[50vh] overflow-y-auto p-2" role="listbox">
          {!shown.length && <li className="px-3 py-6 text-center text-sm text-muted">Nothing matches “{q}”.</li>}
          {shown.map((c, i) => {
            const header = c.group !== lastGroup ? c.group : null;
            lastGroup = c.group;
            return (
              <li key={c.id}>
                {header && <p className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">{header}</p>}
                <button type="button" role="option" aria-selected={i === sel} onMouseEnter={() => setSel(i)} onClick={c.run}
                  className={cx("flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition",
                    i === sel ? "bg-brand-soft text-foreground" : "text-muted")}>
                  <span className="text-brand [&_svg]:size-4">{c.icon}</span>
                  <span className="font-medium text-foreground">{c.label}</span>
                  {c.hint && <span className="ml-auto truncate pl-3 text-xs text-muted">{c.hint}</span>}
                </button>
              </li>
            );
          })}
        </ul>
        <div className="flex items-center gap-3 border-t border-border px-4 py-2 text-[11px] text-muted">
          <span><kbd className="font-mono">↑↓</kbd> move</span>
          <span><kbd className="font-mono">↵</kbd> open</span>
          <span className="ml-auto">Tip: press <kbd className="font-mono">g</kbd> then a letter to jump, e.g. <kbd className="font-mono">g d</kbd></span>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ layout

const COLLAPSE_KEY = "finresearch.sidebar";

export function AppShell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [collapsed, setCollapsed] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [palette, setPalette] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- restore the saved sidebar width after hydration
    try { setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1"); } catch { /* storage blocked */ }
  }, []);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- close the mobile drawer on navigation
  useEffect(() => setMobile(false), [path]);

  const toggle = () => {
    setCollapsed((c) => {
      try { localStorage.setItem(COLLAPSE_KEY, c ? "0" : "1"); } catch { /* storage blocked */ }
      return !c;
    });
  };

  // keyboard: Cmd/Ctrl+K palette, "/" palette, "?" shortcuts, "g <key>" jumps
  useEffect(() => {
    let pendingG = 0;
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      const typing = el?.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el?.tagName);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((p) => !p); return; }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "/") { e.preventDefault(); setPalette(true); return; }
      if (e.key === "?") { router.push("/help#shortcuts"); return; }
      // a second "g" inside the window is the "g g" jump, not a new prefix
      if (e.key === "g" && Date.now() - pendingG >= 1200) { pendingG = Date.now(); return; }
      if (Date.now() - pendingG < 1200) {
        const hit = ALL_NAV.find((n) => n.keys === `g ${e.key.toLowerCase()}`);
        pendingG = 0;
        if (hit) router.push(hit.href);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router]);

  const current = navFor(path);
  const closePalette = useCallback(() => setPalette(false), []);

  const sidebar = (isMobile: boolean) => (
    <nav aria-label="Main" className="flex h-full flex-col">
      <div className={cx("flex h-16 items-center border-b border-border px-4", collapsed && !isMobile ? "justify-center px-2" : "justify-between")}>
        <Logo collapsed={collapsed && !isMobile} />
        {isMobile && (
          <button type="button" aria-label="Close menu" onClick={() => setMobile(false)} className="text-muted"><X className="size-5" /></button>
        )}
      </div>
      <div className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
        {NAV.map((g) => (
          <div key={g.group}>
            {(!collapsed || isMobile) && <p className="mb-1.5 px-2 text-[10px] font-semibold uppercase tracking-widest text-muted/80">{g.group}</p>}
            <ul className="space-y-0.5">
              {g.items.map((n) => {
                const active = current?.href === n.href;
                return (
                  <li key={n.href}>
                    <Link href={n.href} title={collapsed && !isMobile ? n.label : undefined} aria-current={active ? "page" : undefined}
                      className={cx(
                        "group relative flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm transition duration-150 [&_svg]:size-[18px] [&_svg]:shrink-0",
                        collapsed && !isMobile && "justify-center",
                        active ? "bg-brand-soft font-medium text-foreground" : "text-muted hover:bg-background-subtle hover:text-foreground",
                      )}>
                      {active && <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-brand" />}
                      <span className={cx("transition", active ? "text-brand" : "group-hover:text-brand")}>{n.icon}</span>
                      {(!collapsed || isMobile) && <span className="truncate">{n.label}</span>}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
      {!isMobile && (
        <div className="border-t border-border p-3">
          <button type="button" onClick={toggle} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={cx("flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-xs text-muted transition hover:bg-background-subtle hover:text-foreground", collapsed && "justify-center")}>
            <ChevronsLeft className={cx("size-4 transition-transform duration-300", collapsed && "rotate-180")} />
            {!collapsed && "Collapse"}
          </button>
        </div>
      )}
    </nav>
  );

  return (
    <div className="app-backdrop flex min-h-screen">
      <aside className={cx("sticky top-0 hidden h-screen shrink-0 border-r border-border bg-card/70 backdrop-blur-xl transition-[width] duration-300 lg:block", collapsed ? "w-[72px]" : "w-60")}>
        {sidebar(false)}
      </aside>
      {mobile && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-sm animate-fade-in" onClick={() => setMobile(false)} />
          <aside className="absolute inset-y-0 left-0 w-72 border-r border-border bg-card shadow-pop animate-[fade-up_0.25s_ease-out]">{sidebar(true)}</aside>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex h-16 items-center gap-3 border-b border-border bg-background/75 px-4 backdrop-blur-xl sm:px-6">
          <button type="button" aria-label="Open menu" onClick={() => setMobile(true)} className="text-muted lg:hidden"><Menu className="size-5" /></button>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">{current?.label ?? "FinResearch"}</p>
            <p className="hidden truncate text-xs text-muted sm:block">{current?.description}</p>
          </div>
          <button type="button" onClick={() => setPalette(true)}
            className="ml-auto flex h-9 w-full min-w-0 max-w-72 items-center gap-2 rounded-lg border border-border bg-card px-3 text-sm text-muted transition hover:border-border-strong">
            <Search className="size-4" />
            <span className="flex-1 truncate text-left">Search or jump to…</span>
            <kbd className="hidden rounded border border-border px-1.5 text-[10px] sm:inline">⌘K</kbd>
          </button>
          <ApiStatus />
          <AlertBadge />
          <Link href="/help" title="Help (press ?)" aria-label="Help" className="grid size-9 place-items-center rounded-lg text-muted transition hover:bg-background-subtle hover:text-foreground">
            <CircleHelp className="size-4" />
          </Link>
          <ThemeToggle />
          <ProfileMenu />
        </header>
        <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 sm:px-6 lg:py-8">{children}</main>
        <footer className="border-t border-border px-6 py-4 text-center text-[11px] text-muted">
          FinResearch is a personal research tool, not investment advice. Every figure is cited to its source; check the
          evidence before you act. <Link href="/help#disclaimer" className="underline underline-offset-2">Read more</Link>
        </footer>
      </div>
      <CommandPalette open={palette} onClose={closePalette} />
      <WelcomeTour />
      <PreferenceEffects />
    </div>
  );
}
