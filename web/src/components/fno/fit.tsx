"use client";

// Figures that never break or spill (#326): a rupee amount stays on one line and, when its box is narrower than the
// figure (a nine-digit loss in a half-width phone tile), the font shrinks to fit instead of the text being cut off;
// readings made of several parts wrap between the parts, never inside one.

import { Children, Fragment, type ReactNode } from "react";

import { cx } from "@/components/ui";

/** One figure on one line whose font shrinks, when its box is too narrow, so the whole figure fits. Pure CSS: the
 * wrapper is a size container and the figure's font is min(its normal size, the container's width ÷ its length in
 * monospace advances), so nothing is measured in script and nothing can flicker. Pass `title` for the exact value. */
export function Fig({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  const text = Children.toArray(children).filter((c) => typeof c === "string" || typeof c === "number").join("");
  // a JetBrains Mono advance is 0.6em; 0.62 leaves room for the ₹ and − glyphs and rounding
  const ems = Math.max(1, text.length) * 0.62;
  return (
    <span className="block min-w-0 [container-type:inline-size]" title={title}>
      <span className={cx("num whitespace-nowrap", className)} style={{ fontSize: `min(1em, calc(100cqi / ${ems.toFixed(2)}))` }}>{children}</span>
    </span>
  );
}

/** Text whose parts each stay whole ("price +0.8%", "slope 0.40%/10d") but may wrap between the parts. */
export function Parts({ parts, sep = " · ", className }: { parts: ReactNode[]; sep?: string; className?: string }) {
  return (
    <span className={className}>
      {parts.map((p, i) => (
        // the space after each part sits outside its no-wrap box: that is where the line may break
        <Fragment key={i}>
          <span className="whitespace-nowrap">{p}{i < parts.length - 1 ? sep.trimEnd() : ""}</span>
          {i < parts.length - 1 && " "}
        </Fragment>
      ))}
    </span>
  );
}
