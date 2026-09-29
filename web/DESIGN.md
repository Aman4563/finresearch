# FinResearch UI design guide

The dashboard should feel like a calm research terminal: dark-first (light supported), dense but readable, numbers
in a tabular mono font, one teal brand accent with indigo as secondary, green/red only for gains/losses and pass/fail.
Motion is quiet and purposeful (enter, hover, live data), never decorative loops. Respect `prefers-reduced-motion`
(globals.css already neutralises animations).

## Tokens (src/app/globals.css)
Use Tailwind colours mapped from tokens, never raw palette colours (no `bg-slate-900`, `text-rose-600`):
`bg-background`, `bg-background-subtle`, `bg-card`, `bg-card-hover`, `text-foreground`, `text-muted`, `border-border`,
`border-border-strong`, `brand` / `brand-strong` / `brand-soft` / `brand-fg`, `accent` / `accent-soft`,
`gain` / `gain-soft`, `loss` / `loss-soft`, `warn` / `warn-soft`, `info` / `info-soft`. Shadows: `shadow-card`,
`shadow-pop`, `shadow-glow`. Charts use `var(--chart-1..6)`, `var(--gain)`, `var(--loss)`.
Numbers: add the `num` class (JetBrains Mono, tabular). Theme is `data-theme` on <html>; `dark:` variants follow it.

## Animations
`animate-fade-up` (section entrance), `animate-fade-in`, `animate-scale-in` (popovers, new values), `.stagger` on a
parent (children fade up one after another), `animate-pulse-ring` (live dot), `.skeleton` / `<Skeleton>` while loading,
`<AnimatedNumber>` / `useCountUp` for KPIs, `Progress` animates width. Route changes cross-fade (ViewTransition "page").

## Components
- `src/components/ui.tsx`: `PageHeader` (every page starts with one: icon, title, one-line purpose, actions),
  `Card` (title, subtitle, icon, actions, `help` tooltip, `interactive` hover lift), `Stat` (KPI tile with count-up and
  `Delta`), `Badge` (status → tone; live statuses pulse), `Button` (primary/secondary/ghost/danger, sm/md, icon),
  `Segmented`, `InfoTip` (explain jargon inline: QIB, NII, GMP, YTM, XIRR, IV, greeks…), `Callout`, `ErrorNote`
  (with `onRetry`), `EmptyState` (always explain what to do next), `Skeleton`/`SkeletonRows`, `Modal`, `Table`
  (styled rows, hover), `Field` + `inputClass` for forms, `Progress`, `cx`.
- `src/components/charts.tsx`: `TimeSeriesChart` (area/line, range selector 1M…ALL, change %, references),
  `BarsChart` (vertical/horizontal, reference line e.g. 1x subscribed, per-row colours), `DonutChart` (hover
  highlight, legend with %), `PayoffChart` (profit green / loss red, spot line, breakevens), `Sparkline`, `Gauge`,
  formatters `fmtINR`, `fmtCompactINR`, `fmtPct`, `fmtTimes`, `shortDate`.
- `src/components/shell.tsx`: sidebar `NAV`, command palette (⌘K, `/`), `g <key>` shortcuts, `?` → /help#shortcuts,
  theme toggle, profile menu, API status. Add new pages to `NAV` there.
- Icons: `lucide-react` only, `size-4` inline / `size-5` in headers.

## Page rules
1. Start with `PageHeader`, then KPI `Stat` tiles where there are headline numbers, then content cards.
2. Loading → skeletons of the final shape; errors → `ErrorNote` with retry; nothing yet → `EmptyState` with the next step.
3. Explain every finance term the first time it appears on a page with `InfoTip` (plain English, one or two sentences).
4. Dates: `day()` for calendar dates, `when()` for timestamps (lib/api.ts). Money: ₹ with Indian grouping.
5. Tables: `Table`; right-align numbers with `num`; keep the primary action at the row end.
6. Mobile: everything must work at 375px wide (stack grids with `sm:`/`lg:` breakpoints; tables scroll).
7. Destructive or plan-consuming actions (a research run uses the Claude plan window) keep a confirmation.
