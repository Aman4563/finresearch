// Layout checks for the e2e tests (#326): text that overlaps other text, text cut off by an overflow-hidden box, a
// truncated label without a tooltip, a figure split over two lines, and a page wider than the viewport. Measured in the
// browser from the text's own line boxes (Range.getClientRects), clipped to what is actually visible.
import { type Page, type TestInfo, expect } from "@playwright/test";

export type LayoutProblem = { kind: "overlap" | "clipped" | "truncated-no-title" | "number-wraps" | "page-overflow"; detail: string };

/** Problems inside every element matching `selector` (default: the page's main content). */
export async function layoutProblems(page: Page, selector = "main"): Promise<LayoutProblem[]> {
  // measure the final layout: web fonts in (figures refit when they arrive) and a frame for the refit to apply
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  });
  return page.evaluate((sel) => {
    type R = { l: number; t: number; r: number; b: number };
    const problems: { kind: string; detail: string }[] = [];
    const snip = (s: string) => s.trim().replace(/\s+/g, " ").slice(0, 60);
    const inter = (a: R, b: R): R => ({ l: Math.max(a.l, b.l), t: Math.max(a.t, b.t), r: Math.min(a.r, b.r), b: Math.min(a.b, b.b) });
    const ok = (a: R) => a.r - a.l >= 1 && a.b - a.t >= 1;
    const se = document.scrollingElement ?? document.documentElement;
    if (se.scrollWidth > window.innerWidth) {
      // name the boxes that stick out past the viewport while their parent does not (none inside a box that clips or
      // scrolls them)
      const out: string[] = [];
      const sticks = (e: Element | null) => !!e && e.getBoundingClientRect().right > window.innerWidth + 1;
      for (const e of document.body.querySelectorAll("*")) {
        if (out.length >= 3 || !sticks(e) || sticks(e.parentElement)) continue;
        let clipped = false;
        for (let a = e.parentElement; a && !clipped; a = a.parentElement) clipped = getComputedStyle(a).overflowX !== "visible";
        if (!clipped) out.push(`<${e.tagName.toLowerCase()} class="${(e.getAttribute("class") ?? "").slice(0, 60)}"> "${snip(e.textContent ?? "")}"`);
      }
      problems.push({ kind: "page-overflow", detail: `page is ${se.scrollWidth}px wide in a ${window.innerWidth}px viewport: ${out.join("; ")}` });
    }

    const NUM = /(?<![\w-])[−+-]?₹?\d[\d,]*(?:\.\d+)?%?/g; // a figure, not the year in 20-Oct-2026
    const texts: { node: Text; el: Element; rects: R[]; group: Element | null }[] = [];
    for (const root of document.querySelectorAll(sel)) {
      const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode() as Text | null; n; n = walk.nextNode() as Text | null) {
        if (!n.data.trim()) continue;
        const el = n.parentElement!;
        if (el.closest(".sr-only, script, style, svg, [hidden], option, datalist, select")) continue;
        // the body of a closed <details> is not rendered, though its boxes still measure
        const shut = el.closest("details:not([open])");
        if (shut && !el.closest("summary")) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility !== "visible" || Number(cs.opacity) === 0) continue;
        // the visible part: the line boxes clipped by every overflow box above; a sticky or fixed box (a sticky table
        // head over scrolled rows) is meant to cover what scrolls under it, so its text is compared within it only
        let clip: R = { l: -1e9, t: -1e9, r: 1e9, b: 1e9 };
        let cutBy: Element | null = null;
        let group: Element | null = null;
        for (let a: Element | null = el; a && a !== document.documentElement; a = a.parentElement) {
          const s = getComputedStyle(a);
          if (s.display === "none") { clip = { l: 0, t: 0, r: 0, b: 0 }; break; }
          if (!group && (s.position === "sticky" || s.position === "fixed")) group = a;
          if (s.overflowX !== "visible" || s.overflowY !== "visible") {
            const b = a.getBoundingClientRect();
            const next = inter(clip, { l: b.left, t: b.top, r: b.right, b: b.bottom });
            // text cut by a box the reader cannot scroll (hidden / clip), unless it ends in an ellipsis
            if (!cutBy && (s.overflowX === "hidden" || s.overflowX === "clip") && s.textOverflow !== "ellipsis") cutBy = a;
            clip = next;
          }
        }
        const range = document.createRange();
        range.selectNodeContents(n);
        const raw = [...range.getClientRects()].map((x) => ({ l: x.left, t: x.top, r: x.right, b: x.bottom })).filter(ok);
        const vis = raw.map((x) => inter(x, clip)).filter(ok);
        if (!vis.length) continue;
        texts.push({ node: n, el, rects: vis, group });
        // cut off: part of the text is outside a non-scrolling box (2px slack for italics and rounding)
        if (cutBy && raw.some((x) => x.r > clip.r + 2 || x.l < clip.l - 2)) {
          problems.push({ kind: "clipped", detail: `"${snip(n.data)}" is cut off by <${cutBy.tagName.toLowerCase()} class="${(cutBy.getAttribute("class") ?? "").slice(0, 80)}">` });
        }
        // an ellipsis that hides part of the label must come with the full text on hover
        const ell = el.closest("*") as HTMLElement;
        if (cs.textOverflow === "ellipsis" && ell.scrollWidth > ell.clientWidth + 1 && !el.closest("[title]")) {
          problems.push({ kind: "truncated-no-title", detail: `"${snip(n.data)}" is truncated and has no title tooltip` });
        }
        // a figure split over two lines ("−₹12,34,5" / "6,789")
        if (raw.length > 1) {
          for (const m of n.data.matchAll(NUM)) {
            if (m[0].replace(/\D/g, "").length < 4) continue;
            const r = document.createRange();
            r.setStart(n, m.index!);
            r.setEnd(n, m.index! + m[0].length);
            const tops = new Set([...r.getClientRects()].filter((x) => x.width > 0).map((x) => Math.round(x.top)));
            if (tops.size > 1) problems.push({ kind: "number-wraps", detail: `"${m[0]}" wraps inside "${snip(n.data)}"` });
          }
        }
      }
    }
    // overlap: any two text runs whose visible line boxes intersect by more than a hair (sweep on x)
    const boxes = texts.flatMap((x, i) => x.rects.map((r) => ({ i, r })));
    boxes.sort((a, b) => a.r.l - b.r.l);
    const seen = new Set<string>();
    for (let a = 0; a < boxes.length; a++) {
      const A = boxes[a];
      for (let b = a + 1; b < boxes.length && boxes[b].r.l < A.r.r - 2; b++) {
        const B = boxes[b];
        if (A.i === B.i) continue;
        const ta = texts[A.i], tb = texts[B.i];
        if (ta.group !== tb.group) continue;
        const x = inter(A.r, B.r);
        const h = Math.min(A.r.b - A.r.t, B.r.b - B.r.t);
        // line boxes of tightly leaded text touch; real overlap covers a good part of the shorter line
        if (x.r - x.l > 2 && x.b - x.t > Math.max(2, h * 0.3)) {
          const key = `${Math.min(A.i, B.i)}-${Math.max(A.i, B.i)}`;
          if (seen.has(key)) continue;
          seen.add(key);
          problems.push({ kind: "overlap", detail: `"${snip(ta.node.data)}" overlaps "${snip(tb.node.data)}"` });
        }
      }
    }
    return problems as LayoutProblem[];
  }, selector);
}

/** Fails (softly, so every problem is listed) on each layout problem inside `selector`. */
export async function expectCleanLayout(page: Page, where: string, selector = "main") {
  const problems = await layoutProblems(page, selector);
  expect.soft(problems.map((p) => `${p.kind}: ${p.detail}`), `layout problems at ${where}`).toEqual([]);
  return problems;
}

/** A full-page screenshot attached to the test report (and copied to $SHOT_DIR when set, for before/after reviews). */
export async function attachShot(page: Page, testInfo: TestInfo, name: string) {
  const path = testInfo.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true, animations: "disabled" });
  await testInfo.attach(name, { path, contentType: "image/png" });
  const dir = process.env.SHOT_DIR;
  if (dir) {
    const fs = await import("node:fs/promises");
    await fs.mkdir(dir, { recursive: true });
    await fs.copyFile(path, `${dir}/${name}.png`);
  }
}
