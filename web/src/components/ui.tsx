"use client";

import { CircleHelp, Inbox, TriangleAlert, X } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

// ------------------------------------------------------------------ status badges

type Tone = "neutral" | "brand" | "gain" | "loss" | "warn" | "info" | "accent";

const TONE: Record<Tone, string> = {
  neutral: "bg-background-subtle text-muted ring-border",
  brand: "bg-brand-soft text-brand-strong ring-brand/25",
  gain: "bg-gain-soft text-gain ring-gain/25",
  loss: "bg-loss-soft text-loss ring-loss/25",
  warn: "bg-warn-soft text-warn ring-warn/25",
  info: "bg-info-soft text-info ring-info/25",
  accent: "bg-accent-soft text-accent ring-accent/25",
};

/** Run, step, claim and alert statuses -> a tone. Unknown statuses are neutral. */
const STATUS: Record<string, Tone> = {
  done: "gain", verified: "gain", ok: "gain", clear: "gain", APPLY: "gain", applied: "gain",
  running: "info", live: "info", open: "info", current: "info",
  pending: "neutral", upcoming: "neutral", closed: "neutral", cancelled: "neutral",
  unverified: "warn", needs_review: "warn", paused: "warn", deferred: "warn", missed: "warn", "APPLY-CONDITIONAL": "warn",
  blocked: "loss", failed: "loss", contradicted: "loss", unsupported: "loss", SKIP: "loss", fired: "loss",
};

const LIVE = new Set(["running", "live", "open", "current"]);

export function Badge({ status, tone, children, dot }: { status?: string; tone?: Tone; children?: ReactNode; dot?: boolean }) {
  const t = tone ?? STATUS[status ?? ""] ?? "neutral";
  const pulse = dot ?? LIVE.has(status ?? "");
  return (
    <span className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset whitespace-nowrap", TONE[t])}>
      {pulse && <span className="size-1.5 rounded-full bg-current animate-pulse-ring" />}
      {children ?? (status ?? "").replaceAll("_", " ")}
    </span>
  );
}

// ------------------------------------------------------------------ surfaces

