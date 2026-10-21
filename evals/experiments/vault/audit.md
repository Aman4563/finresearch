# Vault data audit (study `vault_data_audit`; P1.3 acceptance)

Part of the vault pack (PREREG.md, #364). The only repeatable study: each read is logged with a running number.
Outputs are aggregates only (counts, shares, correlations, residual summaries); no strategy P&L, no return series,
no per-day figure is printed, stored outside the vault, or shown to a model.

Checks (from #365; a study that depends on a failed check waits):

1. Expiry-day futures settle vs index close within 0.05 % wherever ind_close_all exists (2013 on).
2. 2008-2012: the expiring options' uniform last-session settle equals the expiring future's final settle on every
   monthly expiry, after confirming on 20 sampled legacy expiries (2008-2021, seed 365) that the uniform-settle
   property holds before 2022.
3. Rows kept per day reconcile with the file's rows (manifest rows_kept + rows_dead vs rows_total).
4. IV30 non-null on >= 95 % of NIFTY and BANKNIFTY days in 2018-2021.
5. Correlation of daily changes in derived IV30 and India VIX >= 0.9, from the first ind_close_all file carrying
   India VIX.
6. The dated lot table matches NSE circulars on 20 random contracts (seed 365).
7. Put-call parity residuals reported by era (no threshold).
8. Coverage: the moneyness range covered on >= 90 % of days per era (fixes `vrp.md`'s common range), the first
   year BANKNIFTY has >= 10 traded OTM strikes per side on >= 90 % of days, and the first weekly expiry per index.
