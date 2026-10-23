// Models page (#379): every inventory entry with its status badge (validated / unvalidated / failed) and the reason,
// its limits of use, its evidence age (unknown when there is none, never 0 days), its filed red-team reviews and the
// latest monthly re-validation by regime split. Synthetic fixture only.
import { type Page, expect, test } from "@playwright/test";

import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const today = new Date().toISOString().slice(0, 10);
const LIMITS = "Personal research only: no distribution and no automated orders (nothing here places an order).";
const base = {
  kind: "rule", required_validation: "x", intended_use: "Manual use.", restrictions: "Analysis only.",
  limits_of_use: LIMITS, inputs: ["option chain"], code: ["signals/fno.py"], last_update: "2026-10-20",
  outcome_metric: "Brier score", validated_on: null, valid_until: null, evidence: "", regime_tags: [], methods: [],
  registrations: [], red_team: [], evidence_as_of: null, evidence_age_days: null, evidence_stale: null, revalidation: null,
};
const MODELS = {
  as_of: today, limits_of_use: LIMITS, max_evidence_age_days: 45,
  tiers: { A: "pre-registered test + red team", B: "calibration", C: "reasonableness" },
  regime_changes: [{ day: "2025-09-01", tag: "expiry_weekday", what: "NIFTY's weekly expiry moved to Tuesday", source: "s" }],
  revalidation: { after: "22:30", every: "month", splits: ["before_2024-11-20", "2024-11-20_to_2025-08-31", "from_2025-09-01"] },
  redteam_invalid: [],
  entries: [
    { ...base, key: "example_gate", name: "Example strategy gate", tier: "A", purpose: "Screen a trade.",
      status: "unvalidated", status_reason: "tier A: no red-team review of its PREREG and code is filed (evals/redteam/)" },
    { ...base, key: "recipe:example_ic", name: "Example iron condor", kind: "recipe", tier: "A", purpose: "A recipe.",
      status: "failed", status_reason: "failed its test on 12-Oct-2026: nothing passed", evidence_as_of: "2026-08-01",
      evidence_age_days: 83, evidence_stale: ["the backtest run is 83 days old (evidence expires after 45 days): re-run it"],
      revalidation: { slot: "revalidate:2026-10", created_at: "2026-10-01T22:40:00+05:30", code_hash: "c", regime_tags: {},
        splits: { "from_2025-09-01": { from: "2025-09-01", to: null, status: "computed", reason: null,
          metrics: { trades: 40, expectancy: -12.5, t_stat: -1.2, ci_low: -30, ci_high: 4 } },
          "before_2024-11-20": { from: null, to: "2024-11-19", status: "no_data", reason: "no trade was entered in this split", metrics: null } } },
      red_team: [{ model: "recipe:example_ic", reviewed_on: "2026-10-20", reviewer: "separate session", prereg: "evals/x/PREREG.md",
        prereg_commit: "0123456789ab", prereg_committed_at: "2026-10-12T10:00:00+05:30", verdict: "do-not-adopt",
        path: "evals/redteam/recipe-example_ic/2026-10-20-a.md" }] },
    { ...base, key: "example_blend", name: "Example calibrated blend", kind: "signal", tier: "B", purpose: "A probability.",
      status: "validated", status_reason: "validated on 01-Oct-2026, valid until 01-Jan-2027", validated_on: "2026-10-01",
      valid_until: "2027-01-01", evidence_as_of: "2026-10-01", evidence_age_days: 22 },
  ],
};

test("models: every entry shows its status badge, reason, limits of use and evidence age (#379)", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/models": MODELS });
  await page.goto("/models");
  await expect(page.getByRole("heading", { name: "Model inventory" })).toBeVisible();
  await expect(page.getByText(LIMITS).first()).toBeVisible();
  const gate = page.getByRole("article", { name: "Example strategy gate" });
  await expect(gate.getByText("unvalidated", { exact: true })).toBeVisible();
  await expect(gate.getByText(/no red-team review/)).toBeVisible();
  await expect(gate.getByText("Evidence age: unknown")).toBeVisible(); // no evidence is unknown, never 0 days
  const ic = page.getByRole("article", { name: "Example iron condor" });
  await expect(ic.getByText("failed", { exact: true })).toBeVisible();
  await expect(ic.getByText("Evidence age: 83 days (2026-08-01)")).toBeVisible();
  await expect(ic.getByText(/83 days old/)).toBeVisible();
  await expect(ic.getByText(/do-not-adopt/)).toBeVisible();
  const rv = ic.getByRole("region", { name: "Re-validation by regime split" });
  await expect(rv.getByRole("row", { name: /from_2025-09-01 40 -12.5/ })).toBeVisible();
  await expect(rv.getByRole("row", { name: /before_2024-11-20 no trade was entered/ })).toBeVisible();
  await expect(page.getByRole("article", { name: "Example calibrated blend" }).getByText("validated", { exact: true })).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("models: a status filter narrows the list (#379)", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/models": MODELS });
  await page.goto("/models");
  await page.getByRole("tab", { name: /^failed/ }).click();
  await expect(page.getByRole("article")).toHaveCount(1);
  await expect(page.getByRole("article", { name: "Example iron condor" })).toBeVisible();
  api.expectClean();
});