export function Card({
  title, subtitle, children, actions, icon, className, interactive, padded = true, help,
}: {
  title?: ReactNode; subtitle?: ReactNode; children: ReactNode; actions?: ReactNode; icon?: ReactNode;
  className?: string; interactive?: boolean; padded?: boolean; help?: ReactNode;
}) {
  return (
    <section
      className={cx(
        "rounded-xl border border-border bg-card shadow-card",
        interactive && "transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow",
        padded && "p-4 sm:p-5",
        className,
      )}
    >
      {(title || actions) && (
        <div className={cx("flex items-start justify-between gap-3", padded ? "mb-4" : "px-4 pt-4 pb-3 sm:px-5")}>
          <div className="flex min-w-0 items-start gap-2.5">
            {icon && <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand">{icon}</span>}
            <div className="min-w-0">
              {title && (
                <h2 className="flex items-center gap-1.5 text-sm font-semibold tracking-tight">
                  {title}
                  {help && <InfoTip>{help}</InfoTip>}
                </h2>
              )}
              {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
            </div>
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/** Page title block: what this page is for, in one line, with optional actions. */
export function PageHeader({ title, description, actions, eyebrow, icon }: {
  title: ReactNode; description?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode; icon?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4 animate-fade-up">
      <div className="flex min-w-0 items-start gap-3">
        {icon && (
          <span className="grid size-11 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-brand to-accent text-white shadow-glow">
            {icon}
          </span>
        )}
        <div className="min-w-0">
          {eyebrow && <p className="mb-0.5 text-xs font-medium uppercase tracking-wider text-brand">{eyebrow}</p>}
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {description && <p className="mt-1 max-w-3xl text-sm text-muted">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ numbers

/** Animates a number from its previous value to the new one (respects reduced motion). */
export function useCountUp(value: number | null | undefined, ms = 700) {
  const [shown, setShown] = useState<number | null>(value ?? null);
  const from = useRef<number>(value ?? 0);
  useEffect(() => {
    if (value == null || Number.isNaN(value)) return;
    const reduce = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const tick = (now: number) => {
      const t = reduce ? 1 : Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 3);
      setShown(a + (value - a) * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
      else from.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, ms]);
  return shown;
}

export function AnimatedNumber({ value, format = (n) => n.toLocaleString("en-IN", { maximumFractionDigits: 2 }) }: {
  value: number | null | undefined; format?: (n: number) => string;
}) {
  const shown = useCountUp(value);
  return <span className="num">{shown == null ? "—" : format(shown)}</span>;
}

/** A KPI tile: label, big number, optional change and footnote. */
export function Stat({ label, value, format, display, delta, deltaLabel, hint, icon, tone = "brand", href, help }: {
  label: ReactNode; value?: number | null; format?: (n: number) => string; display?: ReactNode;
  delta?: number | null; deltaLabel?: string; hint?: ReactNode; icon?: ReactNode; tone?: Tone; href?: string; help?: ReactNode;
}) {
  const body = (
    <div className="group relative overflow-hidden rounded-xl border border-border bg-card p-4 shadow-card transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow">
      <div className="flex items-center justify-between gap-2">
        <p className="flex items-center gap-1 text-xs font-medium text-muted">
          {label}
          {help && <InfoTip>{help}</InfoTip>}
        </p>
        {icon && <span className={cx("grid size-7 place-items-center rounded-lg ring-1 ring-inset", TONE[tone])}>{icon}</span>}
      </div>
      <p className="mt-2 text-2xl font-semibold tracking-tight">
        {display ?? <AnimatedNumber value={value} format={format} />}
      </p>
      <div className="mt-1 flex items-center gap-2 text-xs">
        {delta != null && <Delta value={delta} />}
        {(deltaLabel || hint) && <span className="truncate text-muted">{deltaLabel ?? hint}</span>}
      </div>
      <span className="pointer-events-none absolute -right-8 -bottom-8 size-24 rounded-full bg-brand/5 transition group-hover:scale-125" />
    </div>
  );
  return href ? <Link href={href} className="block">{body}</Link> : body;
}

/** A signed change, green up / red down. `value` is in percent. */
export function Delta({ value, suffix = "%", digits = 2 }: { value: number | null | undefined; suffix?: string; digits?: number }) {
  if (value == null || Number.isNaN(value)) return <span className="text-muted">—</span>;
  const up = value > 0, flat = value === 0;
  return (
    <span className={cx("num inline-flex items-center font-medium", flat ? "text-muted" : up ? "text-gain" : "text-loss")}>
      {flat ? "" : up ? "▲ " : "▼ "}
      {Math.abs(value).toFixed(digits)}
      {suffix}
    </span>
  );
}

/** Horizontal progress bar; `value` in 0..max. Colour turns warn/loss past thresholds when `thresholds` is set. */
export function Progress({ value, max = 1, tone = "brand", thresholds, label, className }: {
  value: number | null | undefined; max?: number; tone?: Tone; thresholds?: [number, number]; label?: ReactNode; className?: string;
}) {
  const ratio = value == null ? 0 : Math.max(0, Math.min(1, value / max));
  const t: Tone = thresholds ? (ratio >= thresholds[1] ? "loss" : ratio >= thresholds[0] ? "warn" : tone) : tone;
  const bar: Record<Tone, string> = {
    neutral: "bg-muted", brand: "bg-gradient-to-r from-brand to-accent", gain: "bg-gain", loss: "bg-loss",
    warn: "bg-warn", info: "bg-info", accent: "bg-accent",
  };
  return (
    <div className={className}>
      {label && <div className="mb-1 flex justify-between text-xs text-muted">{label}</div>}
      <div className="h-2 overflow-hidden rounded-full bg-background-subtle">
        <div className={cx("h-full rounded-full transition-[width] duration-700 ease-out", bar[t])} style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ feedback

export function ErrorNote({ error, onRetry }: { error: string | null | undefined; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div role="alert" className="flex items-start gap-2 rounded-lg border border-loss/30 bg-loss-soft px-3 py-2 text-sm text-loss animate-fade-in">
      <TriangleAlert className="mt-0.5 size-4 shrink-0" />
      <p className="flex-1">{error}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="text-xs font-medium underline underline-offset-2">
          Retry
        </button>
      )}
    </div>
  );
}

export function Callout({ tone = "info", title, children, icon }: { tone?: Tone; title?: ReactNode; children: ReactNode; icon?: ReactNode }) {
  return (
    <div className={cx("flex gap-3 rounded-lg px-3.5 py-3 text-sm ring-1 ring-inset", TONE[tone])}>
      {icon && <span className="mt-0.5 shrink-0">{icon}</span>}
      <div className="min-w-0 text-foreground/90">
        {title && <p className="mb-0.5 font-medium text-foreground">{title}</p>}
        {children}
      </div>
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("skeleton rounded-md", className ?? "h-4 w-full")} />;
}

/** Placeholder rows while data loads. */
export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2.5">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={cx("h-4", i % 3 === 2 ? "w-2/3" : "w-full")} />
      ))}
    </div>
  );
}

export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border px-6 py-10 text-center animate-fade-in">
      <span className="mb-3 grid size-11 place-items-center rounded-full bg-brand-soft text-brand">{icon ?? <Inbox className="size-5" />}</span>
      <p className="font-medium">{title}</p>
      {children && <p className="mt-1 max-w-md text-sm text-muted">{children}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ controls

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  icon?: ReactNode;
};

const BTN: Record<NonNullable<ButtonProps["variant"]>, string> = {
  primary: "bg-brand text-brand-fg hover:bg-brand-strong shadow-sm hover:shadow-glow",
  secondary: "bg-card text-foreground ring-1 ring-inset ring-border hover:bg-card-hover hover:ring-border-strong",
  ghost: "text-muted hover:bg-background-subtle hover:text-foreground",
  danger: "bg-loss text-white hover:opacity-90",
};

export function Button({ variant = "primary", size = "sm", icon, className, children, ...props }: ButtonProps) {
  return (
    <button
      type="button"
      {...props}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition duration-150 active:scale-[0.97] disabled:pointer-events-none disabled:opacity-50",
        size === "sm" ? "h-8 px-3 text-xs" : "h-10 px-4 text-sm",
        BTN[variant],
        className,
      )}
    >
      {icon}
      {children}
    </button>
  );
}

export const inputClass =
  "h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground placeholder:text-muted/70 transition focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20";

export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-muted">{hint}</span>}
    </label>
  );
}

/** Pill tabs / segmented control. */
export function Segmented<T extends string>({ value, onChange, options, size = "sm" }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode }[]; size?: "sm" | "md";
}) {
  return (
    <div role="tablist" className="inline-flex rounded-lg bg-background-subtle p-0.5 ring-1 ring-inset ring-border">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={o.value === value}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded-md font-medium transition duration-150",
            size === "sm" ? "px-2.5 py-1 text-xs" : "px-3.5 py-1.5 text-sm",
            o.value === value ? "bg-card text-foreground shadow-sm" : "text-muted hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A small (?) that explains a term on hover or focus. */
export function InfoTip({ children, label = "What is this?" }: { children: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span className="relative inline-flex" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        aria-label={label}
        aria-describedby={open ? id : undefined}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen((o) => !o)}
        className="text-muted/70 transition hover:text-brand"
      >
        <CircleHelp className="size-3.5" />
      </button>
      {open && (
        <span
          id={id}
          role="tooltip"
          className="absolute bottom-full left-1/2 z-50 mb-2 w-64 -translate-x-1/2 rounded-lg border border-border bg-card px-3 py-2 text-left text-xs font-normal leading-relaxed text-foreground shadow-pop animate-scale-in"
        >
          {children}
        </span>
      )}
    </span>
  );
}

/** Centered dialog with a backdrop; closes on Escape or backdrop click. */
export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[12vh] backdrop-blur-sm animate-fade-in" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        className={cx("w-full rounded-xl border border-border bg-card shadow-pop animate-scale-in", wide ? "max-w-3xl" : "max-w-lg")}
        onClick={(e) => e.stopPropagation()}
      >
        {title && (
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <h2 className="text-sm font-semibold">{title}</h2>
            <button type="button" aria-label="Close" onClick={onClose} className="text-muted hover:text-foreground">
              <X className="size-4" />
            </button>
          </div>
        )}
        {children}
      </div>
    </div>
  );
}

/** A table with sticky header styling and row hover; pass <thead>/<tbody> children. */
export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx("-mx-4 overflow-x-auto sm:-mx-5", className)}>
      <table className="w-full min-w-max text-sm [&_td]:px-4 [&_td]:py-2.5 sm:[&_td]:px-5 [&_th]:px-4 [&_th]:py-2 sm:[&_th]:px-5 [&_th]:text-left [&_th]:text-[11px] [&_th]:font-medium [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-muted [&_thead_tr]:border-b [&_thead_tr]:border-border [&_tbody_tr]:border-b [&_tbody_tr]:border-border/60 [&_tbody_tr:last-child]:border-0 [&_tbody_tr]:transition-colors [&_tbody_tr:hover]:bg-card-hover">
        {children}
      </table>
    </div>
  );
}
