"use client";

import { Check, RotateCcw, Save } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";

import { Button, cx } from "@/components/ui";
import type { AvatarColor, Preferences, Profile } from "@/lib/api";
import { DEFAULT_TIME_FRAMES, DEFAULT_WATCH } from "@/lib/timeframes";

// ------------------------------------------------------------------ avatar

/** Full static class strings (Tailwind only sees literal class names). */
export const AVATAR_GRADIENT: Record<AvatarColor, string> = {
  brand: "from-brand to-accent",
  accent: "from-accent to-info",
  gain: "from-gain to-brand",
  info: "from-info to-accent",
  warn: "from-warn to-loss",
  loss: "from-loss to-accent",
};

export const AVATAR_COLORS = Object.keys(AVATAR_GRADIENT) as AvatarColor[];

export function initialsOf(name: string | null | undefined) {
  const parts = (name ?? "").trim().split(/\s+/).filter(Boolean);
  return (parts.length ? parts.slice(0, 2).map((p) => p[0]).join("") : "FR").toUpperCase();
}

export function Avatar({ name, color, size = "md", className }: {
  name?: string | null; color?: AvatarColor | null; size?: "sm" | "md" | "lg"; className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cx(
        "grid shrink-0 place-items-center rounded-full bg-gradient-to-br font-semibold text-white",
        AVATAR_GRADIENT[color ?? "brand"],
        size === "lg" ? "size-16 text-xl shadow-glow sm:size-20 sm:text-2xl" : size === "md" ? "size-10 text-sm" : "size-8 text-xs",
        className,
      )}
    >
      {initialsOf(name)}
    </span>
  );
}

// ------------------------------------------------------------------ profile defaults

export const DEFAULT_PREFERENCES: Preferences = {
  default_landing: "/",
  number_format: "lakh_crore",
  compact_tables: false,
  reduce_motion: false,
};

/** Fill the optional identity fields so older API builds (without them) still render and save cleanly. */
export function withDefaults(p: Profile): Profile {
  return {
    ...p,
    display_name: p.display_name ?? "",
    avatar_color: p.avatar_color ?? null,
    preferences: {
      ...DEFAULT_PREFERENCES, ...(p.preferences ?? {}),
      time_frames: { ...DEFAULT_TIME_FRAMES, ...(p.preferences?.time_frames ?? {}) },
      watch: { ...DEFAULT_WATCH, ...(p.preferences?.watch ?? {}) },
    },
  };
}

// ------------------------------------------------------------------ reduce motion

const MOTION_KEY = "finresearch.reduce-motion";

/** Apply (or preview) the "reduce motion" preference; globals.css neutralises animation under data-reduce-motion. */
export function applyReduceMotion(on: boolean, persist = true) {
  document.documentElement.dataset.reduceMotion = on ? "1" : "0";
  if (persist) {
    try { localStorage.setItem(MOTION_KEY, on ? "1" : "0"); } catch { /* storage blocked */ }
  }
}

/** Mounted once by the shell: restores the saved reduce-motion choice on every page. */
export function PreferenceEffects() {
  useEffect(() => {
    try {
      if (localStorage.getItem(MOTION_KEY) === "1") applyReduceMotion(true, false);
    } catch { /* storage blocked */ }
  }, []);
  return null;
}

// ------------------------------------------------------------------ switch

export function Switch({ checked, onChange, label, description }: {
  checked: boolean; onChange: (v: boolean) => void; label: ReactNode; description?: ReactNode;
}) {
  return (
    <label className="flex cursor-pointer items-start justify-between gap-4">
      <span className="min-w-0">
        <span className="block text-sm font-medium">{label}</span>
        {description && <span className="mt-0.5 block text-xs text-muted">{description}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cx(
          "relative mt-0.5 h-6 w-11 shrink-0 rounded-full ring-1 ring-inset transition duration-200",
          checked ? "bg-brand ring-brand" : "bg-background-subtle ring-border-strong",
        )}
      >
        <span
          className={cx(
            "absolute top-0.5 left-0.5 size-5 rounded-full bg-white shadow-sm transition-transform duration-200",
            checked && "translate-x-5",
          )}
        />
      </button>
    </label>
  );
}

