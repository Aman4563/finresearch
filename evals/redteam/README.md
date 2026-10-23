# Red-team reviews (#379)

Every adoption decision for a model in the inventory (`src/finresearch/inventory/`) gets a red-team review by a
separate Claude session that is given only the model's PREREG and its code: no results, no discussion. This is the
substitute for an independent model-validation function (SR 11-7 "effective challenge"). A tier-A model cannot read
"validated" without at least one review filed here.

## Filing one

```bash
uv run finresearch models redteam-new <entry key> --prereg evals/experiments/<x>/PREREG.md --slug <short-name>
```

This writes `evals/redteam/<entry key>/<YYYY-MM-DD>-<slug>.md` (a recipe `recipe:x` goes in `recipe-x/`) with the
PREREG's last commit and its committer timestamp, the external timestamp the review is anchored to. Give the separate
session only the files listed under "Given only", then fill in `reviewer`, `verdict` (`adopt`, `do-not-adopt` or
`changes-needed`) and the findings. The file is invalid while `verdict: TODO`, so CI stays red until it is finished
(`tests/test_model_redteam.py`). Reviews are append-only: a later review is a new file, never an edit of an old one.

The models page and `GET /api/models` list each entry's filed reviews.
