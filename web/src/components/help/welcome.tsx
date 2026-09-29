"use client";

import { ArrowLeft, ArrowRight, CircleHelp, FileSearch, ListChecks, Rocket, ShieldCheck, UserRound, X } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { LinkButton } from "@/components/profile/common";
import { Button, cx } from "@/components/ui";

const KEY = "finresearch.welcome";
/** Dispatch on window to open the tour again (e.g. from the help centre). */
export const OPEN_WELCOME = "finresearch:open-welcome";

export function openWelcome() {
  window.dispatchEvent(new Event(OPEN_WELCOME));
}

const STEPS: { icon: ReactNode; title: string; text: string; points: string[] }[] = [
  {
    icon: <Rocket className="size-6" />,
    title: "Welcome to FinResearch",
    text: "Your personal research desk for Indian IPOs, stocks, mutual funds, bonds and F&O.",
    points: ["Live NSE/BSE data and AMFI NAVs", "Deep reports written by a team of AI researchers", "Everything runs on your own Mac"],
  },
  {
    icon: <FileSearch className="size-6" />,
    title: "Every number is checked",
    text: "Researchers must cite the exact page and line (or link and time) for each figure. Python then verifies quotes and recomputes numbers before a report is published.",
    points: ["Click any figure to see its source", "Unverified claims are clearly marked", "Grey-market talk never drives a verdict"],
  },
  {
    icon: <ShieldCheck className="size-6" />,
    title: "Suggestions follow your rules",
    text: "Tell the app your capital, category and risk, and set red lines like “skip if QIB is below 1x”. Your rules always override the AI.",
    points: ["Set up your profile first", "Add rules from ready-made templates", "Press ? any time for help and shortcuts"],
  },
];

/** First-visit tour. Mounted once by the shell; `?welcome=0` suppresses it (screenshots), dismissal is remembered. */
export function WelcomeTour() {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    let seen = false;
    try { seen = localStorage.getItem(KEY) === "done"; } catch { seen = true; /* storage blocked: don't nag every page */ }
    const suppressed = new URLSearchParams(window.location.search).get("welcome") === "0";
    // eslint-disable-next-line react-hooks/set-state-in-effect -- decide after hydration (localStorage is client-only)
    if (!seen && !suppressed) setOpen(true);
    const reopen = () => { setStep(0); setOpen(true); };
    window.addEventListener(OPEN_WELCOME, reopen);
    return () => window.removeEventListener(OPEN_WELCOME, reopen);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    try { localStorage.setItem(KEY, "done"); } catch { /* storage blocked */ }
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      else if (e.key === "ArrowRight") setStep((s) => Math.min(STEPS.length - 1, s + 1));
      else if (e.key === "ArrowLeft") setStep((s) => Math.max(0, s - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  if (!open) return null;
  const s = STEPS[step];
  const last = step === STEPS.length - 1;

  return (
    <div className="fixed inset-0 z-[80] flex items-end justify-center bg-black/50 p-3 backdrop-blur-sm animate-fade-in sm:items-center sm:p-4" onClick={close}>
      <div role="dialog" aria-modal="true" aria-labelledby="welcome-title"
        className="relative w-full max-w-md overflow-hidden rounded-2xl border border-border bg-card shadow-pop animate-scale-in"
        onClick={(e) => e.stopPropagation()}>
        <div className="relative h-32 overflow-hidden bg-gradient-to-br from-brand to-accent">
          <svg viewBox="0 0 400 128" className="absolute inset-0 size-full text-white/25" preserveAspectRatio="none" aria-hidden>
            <path d="M0 100 L60 84 L110 92 L170 60 L230 70 L290 36 L350 44 L400 16" fill="none" stroke="currentColor" strokeWidth="2.5" />
            <path d="M0 100 L60 84 L110 92 L170 60 L230 70 L290 36 L350 44 L400 16 L400 128 L0 128 Z" fill="currentColor" opacity="0.25" />
          </svg>
          <span key={step} className="absolute bottom-4 left-5 grid size-14 place-items-center rounded-2xl bg-white/15 text-white ring-1 ring-white/30 backdrop-blur animate-scale-in">
            {s.icon}
          </span>
          <button type="button" aria-label="Close welcome" onClick={close}
            className="absolute top-3 right-3 grid size-8 place-items-center rounded-lg text-white/80 transition hover:bg-white/15 hover:text-white">
            <X className="size-4" />
          </button>
          <span className="absolute top-4 left-5 text-[11px] font-medium uppercase tracking-widest text-white/80">
            Step {step + 1} of {STEPS.length}
          </span>
        </div>

        <div key={step} className="px-5 pt-5 pb-4 animate-fade-up">
          <h2 id="welcome-title" className="text-lg font-semibold tracking-tight">{s.title}</h2>
          <p className="mt-1 text-sm text-muted">{s.text}</p>
          <ul className="mt-3 space-y-1.5">
            {s.points.map((pt) => (
              <li key={pt} className="flex items-start gap-2 text-sm">
                <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-brand" />
                {pt}
              </li>
            ))}
          </ul>
          {last && (
            <div className="mt-4 grid grid-cols-3 gap-2">
              {[
                { href: "/profile", label: "Profile", icon: <UserRound className="size-4" /> },
                { href: "/rules", label: "Rules", icon: <ListChecks className="size-4" /> },
                { href: "/help", label: "Help", icon: <CircleHelp className="size-4" /> },
              ].map((l) => (
                <Link key={l.href} href={l.href} onClick={close}
                  className="flex flex-col items-center gap-1 rounded-xl border border-border p-2.5 text-xs font-medium text-muted transition hover:border-brand/50 hover:bg-brand-soft hover:text-foreground">
                  <span className="text-brand">{l.icon}</span>
                  {l.label}
                </Link>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center gap-3 border-t border-border px-5 py-3">
          <div className="flex gap-1.5" aria-hidden>
            {STEPS.map((_, i) => (
              <button key={i} type="button" tabIndex={-1} onClick={() => setStep(i)}
                className={cx("h-1.5 rounded-full transition-all duration-300", i === step ? "w-5 bg-brand" : "w-1.5 bg-border-strong")} />
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            {step > 0 ? (
              <Button variant="ghost" icon={<ArrowLeft className="size-3.5" />} onClick={() => setStep(step - 1)}>Back</Button>
            ) : (
              <Button variant="ghost" onClick={close}>Skip</Button>
            )}
            {last ? (
              <LinkButton href="/profile" onClick={close} icon={<UserRound className="size-3.5" />}>Set up my profile</LinkButton>
            ) : (
              <Button onClick={() => setStep(step + 1)}>Next <ArrowRight className="size-3.5" /></Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
