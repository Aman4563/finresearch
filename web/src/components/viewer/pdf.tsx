"use client";

/* In-app PDF viewer on pdf.js: renders pages itself (so it works whatever the browser's own PDF settings are), only the
   pages near the viewport (reports are ~60 pages, source documents 400+ and 10+ MB), with a selectable text layer and
   find-in-document. Big files load by byte ranges, so the first page shows before the whole file has arrived. */

import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Loader2, Minus, PanelLeft, Plus, Search, X } from "lucide-react";
import type { PDFDocumentProxy, PDFPageProxy, RenderTask, TextLayer } from "pdfjs-dist";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { Button, Progress, Skeleton, cx } from "@/components/ui";
import { API_URL } from "@/lib/api";

import "./viewer.css";

type Pdfjs = typeof import("pdfjs-dist");
let pdfjsPromise: Promise<Pdfjs> | null = null;
const ASSETS = "/pdfjs"; // copied from pdfjs-dist by scripts/copy-pdfjs-assets.mjs

/** pdf.js is browser-only (it touches DOMMatrix at import), so it loads on demand; its worker is in public/pdfjs/. */
function loadPdfjs(): Promise<Pdfjs> {
  pdfjsPromise ??= import("pdfjs-dist")
    .then((m) => {
      m.GlobalWorkerOptions.workerSrc = `${ASSETS}/pdf.worker.min.mjs?v=${m.version}`;
      return m;
    })
    .catch((e) => {
      pdfjsPromise = null;
      throw e;
    });
  return pdfjsPromise;
}

const CSS_UNITS = 96 / 72; // 100% = the page at its printed size on a 96 dpi screen
const GAP = 14;
const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];
const MAX_CANVAS_PIXELS = 8_388_608; // per page canvas: sharp on retina, far below browser canvas limits
const THUMB_W = 92;

type Size = [number, number]; // page width, height at 100% (CSS px)
type Zoom = "fit" | number;
type Match = { page: number; start: number; end: number };
type Layer = { divs: HTMLElement[]; strs: string[] };
/** A page's text for search: lower-cased, whitespace collapsed, and each char's origin (text item, offset in it). */
type PageText = { norm: string; item: Int32Array; offset: Int32Array };

const fmtBytes = (n: number) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const normQuery = (q: string) => q.toLowerCase().replace(/\s+/g, " ").trim();

function buildText(items: { str: string; hasEOL?: boolean }[]): PageText {
  const out: string[] = [];
  const item: number[] = [];
  const offset: number[] = [];
  let space = true;
  items.forEach((it, i) => {
    const s = it.str;
    for (let j = 0; j < s.length; j++) {
      const c = s[j];
      if (c === "­") continue; // soft hyphen
      if (/\s/.test(c)) {
        if (space) continue;
        space = true;
        out.push(" ");
        item.push(i);
        offset.push(j);
        continue;
      }
      space = false;
      for (const l of c.toLowerCase()) {
        out.push(l);
        item.push(i);
        offset.push(j);
      }
    }
    if (it.hasEOL && !space) {
      space = true;
      out.push(" ");
      item.push(i);
      offset.push(s.length); // past the item's text: never highlighted
    }
  });
  return { norm: out.join(""), item: Int32Array.from(item), offset: Int32Array.from(offset) };
}

/** Wrap the matched parts of the page's text spans in highlight spans; returns the selected match's element. */
function paint(layer: Layer, text: PageText | undefined, ranges: { start: number; end: number; sel: boolean }[]): HTMLElement | null {
  layer.divs.forEach((d, i) => {
    if (d.dataset.hl) {
      d.textContent = layer.strs[i];
      delete d.dataset.hl;
    }
  });
  if (!text || !ranges.length) return null;
  const per = new Map<number, { a: number; b: number; sel: boolean }[]>();
  const push = (i: number, a: number, b: number, sel: boolean) => {
    const list = per.get(i) ?? [];
    list.push({ a, b, sel });
    per.set(i, list);
  };
  for (const r of ranges) {
    let cur = -1;
    let a = 0;
    let b = 0;
    for (let k = r.start; k < r.end && k < text.norm.length; k++) {
      const it = text.item[k];
      const off = text.offset[k];
      if (off >= (layer.strs[it]?.length ?? 0)) continue;
      if (it !== cur) {
        if (cur >= 0) push(cur, a, b, r.sel);
        cur = it;
        a = off;
      }
      b = off + 1;
    }
    if (cur >= 0) push(cur, a, b, r.sel);
  }
  let selected: HTMLElement | null = null;
  per.forEach((segs, i) => {
    const d = layer.divs[i];
    const s = layer.strs[i];
    if (!d || s == null) return;
    segs.sort((x, y) => x.a - y.a);
    const frag = document.createDocumentFragment();
    let pos = 0;
    for (const g of segs) {
      if (g.a < pos) continue;
      if (g.a > pos) frag.append(s.slice(pos, g.a));
      const span = document.createElement("span");
      span.className = g.sel ? "hl sel" : "hl";
      span.textContent = s.slice(g.a, g.b);
      frag.append(span);
      if (g.sel && !selected) selected = span;
      pos = g.b;
    }
    if (pos < s.length) frag.append(s.slice(pos));
    d.replaceChildren(frag);
    d.dataset.hl = "1";
  });
  return selected;
}

