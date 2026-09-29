// Copies pdf.js's runtime assets next to the app (public/pdfjs/), so the in-app PDF viewer loads them from the
// dashboard's own origin in dev and production alike: the worker, the wasm image decoders (scanned CCITT/JBIG2 pages
// and JPEG 2000 images render blank without them), CMaps and standard fonts (text in PDFs that don't embed fonts) and
// ICC profiles. Runs before `dev` and `build`; the copy is gitignored.
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const pkg = dirname(require.resolve("pdfjs-dist/package.json"));
const out = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "pdfjs");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
cpSync(join(pkg, "build", "pdf.worker.min.mjs"), join(out, "pdf.worker.min.mjs"));
for (const dir of ["wasm", "cmaps", "standard_fonts", "iccs"]) cpSync(join(pkg, dir), join(out, dir), { recursive: true });
console.log(`pdf.js assets -> ${out}`);
