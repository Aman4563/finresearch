# Addendum to the stock factor diagnostics' registration (#506)

Post hoc notes. `PREREG.md` and `factordiag/specs.py` are never edited; a change to a registered value would be a new
`SPEC_VERSION` recorded here with its hash committed before the changed study reads a price. Nothing below changes
the registration, its hash or any registered procedure.

## Note 1 (23-Nov-2026, after the registration was pushed, before any price was read): the relabelling figures

The registration quotes how often the source's stock-relabelling null, which is **not used**, rejects a true null on
the synthetic panels: 21 to 25 % for the score tied to beta, from one run of 400 panels (seed 507). A larger run of
600 panels on another stream (seed 41), written while adding the committed test, gives **16 to 17 % at one month and
20 to 22 % at twelve months**. The registered one-month figure was on the high side of its sampling error (the
binomial standard deviation at 400 panels is 2 points). The conclusion is unchanged: relabelling rejects a true null
three to four times too often on these panels, against 3 to 6 % for the registered sign-flip, which is why it is not
used. The committed test (`tests/test_factordiag_synthetic.py`) asserts only that: more than 10 % and more than twice
the sign-flip's rate, at one and at twelve months.

The figures of the registered procedures themselves (family A, the block interval, Newey-West, family B) are
reproduced exactly, digit for digit, by `uv run python -m finresearch.factordiag.synth 2000 506`.

## Note 2 (23-Nov-2026, before any price was read): how two registered readings are applied

Written while the reading rules were being coded, with no result in hand. Neither changes a registered value.

- **"Short-lived" names no sign.** The registered condition is "detected at 1 or 3 months and not at 12, with the
  (6, 12] segment's interval including 0". The code applies it as written, whatever the sign of the detected IC. For
  a component detected with a negative IC it therefore appears beside "ranks the wrong way": read together, a
  wrong-way ranking seen only at short horizons. "Not at 12" means the 12-month cell is known and not detected; an
  unknown 12-month cell, or an unknown (6, 12] interval, gives no "short-lived" note.
- **Order of the checks for a cell detected with a positive IC**: the long-only spread's interval first (unknown: no
  reading; not wholly above 0, including an interval that touches 0: "not captured"), then the long-only break-even
  cost (unknown: no reading; below the harness's cost per side: "smaller than its trading cost"; equal or above:
  "disagrees"). A q exactly equal to 0.10 counts as detected ("at most").