export function describePdfError(e: unknown): string {
  const err = (e ?? {}) as { name?: string; message?: string; status?: number };
  const msg = err.message ?? String(e);
  if (err.name === "ResponseException" && err.status === 404) return "The file was not found (404). The run's pack may have been rebuilt or the document removed.";
  if (err.name === "ResponseException" && err.status) return `The API answered ${err.status} for this file.`;
  if (err.name === "InvalidPDFException") return "This file is not a valid PDF (it may be damaged or still being written).";
  if (err.name === "PasswordException") return "This PDF is password-protected. Download it and open it in your PDF app.";
  if (/worker/i.test(msg)) return `The PDF engine failed to start (${msg}). Rebuild the dashboard (pnpm build) and reload.`;
  if (/fetch|network|load failed/i.test(msg)) return `Could not reach the FinResearch API at ${API_URL}. Is \`uv run finresearch serve\` running?`;
  return `Could not open the PDF: ${msg}`;
}

// ------------------------------------------------------------------ loading

type Loaded = { doc: PDFDocumentProxy; first: Size };

function usePdfDocument(url: string, attempt: number) {
  const [state, setState] = useState<{ url: string; attempt: number; loaded?: Loaded; error?: string; bytes?: [number, number] }>({ url, attempt });
  useEffect(() => {
    let dead = false;
    let task: ReturnType<Pdfjs["getDocument"]> | null = null;
    loadPdfjs()
      .then(async (pdfjs) => {
        if (dead) return;
        task = pdfjs.getDocument({
          url,
          withCredentials: true, // the API token cookie
          rangeChunkSize: 1 << 18,
          disableAutoFetch: true, // fetch only the byte ranges the visible pages need
          disableStream: true,
          enableXfa: false,
          wasmUrl: `${ASSETS}/wasm/`,
          cMapUrl: `${ASSETS}/cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${ASSETS}/standard_fonts/`,
          iccUrl: `${ASSETS}/iccs/`,
        });
        task.onProgress = ({ loaded, total }: { loaded: number; total: number }) => {
          if (!dead) setState((s) => ({ ...s, bytes: [loaded, total || 0] }));
        };
        const doc = await task.promise;
        const p1 = await doc.getPage(1);
        const vp = p1.getViewport({ scale: CSS_UNITS });
        if (!dead) setState({ url, attempt, loaded: { doc, first: [vp.width, vp.height] } });
      })
      .catch((e) => {
        if (!dead) setState({ url, attempt, error: describePdfError(e) });
      });
    return () => {
      dead = true;
      void task?.destroy();
    };
  }, [url, attempt]);
  // a new url / retry starts from scratch (state from the previous one is ignored until its effect reports)
  return state.url === url && state.attempt === attempt ? state : { url, attempt };
}

// ------------------------------------------------------------------ one page

