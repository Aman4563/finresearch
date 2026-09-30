"use client";

// "Blur amounts": a privacy toggle for screen sharing. It sets data-privacy="blur" on <html> (saved in this browser)
// and marks every element whose own text shows a rupee amount (₹) with data-inr, plus anything marked data-private;
// globals.css blurs those (hidden when printed). Shift+B toggles it; the command palette has it too.

import { EyeOff, Eye } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { cx } from "@/components/ui";
import { PRIVACY_KEY as KEY } from "@/lib/privacy-script";

export const PRIVACY_EVENT = "finresearch:privacy";

function tag(root: ParentNode) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.nodeValue && n.nodeValue.includes("₹") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
  });
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (el && !el.hasAttribute("data-inr") && !el.closest("input, textarea, select")) el.setAttribute("data-inr", "");
  }
}

/** Keep tagging new text while blur is on (pages load data after the first paint). */
function PrivacyTagger() {
  useEffect(() => {
    let raf = 0;
    const run = () => { raf = 0; if (document.documentElement.dataset.privacy === "blur") tag(document.body); };
    const schedule = () => { if (!raf) raf = requestAnimationFrame(run); };
    const obs = new MutationObserver(schedule);
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    schedule();
    window.addEventListener(PRIVACY_EVENT, schedule);
    return () => { obs.disconnect(); if (raf) cancelAnimationFrame(raf); window.removeEventListener(PRIVACY_EVENT, schedule); };
  }, []);
  return null;
}

export function usePrivacy() {
  const [blur, setBlur] = useState(false);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read the saved choice after hydration
    setBlur(document.documentElement.dataset.privacy === "blur");
    const on = () => setBlur(document.documentElement.dataset.privacy === "blur");
    window.addEventListener(PRIVACY_EVENT, on);
    return () => window.removeEventListener(PRIVACY_EVENT, on);
  }, []);
  const toggle = useCallback(() => {
    const next = document.documentElement.dataset.privacy !== "blur";
    if (next) document.documentElement.dataset.privacy = "blur";
    else delete document.documentElement.dataset.privacy;
    try { localStorage.setItem(KEY, next ? "blur" : "show"); } catch { /* private mode: lasts for this page */ }
    window.dispatchEvent(new Event(PRIVACY_EVENT));
  }, []);
  return { blur, toggle };
}

export function PrivacyToggle() {
  const { blur, toggle } = usePrivacy();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el?.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el?.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "B" && e.shiftKey) { e.preventDefault(); toggle(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);
  const label = blur ? "Amounts blurred (Shift+B to show)" : "Blur amounts for screen sharing (Shift+B)";
  return (
    <>
      <button type="button" onClick={toggle} title={label} aria-label={label} aria-pressed={blur}
        className={cx("hidden size-9 place-items-center sm:grid rounded-lg transition hover:bg-background-subtle hover:text-foreground", blur ? "text-brand" : "text-muted")}>
        <span key={String(blur)} className="animate-scale-in">{blur ? <EyeOff className="size-4" /> : <Eye className="size-4" />}</span>
      </button>
      <PrivacyTagger />
    </>
  );
}