// ------------------------------------------------------------------ save bar + toast

/** Sticky bar that slides up while there are unsaved changes; shows a success toast after a save. */
export function SaveBar({ dirty, saving, error, savedAt, onSave, onDiscard, invalid, label = "You have unsaved changes" }: {
  dirty: boolean; saving: boolean; error?: string | null; savedAt: number | null; onSave: () => void; onDiscard: () => void;
  invalid?: string | null; label?: string;
}) {
  const [toast, setToast] = useState(false);
  useEffect(() => {
    if (!savedAt) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- show the toast for each new save
    setToast(true);
    const t = setTimeout(() => setToast(false), 3200);
    return () => clearTimeout(t);
  }, [savedAt]);

  return (
    <>
      {dirty && (
        <div className="sticky bottom-3 z-30 mt-6 animate-fade-up">
          <div className="flex flex-wrap items-center gap-3 rounded-xl border border-border-strong bg-card/95 px-4 py-3 shadow-pop backdrop-blur-xl">
            <span className="size-2 shrink-0 rounded-full bg-warn animate-pulse-ring text-warn" />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">{label}</p>
              {(error || invalid) && <p className="text-xs text-loss">{error ?? invalid}</p>}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" icon={<RotateCcw className="size-3.5" />} onClick={onDiscard} disabled={saving}>
                Discard
              </Button>
              <Button icon={<Save className="size-3.5" />} onClick={onSave} disabled={saving || !!invalid}>
                {saving ? "Saving…" : "Save changes"}
              </Button>
            </div>
          </div>
        </div>
      )}
      {toast && (
        <div role="status" className="fixed right-4 bottom-4 z-[70] flex items-center gap-3 rounded-xl border border-gain/30 bg-card px-4 py-3 shadow-pop animate-scale-in">
          <span className="grid size-8 place-items-center rounded-full bg-gain-soft text-gain">
            <Check className="size-4 animate-scale-in" strokeWidth={3} />
          </span>
          <div>
            <p className="text-sm font-medium">Saved</p>
            <p className="text-xs text-muted">Your next suggestion will use these settings.</p>
          </div>
        </div>
      )}
    </>
  );
}

/** Dirty-tracking editable copy of a loaded value. */
export function useDraft<T>(loaded: T | null) {
  const [base, setBase] = useState<T | null>(null);
  const [draft, setDraft] = useState<T | null>(null);
  useEffect(() => {
    if (loaded && !base) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- seed the editable copy once the data loads
      setBase(loaded);
      setDraft(loaded);
    }
  }, [loaded, base]);
  const dirty = !!draft && !!base && JSON.stringify(draft) !== JSON.stringify(base);
  return {
    draft, setDraft, dirty,
    discard: () => setDraft(base),
    commit: (saved: T) => { setBase(saved); setDraft(saved); },
  };
}

// ------------------------------------------------------------------ link styled as a button

const LINK_BTN = {
  primary: "bg-brand text-brand-fg hover:bg-brand-strong shadow-sm hover:shadow-glow",
  secondary: "bg-card text-foreground ring-1 ring-inset ring-border hover:bg-card-hover hover:ring-border-strong",
  ghost: "text-muted hover:bg-background-subtle hover:text-foreground",
};

/** A navigation link that looks like a `Button` (a link inside a button is invalid HTML). */
export function LinkButton({ href, variant = "primary", icon, children, onClick }: {
  href: string; variant?: keyof typeof LINK_BTN; icon?: ReactNode; children: ReactNode; onClick?: () => void;
}) {
  return (
    <Link href={href} onClick={onClick}
      className={cx("inline-flex h-8 items-center justify-center gap-1.5 rounded-lg px-3 text-xs font-medium transition duration-150 active:scale-[0.97]", LINK_BTN[variant])}>
      {icon}
      {children}
    </Link>
  );
}

/** Like `Field`, but a <div>: use it when the label holds an InfoTip button or the control is a button group, since a
 * <label> forwards clicks to its first button. Give the control its own aria-label. */
export function Labelled({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="block space-y-1.5">
      <span className="block text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-muted">{hint}</span>}
    </div>
  );
}