const PageView = memo(function PageView({ doc, n, zoom, top, left, w, h, onSize, onLayer }: {
  doc: PDFDocumentProxy; n: number; zoom: number; top: number; left: number; w: number; h: number;
  onSize: (n: number, s: Size) => void; onLayer: (n: number, l: Layer | null) => void;
}) {
  const canvasHost = useRef<HTMLDivElement>(null);
  const textHost = useRef<HTMLDivElement>(null);
  const pageRef = useRef<PDFPageProxy | null>(null);
  const [drawn, setDrawn] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    let dead = false;
    let task: RenderTask | null = null;
    let tl: TextLayer | null = null;
    (async () => {
      const pdfjs = await loadPdfjs();
      const page = await doc.getPage(n);
      if (dead) return;
      pageRef.current = page;
      const base = page.getViewport({ scale: CSS_UNITS });
      onSize(n, [base.width, base.height]);
      const viewport = page.getViewport({ scale: zoom * CSS_UNITS });
      const out = Math.min(window.devicePixelRatio || 1, Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height)));
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width * out);
      canvas.height = Math.floor(viewport.height * out);
      canvas.setAttribute("aria-hidden", "true");
      task = page.render({ canvas, viewport, transform: out !== 1 ? [out, 0, 0, out, 0, 0] : undefined });
      await task.promise;
      if (dead) return;
      canvasHost.current?.replaceChildren(canvas); // swap only when done: a zoom keeps the old (stretched) page meanwhile
      setDrawn(true);
      const div = textHost.current;
      if (!div) return;
      div.replaceChildren();
      div.style.setProperty("--total-scale-factor", String(viewport.scale));
      tl = new pdfjs.TextLayer({ textContentSource: page.streamTextContent({ includeMarkedContent: true }), container: div, viewport });
      await tl.render();
      if (!dead) onLayer(n, { divs: tl.textDivs, strs: tl.textContentItemsStr });
    })().catch((e: { name?: string; message?: string }) => {
      if (!dead && e?.name !== "RenderingCancelledException") setFailed(e?.message ?? "render failed");
    });
    return () => {
      dead = true;
      task?.cancel();
      tl?.cancel();
      onLayer(n, null);
    };
  }, [doc, n, zoom, onSize, onLayer]);

  // leaving the viewport window unmounts the page: let pdf.js drop its cached drawing data too
  useEffect(() => () => void pageRef.current?.cleanup(), []);

  return (
    <div className="pdf-page" style={{ top, left, width: w, height: h }} data-page={n} aria-label={`Page ${n}`} role="region">
      {!drawn && !failed && <div className="skeleton absolute inset-0 !rounded-none opacity-60" />}
      {failed && <p className="absolute inset-x-0 top-1/3 px-4 text-center text-xs text-loss">Page {n} could not be drawn: {failed}</p>}
      <div ref={canvasHost} className="absolute inset-0" />
      <div ref={textHost} className="textLayer" />
    </div>
  );
});

const Thumb = memo(function Thumb({ doc, n, w, h }: { doc: PDFDocumentProxy; n: number; w: number; h: number }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let dead = false;
    let task: RenderTask | null = null;
    (async () => {
      const page = await doc.getPage(n);
      if (dead) return;
      const vp = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: (w / vp.width) * Math.min(2, window.devicePixelRatio || 1) });
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      canvas.style.cssText = "width:100%;height:100%;display:block";
      task = page.render({ canvas, viewport });
      await task.promise;
      if (!dead) host.current?.replaceChildren(canvas);
    })().catch(() => {});
    return () => {
      dead = true;
      task?.cancel();
    };
  }, [doc, n, w]);
  return <div ref={host} className="skeleton overflow-hidden !rounded-sm bg-white" style={{ width: w, height: h }} />;
});

// ------------------------------------------------------------------ layout helpers

function stack(sizes: Size[], zoomOf: (i: number) => number, pad: number, gap: number) {
  const n = sizes.length;
  const [ws, hs, tops, zs] = [new Array<number>(n), new Array<number>(n), new Array<number>(n), new Array<number>(n)];
  let y = pad;
  let maxW = 0;
  sizes.forEach(([sw, sh], i) => {
    zs[i] = zoomOf(i);
    ws[i] = Math.floor(sw * zs[i]);
    hs[i] = Math.floor(sh * zs[i]);
    tops[i] = y;
    y += hs[i] + gap;
    maxW = Math.max(maxW, ws[i]);
  });
  return { ws, hs, tops, zs, total: y - gap + pad, maxW };
}

