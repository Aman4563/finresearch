# Oracle tests (#503)

All of FinResearch's finance maths is written in-house and tested against hand-computed values. The oracle suite
is a second line of defence: it compares our functions with mature outside libraries over grids of inputs, to find
the bugs and the unstated conventions that a handful of hand-worked cases cannot.

```bash
uv run --group oracle pytest oracle_tests -q                               # the whole suite (about a minute)
uv run --group oracle pytest oracle_tests -q --oracle-report=/tmp/t.md     # also write the comparison table
uv sync --locked                                                           # back to the default environment
```

## Rules

- The libraries are **dev-only**. They sit in the uv dependency group `oracle` (`pyproject.toml`), which is not a
  default group: `uv sync --locked`, `uv run pytest` and CI's `test` job never install them. Nothing under `src/`
  (or `tests/`, `scripts/`, `migrations/`, `evals/`) may import one; `tests/test_oracle_isolation.py` in the normal
  suite fails if anything does, and checks the import list against `uv.lock`.
- The oracle tests live in `oracle_tests/`, outside pytest's `testpaths`, so the normal suite's pass and skip counts
  do not depend on them. CI runs them in the job `oracle`, which no merge waits on.
- `uv run --group oracle ...` leaves the libraries in `.venv` (uv does not remove packages on `uv run`); the normal
  suite is unaffected because nothing imports them. `uv sync --locked` removes them again.
- Inputs are synthetic or hand-made. The suite reads no market data, no `data/` and no database (its `conftest.py`
  points `FINRESEARCH_DATABASE_URL` at a host that cannot exist).
- Every comparison states its tolerance and why. A difference beyond tolerance is classified, never tuned away:
  **(a)** an in-house bug: fixed in `src/` with a regression test in `tests/` whose expected value is derived
  independently, plus a `FUNCTIONAL_TESTING.md` entry; **(b)** a convention difference: written down in the oracle
  test; **(c)** an oracle limitation: written down, with a second oracle as referee where one exists.
- Not fixed in place, only reported for the owner's decision: anything in the vault's engine pin
  (`src/finresearch/backtest/*.py`, `fincalc/options.py`, `fincalc/charges.py`), a registered spec (`*/specs.py`,
  `PREREG.md`) or a golden tax expectation.
- An oracle is a witness, not an authority: where the two sides differ the test says which one is right and how
  that was decided (a third implementation, arbitrary-precision arithmetic, or the source paper).

## Libraries and licences

Read on 23-Nov-2026 from PyPI's metadata and from the LICENSE file in each project's repository. Only permissive
licences (MIT, BSD, Apache-2.0, NCSA) are allowed. Wheels for CPython 3.12 exist for macOS arm64 and Linux x86_64
for every row (checked in `uv.lock`), so nothing is compiled.

| Library | Version | Licence | Why it is here |
| --- | --- | --- | --- |
| QuantLib | 1.44 | QuantLib licence: modified BSD 3-clause (`LICENSE.TXT`; PyPI says BSD-3-Clause) | bonds, XIRR, Black-76, SVI smile section, one-touch barrier |
| arch | 9.0.0 | University of Illinois/NCSA (`LICENSE.md`; PyPI says NCSA) | GARCH and GJR fits, SPA, Reality Check, StepM, MCS, stationary bootstrap, VaR backtests |
| scipy | 1.18.1 | BSD-3-Clause (`LICENSE.txt`) | distributions, root finding, special functions |
| statsmodels | 0.15.0 | BSD-3-Clause (`LICENSE.txt`) | OLS with HAC and clustered errors |
| mpmath | 1.4.1 | BSD-3-Clause (`LICENSE`) | arbitrary-precision referee where two double-precision answers differ |

Installed with them (dependencies of arch and statsmodels, never imported by the suite directly):

| Library | Version | Licence |
| --- | --- | --- |
| pandas | 3.1.1 | BSD-3-Clause |
| patsy | 1.0.3 | BSD-2-Clause; one file under the PSF licence 2 (`LICENSE.txt`) |
| formulaic | 1.2.2 | MIT |
| interface-meta | 2.0.1 | MIT |
| narwhals | 2.29.0 | MIT |
| wrapt | 2.5.2 | BSD-2-Clause |

Never to be added (licence): FinancePy and optionlab (GPL-3.0), rateslib (source-available, not open source),
mlfinlab (proprietary). mpmath is not in issue #503's candidate list; it was added because QuantLib's own normal
distribution is only accurate to about 1e-7 in the far tail, so a comparison of deep-wing option prices needs a
referee that is exact.

To add a library: read its licence in both places, confirm the wheels, pin it exactly in the `oracle` group, run
`uv lock`, add its import name (and those of every package it pulls in) to `IMPORT_NAMES` in
`tests/test_oracle_isolation.py`, and add a row here.
