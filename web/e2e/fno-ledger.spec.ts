// The F&O card ledger (#376): the registered scores before and after registration, unknown statistics read "unknown"
// (never 0), no calibration claim below 100 independent events, the override log and the fixed panel. Made-up numbers.
import { type Page, expect, test } from "@playwright/test";

import type { CardLedger, LedgerSection } from "../src/components/fno/ledger-types";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

const EMPTY: LedgerSection = {
  events: 0, independent: 0, bss: { n: 0, bss: null, lo: null, hi: null, se: null, block: null, reps: 2000 },
  cohort: { delta: null, se: null, t: null, p: null, ci: null, clusters: 0, n_pass: 0, n_fail: 0, unknown_class: 0, mean_pass: null, mean_fail: null },
  models: [
    { model: "p_rw", label: "real-world PoP (the card's headline)", n: 0, brier: null, log: null, mean_p: null, base_rate: null, reliability: [], pit: { n: 0, bins: [], clipped: 0, no_dots: 0 }, p10: { n: 0, below: 0, share: null, expected: 0.1, p: null } },
  ],
  claims: [],
};
const AFTER: LedgerSection = {
  events: 4, independent: 3, bss: { n: 3, bss: 0.3467, lo: -0.2, hi: 0.8, se: 0.25, block: 2, reps: 2000 },
  cohort: { delta: 200, se: 90, t: 2.2, p: 0.16, ci: [-180, 580], clusters: 3, n_pass: 2, n_fail: 2, unknown_class: 1, mean_pass: 200, mean_fail: 0 },
  models: [
    { model: "p_rw", label: "real-world PoP (the card's headline)", n: 3, brier: 0.1633, log: 0.52, mean_p: 0.57, base_rate: 0.33,
      reliability: [], pit: { n: 4, bins: [2, 1, 0, 0, 0, 1, 0, 0, 0, 0], clipped: 1, no_dots: 0 }, p10: { n: 4, below: 2, share: 0.5, expected: 0.1, p: 0.0523 } },
    { model: "p_rn_atm", label: "risk-neutral PoP (benchmark)", n: 3, brier: 0.25, log: 0.69, mean_p: 0.5, base_rate: 0.33,
      reliability: [], pit: { n: 0, bins: [], clipped: 0, no_dots: 3 }, p10: { n: 0, below: 0, share: null, expected: 0.1, p: null } },
  ],
  claims: [{ underlying: "NIFTY", n_independent: 3, claim_allowed: false }],
};
const LEDGER: CardLedger = {
  prereg: "evals/experiments/card_ledger/PREREG.md", addendum: "evals/experiments/card_ledger/ADDENDUM.md", claim_min: 100,
  registered: "2026-10-22", cards: { open: 2, resolved: 4, void: 1, no_probability: 0 },
  before: EMPTY, after: AFTER, pooled: AFTER,
  overrides: {
    decisions: 2, overrides: 1, unknown_class: 0, rate: 0.5, rows: 2,
    pnl: {
      traded_against: { decisions: 1, resolved: 1, mean_pnl: -250, open: 0, void: 0, no_forecast: 0, clusters: 1 },
      traded_with: { decisions: 1, resolved: 0, mean_pnl: null, open: 1, void: 0, no_forecast: 0, clusters: 0 },
      skipped_enter: { decisions: 0, resolved: 0, mean_pnl: null, open: 0, void: 0, no_forecast: 0, clusters: 0 },
      skipped_other: { decisions: 0, resolved: 0, mean_pnl: null, open: 0, void: 0, no_forecast: 0, clusters: 0 },
    },
    recent: [{ id: 2, created_at: null, card_log_id: 41, card_index: 0, decision: "traded", verdict: "WAIT", override: true, note: null }],
  },
  panel: {
    start: "2026-10-23", rows: 14, sessions: 1, first_session: "2026-10-23", last_session: "2026-10-23", claim_min: 100,
    groups: [{ underlying: "NIFTY", expiry_kind: "near", event: "put25", model: "rn_atm", n: 0, brier: null, log: null, mean_p: null, base_rate: null, claim_allowed: false, events: 1, open: 1, void: 0, unknown_p: 0 }],
    pit: [],
  },
};

test("fno card ledger: scores split by registration, unknowns read unknown, no claim below 100 events", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/card-ledger": LEDGER });
  await page.goto("/fno/ledger");
  await expect(page.getByRole("heading", { name: "F&O card ledger" })).toBeVisible();
  await expect(page.getByText(/NIFTY: 3 of 100 independent events · no calibration claim yet/)).toBeVisible();
  await expect(page.getByText("0.347")).toBeVisible();
  await expect(page.getByText(/t 2.20 · p 0.160 · 3 expiries/)).toBeVisible();
  await expect(page.getByText("unknown (no dots)")).toBeVisible();
  await expect(page.getByText(/1 of 2 recorded decisions went against the card · rate 50%/)).toBeVisible();
  await expect(page.getByText(/Traded with an ENTER card: unknown per set/)).toBeVisible();
  await expect(page.getByText(/1 sessions logged, 2026-10-23 to 2026-10-23/)).toBeVisible();
  await page.getByRole("tab", { name: "Before registration" }).click();
  await expect(page.getByText(/No resolved card yet: nothing is scored/)).toBeVisible();
  await expect(page.getByText(/^unknown$/).first()).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});