/** Index of the last item whose top is <= y (binary search). */
function indexAt(tops: number[], y: number) {
  let lo = 0;
  let hi = tops.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (tops[mid] <= y) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

// ------------------------------------------------------------------ viewer

export function PdfViewer({ url, initialPage = 1, name, onPages }: {
  url: string; initialPage?: number; name: string; onPages?: (n: number) => void;
}) {
  const [attempt, setAttempt] = useState(0);
  const state = usePdfDocument(url, attempt);
  const loaded = state.loaded;

  if (state.error)
    return (
      <div className="grid flex-1 place-items-center p-6">
        <div role="alert" className="max-w-md rounded-xl border border-loss/30 bg-loss-soft p-5 text-center animate-fade-in">
          <p className="font-medium text-loss">Could not show {name}</p>
          <p className="mt-1 text-sm text-foreground/85">{state.error}</p>
          <p className="mt-2 text-xs text-muted">&ldquo;Open in new tab&rdquo; and &ldquo;Download&rdquo; above still work.</p>
          <Button className="mt-4" onClick={() => setAttempt((a) => a + 1)}>Try again</Button>
        </div>
      </div>
    );
  if (!loaded) return <LoadingPdf name={name} bytes={state.bytes} />;
  return <PdfDoc key={`${url}#${attempt}`} doc={loaded.doc} first={loaded.first} initialPage={initialPage} onPages={onPages} />;
}

function LoadingPdf({ name, bytes }: { name: string; bytes?: [number, number] }) {
  const [loaded, total] = bytes ?? [0, 0];
  return (
    <div className="relative flex-1 overflow-hidden bg-background-subtle" aria-busy="true">
      <div className="mx-auto mt-4 w-[min(92%,44rem)] space-y-4">
        <Skeleton className="aspect-[1/1.414] w-full !rounded-sm" />
      </div>
      <div className="absolute inset-x-0 top-24 mx-auto w-[min(88%,22rem)] rounded-xl border border-border bg-card p-4 shadow-pop animate-scale-in">
        <p className="flex items-center gap-2 text-sm font-medium">
          <Loader2 className="size-4 animate-spin text-brand" /> Opening <span className="truncate">{name}</span>
        </p>
        <Progress className="mt-3" value={total ? loaded / total : 0.08} />
        <p className="num mt-1.5 text-[11px] text-muted">{total ? `${fmtBytes(loaded)} of ${fmtBytes(total)}` : "Contacting the API…"}</p>
      </div>
    </div>
  );
}

function PdfDoc({ doc, first, initialPage, onPages }: { doc: PDFDocumentProxy; first: Size; initialPage: number; onPages?: (n: number) => void }) {
  const numPages = doc.numPages;
  const scroller = useRef<HTMLDivElement>(null);
  const rail = useRef<HTMLDivElement>(null);
  const searchBox = useRef<HTMLInputElement>(null);

  // real page sizes, learnt as pages load; the others are assumed to be the most common size seen so far
  const [known, setKnown] = useState<Map<number, Size>>(() => new Map([[1, first]]));
  const { sizes, common } = useMemo(() => {
    const tally = new Map<string, { s: Size; n: number }>();
    known.forEach((s) => {
      const k = `${Math.round(s[0])}x${Math.round(s[1])}`;
      const t = tally.get(k) ?? { s, n: 0 };
      t.n++;
      tally.set(k, t);
    });
    let best: { s: Size; n: number } = { s: first, n: 0 };
    tally.forEach((t) => {
      if (t.n > best.n) best = t;
    });
    return { common: best.s, sizes: Array.from({ length: numPages }, (_, i) => known.get(i + 1) ?? best.s) };
  }, [known, first, numPages]);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const [scrollTop, setScrollTop] = useState(0);
  const [railTop, setRailTop] = useState(0);
  const [railH, setRailH] = useState(0);
  const [zoomMode, setZoomMode] = useState<Zoom>("fit");
  const [thumbs, setThumbs] = useState(false);
  const [pageInput, setPageInput] = useState<string | null>(null);

  useEffect(() => onPages?.(numPages), [numPages, onPages]);
  useEffect(() => {
    // thumbnails open by default on wide screens only
    // eslint-disable-next-line react-hooks/set-state-in-effect -- depends on the viewport, known only after mount
    setThumbs(window.matchMedia("(min-width: 1024px)").matches);
  }, []);

  // page sizes arrive as pages load: batch them into one update per frame
  const pendingSizes = useRef(new Map<number, Size>());
  const sizeFrame = useRef(0);
  const onSize = useCallback((n: number, s: Size) => {
    pendingSizes.current.set(n, s);
    if (sizeFrame.current) return;
    sizeFrame.current = requestAnimationFrame(() => {
      sizeFrame.current = 0;
      const upd = pendingSizes.current;
      pendingSizes.current = new Map();
      setKnown((prev) => {
        let next: Map<number, Size> | null = null;
        upd.forEach((sz, k) => {
          const o = prev.get(k);
          if (!o || Math.abs(o[0] - sz[0]) > 0.5 || Math.abs(o[1] - sz[1]) > 0.5) {
            next ??= new Map(prev);
            next.set(k, sz);
          }
        });
        return next ?? prev;
      });
    });
  }, []);
  useEffect(() => () => cancelAnimationFrame(sizeFrame.current), []);

  // measure the scroll area
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setBox({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const narrow = box.w > 0 && box.w < 640;
  const pad = narrow ? 8 : 20;
  const gap = narrow ? 10 : GAP;
  // "fit width" fits every page on its own, so a landscape table page in a portrait filing is not cut off
  const fitW = box.w ? box.w - 2 * pad : common[0];
  const layout = useMemo(
    () => stack(sizes, zoomMode === "fit" ? (i) => Math.min(4, Math.max(0.2, fitW / sizes[i][0])) : () => zoomMode, pad, gap),
    [sizes, zoomMode, fitW, pad, gap],
  );
  const innerW = Math.max(box.w, layout.maxW + 2 * pad);

  // keep the reader's place (page + fraction) across zoom, resize and page-size updates
  const anchor = useRef({ page: Math.min(Math.max(1, initialPage), numPages) - 1, frac: 0 });
  const toTop = useCallback((a: { page: number; frac: number }) => layout.tops[a.page] + a.frac * layout.hs[a.page] - pad / 2, [layout, pad]);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || !box.w) return;
    const t = Math.max(0, toTop(anchor.current));
    if (Math.abs(el.scrollTop - t) > 1) el.scrollTop = t;
    setScrollTop(el.scrollTop);
  }, [toTop, box.w]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const y = el.scrollTop + pad / 2;
    const p = indexAt(layout.tops, y);
    anchor.current = { page: p, frac: Math.min(1, Math.max(0, (y - layout.tops[p]) / Math.max(1, layout.hs[p]))) };
    setScrollTop(el.scrollTop);
  };

  const current = numPages ? indexAt(layout.tops, scrollTop + Math.min(box.h * 0.35, 240)) + 1 : 1;
  const winFrom = indexAt(layout.tops, scrollTop - box.h);
  const winTo = indexAt(layout.tops, scrollTop + box.h * 2.5);

  const goTo = useCallback((page: number) => {
    const el = scroller.current;
    const p = Math.min(Math.max(1, Math.round(page)), numPages) - 1;
    anchor.current = { page: p, frac: 0 };
    if (el) el.scrollTop = Math.max(0, toTop(anchor.current));
  }, [numPages, toTop]);

  // ---------------------------------------------------------------- search
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [matches, setMatches] = useState<Match[]>([]);
  const [searched, setSearched] = useState(0); // pages searched so far
  const [textless, setTextless] = useState(0); // scanned pages: images without a text layer, invisible to search
  const [cur, setCur] = useState(-1);
  const texts = useRef(new Map<number, PageText>());
  const layers = useRef(new Map<number, Layer>());
  const wantScroll = useRef(false);

  useEffect(() => {
    const t = setTimeout(() => setQ(normQuery(query)), 250);
    return () => clearTimeout(t);
  }, [query]);

  const pageText = useCallback(async (n: number) => {
    const hit = texts.current.get(n);
    if (hit) return hit;
    const page = await doc.getPage(n);
    const base = page.getViewport({ scale: CSS_UNITS });
    onSize(n, [base.width, base.height]);
    const tc = await page.getTextContent();
    const text = buildText(tc.items.filter((it): it is typeof it & { str: string } => "str" in it));
    texts.current.set(n, text);
    return text;
  }, [doc, onSize]);

  const startPage = useRef(1);
  useEffect(() => {
    startPage.current = current;
  }, [current]);
  useEffect(() => {
    let dead = false;
    /* eslint-disable react-hooks/set-state-in-effect -- a new query resets the results before searching */
    setMatches([]);
    setSearched(0);
    setTextless(0);
    setCur(-1);
    /* eslint-enable react-hooks/set-state-in-effect */
    if (!q) return;
    (async () => {
      const found: Match[] = [];
      let last = performance.now();
      let empty = 0;
      for (let n = 1; n <= numPages; n++) {
        const text = await pageText(n);
        if (dead) return;
        if (!text.norm.trim()) empty++;
        for (let i = text.norm.indexOf(q); i >= 0; i = text.norm.indexOf(q, i + q.length)) found.push({ page: n, start: i, end: i + q.length });
        if (n === numPages || performance.now() - last > 120) {
          last = performance.now();
          setMatches(found.slice());
          setSearched(n);
          setTextless(empty);
          // select the first match at or after the page being read, as soon as one is known
          setCur((c) => {
            if (c >= 0) return c;
            const i = found.findIndex((m) => m.page >= startPage.current);
            if (i < 0 && n < numPages) return -1;
            wantScroll.current = true;
            return i >= 0 ? i : found.length ? 0 : -1;
          });
        }
      }
    })().catch(() => {});
    return () => {
      dead = true;
    };
  }, [q, numPages, pageText]);

  const byPage = useMemo(() => {
    const m = new Map<number, { start: number; end: number; idx: number }[]>();
    matches.forEach((x, idx) => {
      const list = m.get(x.page) ?? [];
      list.push({ start: x.start, end: x.end, idx });
      m.set(x.page, list);
    });
    return m;
  }, [matches]);

  const paintRef = useRef<(n: number) => void>(() => {});
  useEffect(() => {
    paintRef.current = (n: number) => {
      const layer = layers.current.get(n);
      if (!layer) return;
      const ranges = (byPage.get(n) ?? []).map((r) => ({ start: r.start, end: r.end, sel: r.idx === cur }));
      const sel = paint(layer, texts.current.get(n), ranges);
      const el = scroller.current;
      if (sel && el && wantScroll.current) {
        wantScroll.current = false;
        const r = sel.getBoundingClientRect();
        const box = el.getBoundingClientRect();
        if (r.top < box.top + box.height * 0.12 || r.bottom > box.bottom - box.height * 0.12) el.scrollTop += r.top - box.top - box.height * 0.35;
        const dx = r.left - box.left;
        if (dx < 0 || dx > box.width - 40) el.scrollLeft += dx - box.width / 3;
      }
    };
    layers.current.forEach((_, n) => paintRef.current(n));
  }, [byPage, cur]);

  const onLayer = useCallback((n: number, l: Layer | null) => {
    if (l) {
      layers.current.set(n, l);
      paintRef.current(n);
    } else layers.current.delete(n);
  }, []);

  // moving to a match: bring its page into the window; the highlight pass then scrolls to the exact spot
  useEffect(() => {
    const m = matches[cur];
    if (!m || !wantScroll.current) return;
    const el = scroller.current;
    if (!el) return;
    const top = layout.tops[m.page - 1];
    const inView = top < el.scrollTop + el.clientHeight && top + layout.hs[m.page - 1] > el.scrollTop;
    if (layers.current.has(m.page)) paintRef.current(m.page);
    else if (!inView) goTo(m.page);
    // matches is read for the page only; re-run on a new selection, not on every progressive result
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur]);

  const step = (d: 1 | -1) => {
    if (!matches.length) return;
    wantScroll.current = true;
    setCur((c) => (c < 0 ? 0 : (c + d + matches.length) % matches.length));
  };
  const searching = !!q && searched < numPages;

  // Cmd/Ctrl+F searches the document (the browser's own find cannot see pages that aren't rendered)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        searchBox.current?.focus();
        searchBox.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // ---------------------------------------------------------------- thumbnails
  const thumbLayout = useMemo(() => {
    return stack(sizes.map(([w, h]) => [THUMB_W, (h / w) * THUMB_W] as Size), () => 1, 12, 30);
  }, [sizes]);
  useLayoutEffect(() => {
    const el = rail.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setRailH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, [thumbs]);
  useEffect(() => {
    const el = rail.current;
    if (!el || !thumbs) return;
    const t = thumbLayout.tops[current - 1];
    const h = thumbLayout.hs[current - 1] + 24;
    if (t < el.scrollTop || t + h > el.scrollTop + el.clientHeight) el.scrollTo({ top: t - el.clientHeight / 3 });
  }, [current, thumbs, thumbLayout]);
  const tFrom = indexAt(thumbLayout.tops, railTop - 200);
  const tTo = indexAt(thumbLayout.tops, railTop + (railH || 800) + 200);

  const zoom = layout.zs[current - 1] ?? 1;
  const zoomPct = Math.round(zoom * 100);
  const zoomStep = (d: 1 | -1) => {
    const next = d > 0 ? ZOOMS.find((z) => z > zoom + 0.01) : [...ZOOMS].reverse().find((z) => z < zoom - 0.01);
    setZoomMode(next ?? (d > 0 ? ZOOMS[ZOOMS.length - 1] : ZOOMS[0]));
  };

  const iconBtn = "grid size-8 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-background-subtle hover:text-foreground disabled:pointer-events-none disabled:opacity-40";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-2 border-b border-border px-2 py-2 sm:px-3">
        <button type="button" className={cx(iconBtn, thumbs && "bg-background-subtle text-foreground")} onClick={() => setThumbs((t) => !t)} aria-pressed={thumbs} aria-label="Page thumbnails" title="Page thumbnails">
          <PanelLeft className="size-4" />
        </button>
        <span className="mx-0.5 hidden h-5 w-px bg-border sm:block" aria-hidden />
        <button type="button" className={iconBtn} onClick={() => goTo(current - 1)} disabled={current <= 1} aria-label="Previous page" title="Previous page">
          <ChevronLeft className="size-4" />
        </button>
        <form
          className="flex items-center gap-1 text-xs text-muted"
          onSubmit={(e) => {
            e.preventDefault();
            const n = Number(pageInput);
            if (Number.isFinite(n) && n >= 1) goTo(n);
            setPageInput(null);
          }}
        >
          <input
            aria-label="Page number"
            inputMode="numeric"
            className="num h-8 w-12 rounded-lg border border-border bg-card px-1.5 text-center text-sm text-foreground focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20"
            value={pageInput ?? String(current)}
            onFocus={(e) => e.currentTarget.select()}
            onChange={(e) => setPageInput(e.target.value.replace(/\D/g, ""))}
            onBlur={() => setPageInput(null)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              const n = Number(pageInput);
              if (pageInput && Number.isFinite(n) && n >= 1) goTo(n);
              setPageInput(null);
            }}
          />
          <span className="num whitespace-nowrap">/ {numPages}</span>
        </form>
        <button type="button" className={iconBtn} onClick={() => goTo(current + 1)} disabled={current >= numPages} aria-label="Next page" title="Next page">
          <ChevronRight className="size-4" />
        </button>
        <span className="mx-0.5 hidden h-5 w-px bg-border sm:block" aria-hidden />
        <button type="button" className={cx(iconBtn, "hidden sm:grid")} onClick={() => zoomStep(-1)} disabled={zoom <= ZOOMS[0] + 0.001} aria-label="Zoom out" title="Zoom out">
          <Minus className="size-4" />
        </button>
        <select
          aria-label="Zoom"
          className="h-8 rounded-lg border border-border bg-card pl-2 pr-1 text-xs text-foreground focus:border-brand focus:outline-none"
          value={zoomMode === "fit" ? "fit" : String(zoomMode)}
          onChange={(e) => setZoomMode(e.target.value === "fit" ? "fit" : Number(e.target.value))}
        >
          <option value="fit">{narrow ? "Fit" : "Fit width"}{zoomMode === "fit" ? ` · ${zoomPct}%` : ""}</option>
          {ZOOMS.map((z) => (
            <option key={z} value={String(z)}>{Math.round(z * 100)}%</option>
          ))}
          {zoomMode !== "fit" && !ZOOMS.includes(zoomMode) && <option value={String(zoomMode)}>{zoomPct}%</option>}
        </select>
        <button type="button" className={cx(iconBtn, "hidden sm:grid")} onClick={() => zoomStep(1)} disabled={zoom >= ZOOMS[ZOOMS.length - 1] - 0.001} aria-label="Zoom in" title="Zoom in">
          <Plus className="size-4" />
        </button>

        {/* find in document */}
        <div className="flex min-w-0 basis-full items-center gap-1 sm:ml-auto sm:basis-auto">
          <label className="relative flex min-w-0 flex-1 items-center sm:w-64 sm:flex-none">
            <Search className="pointer-events-none absolute left-2.5 size-3.5 text-muted" />
            <input
              ref={searchBox}
              type="search"
              aria-label="Find in document"
              placeholder="Find in document"
              className="h-8 w-full rounded-lg border border-border bg-card pl-8 pr-7 text-sm text-foreground placeholder:text-muted/70 focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20 [&::-webkit-search-cancel-button]:hidden"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  if (normQuery(query) !== q) setQ(normQuery(query));
                  else step(e.shiftKey ? -1 : 1);
                } else if (e.key === "Escape") setQuery("");
              }}
            />
            {query && (
              <button type="button" className="absolute right-1.5 grid size-5 place-items-center rounded text-muted hover:text-foreground" onClick={() => setQuery("")} aria-label="Clear search">
                <X className="size-3.5" />
              </button>
            )}
          </label>
          <span
            className="num min-w-[4.5rem] whitespace-nowrap text-center text-[11px] text-muted"
            aria-live="polite"
            title={textless ? `${textless} scanned page${textless > 1 ? "s have" : " has"} no text layer, so find cannot see ${textless > 1 ? "their" : "its"} words` : undefined}
          >
            {!q ? "" : matches.length ? `${cur >= 0 ? cur + 1 : "–"} of ${matches.length}${searching ? "+" : ""}` : searching ? "" : "No matches"}
            {q && !searching && textless > 0 && (
              <span className="hidden text-[10px] text-warn sm:block" title="These pages are scanned images with no text layer, so find cannot see their words">
                {textless === numPages ? "scanned: no text" : `${textless} scanned page${textless > 1 ? "s" : ""} not searchable`}
              </span>
            )}
            {searching && (
              <span className="inline-flex items-center gap-1">
                {matches.length ? " · " : ""}
                <Loader2 className="size-3 animate-spin" /> {searched}/{numPages}
              </span>
            )}
          </span>
          <button type="button" className={iconBtn} onClick={() => step(-1)} disabled={!matches.length} aria-label="Previous match" title="Previous match (Shift+Enter)">
            <ChevronUp className="size-4" />
          </button>
          <button type="button" className={iconBtn} onClick={() => step(1)} disabled={!matches.length} aria-label="Next match" title="Next match (Enter)">
            <ChevronDown className="size-4" />
          </button>
        </div>
      </div>

      <div className="relative flex min-h-0 flex-1">
        {/* thumbnails: a column on wide screens, an overlay on phones */}
        {thumbs && (
          <div
            ref={rail}
            onScroll={(e) => setRailTop(e.currentTarget.scrollTop)}
            className={cx(
              "z-10 w-36 shrink-0 overflow-y-auto border-r border-border bg-card",
              narrow ? "absolute inset-y-0 left-0 shadow-pop animate-fade-in" : "relative",
            )}
            aria-label="Pages"
          >
            <div className="relative" style={{ height: thumbLayout.total }}>
              {Array.from({ length: Math.max(0, tTo - tFrom + 1) }, (_, k) => {
                const i = tFrom + k;
                const n = i + 1;
                const hits = byPage.get(n)?.length ?? 0;
                return (
                  <button
                    key={n}
                    type="button"
                    onClick={() => {
                      goTo(n);
                      if (narrow) setThumbs(false);
                    }}
                    className="group absolute left-1/2 flex -translate-x-1/2 flex-col items-center gap-1"
                    style={{ top: thumbLayout.tops[i] }}
                    aria-label={`Page ${n}${hits ? `, ${hits} matches` : ""}`}
                    aria-current={n === current ? "page" : undefined}
                  >
                    <span className={cx("relative rounded-sm ring-2 transition", n === current ? "ring-brand" : "ring-transparent group-hover:ring-border-strong")}>
                      <Thumb doc={doc} n={n} w={THUMB_W} h={thumbLayout.hs[i]} />
                      {hits > 0 && <span className="num absolute -right-1.5 -top-1.5 rounded-full bg-warn px-1 text-[10px] font-semibold text-white">{hits}</span>}
                    </span>
                    <span className={cx("num text-[11px]", n === current ? "font-semibold text-brand" : "text-muted")}>{n}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <div ref={scroller} onScroll={onScroll} className="relative min-w-0 flex-1 overflow-auto overscroll-contain bg-background-subtle" tabIndex={0} aria-label="Document pages">
          {box.w > 0 && (
            <div className="relative" style={{ height: layout.total, width: innerW }}>
              {Array.from({ length: Math.max(0, winTo - winFrom + 1) }, (_, k) => {
                const i = winFrom + k;
                return (
                  <PageView
                    key={i + 1}
                    doc={doc}
                    n={i + 1}
                    zoom={layout.zs[i]}
                    top={layout.tops[i]}
                    left={Math.max(pad, (innerW - layout.ws[i]) / 2)}
                    w={layout.ws[i]}
                    h={layout.hs[i]}
                    onSize={onSize}
                    onLayer={onLayer}
                  />
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
