"use client";

// Report reader parts: table of contents with scroll-spy, reading progress and the verdict hero parsed from the
// report's own "Verdict" table (every report kind has one; if a report has none the hero shows only the basics).

import { useEffect, useState } from "react";

import { cx } from "@/components/ui";

export type Heading = { id: string; level: 2 | 3; text: string };

const CITE_ANY = /\s*\[C\d+\](\(#cite-\d+\))?/g;
export const plain = (md: string) =>
  md.replace(CITE_ANY, "").replace(/\*\*|__|`/g, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/\s+/g, " ").trim();

/** Headings keyed by their source line, so the rendered <h2>/<h3> (which know their line) get the same id. */
export function extractHeadings(md: string): Heading[] {
  const out: Heading[] = [];
  let fence = false;
  md.split("\n").forEach((line, i) => {
    if (/^\s*```/.test(line)) fence = !fence;
    const m = !fence && line.match(/^(#{2,3})\s+(.+)$/);
    if (m) out.push({ id: `sec-${i + 1}`, level: m[1].length as 2 | 3, text: plain(m[2]) });
  });
  return out;
}

export const headingId = (node: { position?: { start: { line: number } } } | undefined) =>
  node?.position ? `sec-${node.position.start.line}` : undefined;

/** Active heading id and reading progress (0..1) of `el`, updated on scroll. */
export function useReadingState(el: HTMLElement | null, ids: string[]) {
  const [active, setActive] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    if (!el) return;
    let raf = 0;
    const update = () => {
      raf = 0;
      const r = el.getBoundingClientRect();
      const total = r.height - window.innerHeight * 0.6;
      setProgress(Math.max(0, Math.min(1, total > 0 ? -r.top / total : 1)));
      let cur: string | null = null;
      for (const id of ids) {
        const h = document.getElementById(id);
        if (h && h.getBoundingClientRect().top < 140) cur = id;
        else if (h) break;
      }
      setActive(cur ?? ids[0] ?? null);
    };
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [el, ids]);
  return { active, progress };
}

export function Toc({ headings, active, onPick }: { headings: Heading[]; active: string | null; onPick?: (id: string) => void }) {
  return (
    <ul className="space-y-0.5 border-l border-border text-[13px]">
      {headings.map((h) => (
        <li key={h.id}>
          <a
            href={`#${h.id}`}
            onClick={(e) => {
              e.preventDefault();
              document.getElementById(h.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
              history.replaceState(null, "", `#${h.id}`);
              onPick?.(h.id);
            }}
            className={cx(
              "-ml-px block border-l-2 py-1 leading-snug transition",
              h.level === 3 ? "pl-6 text-xs" : "pl-3",
              active === h.id ? "border-brand font-medium text-brand" : "border-transparent text-muted hover:border-border-strong hover:text-foreground",
            )}
          >
            {h.text}
          </a>
        </li>
      ))}
    </ul>
  );
}

export type VerdictInfo = { word: string | null; confidence: string | null; rows: { label: string; value: string }[] };

const cells = (line: string) =>
  line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());

/** Reads the first table under a heading containing "verdict". */
export function parseVerdict(md: string): VerdictInfo | null {
  const lines = md.split("\n");
  const start = lines.findIndex((l) => /^#{2,3}\s.*verdict/i.test(l));
  if (start < 0) return null;
  const rows: { label: string; value: string }[] = [];
  let inTable = false;
  for (let i = start + 1; i < lines.length; i++) {
    const l = lines[i];
    if (/^#{1,3}\s/.test(l)) break;
    if (l.trim().startsWith("|")) {
      inTable = true;
      const c = cells(l);
      if (c.every((x) => /^:?-+:?$/.test(x) || !x)) continue;
      if (c.length >= 2) rows.push({ label: c[0].replace(/\*\*/g, "").trim(), value: c.slice(1).join(" | ") });
    } else if (inTable) break;
  }
  if (rows.length) rows.shift(); // header row
  const text = rows.map((r) => `${r.label} ${r.value}`).join(" ");
  const vRow = rows.find((r) => /^(overall verdict|verdict)$/i.test(r.label)) ?? rows.find((r) => /^\*\*[A-Z]{3,}/.test(r.value));
  const word = vRow?.value.match(/\*\*([A-Z][A-Z-]+(?: [A-Z][A-Z-]+)*)/)?.[1] ?? null;
  const conf = rows.find((r) => /^confidence$/i.test(r.label))?.value ?? text.match(/Confidence:?\**\s*\**([A-Za-z-]+)/i)?.[1] ?? null;
  return {
    word,
    confidence: conf ? plain(conf).split(/[.\s]/)[0].toLowerCase() : null,
    rows: rows.filter((r) => r !== vRow && !/^confidence$/i.test(r.label)),
  };
}

export function verdictTone(word: string | null): "gain" | "loss" | "warn" | "neutral" {
  if (!word) return "neutral";
  if (/^(APPLY|BUY|ACCUMULATE|SUBSCRIBE|INVEST|STRONG BUY|SIP ONLY)$/.test(word) || /^APPLY \(/.test(word)) return "gain";
  if (/AVOID|SKIP|SELL|EXIT|REDEEM|REDUCE|SWITCH/.test(word)) return "loss";
  return "warn";
}
