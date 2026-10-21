---
name: new-migration
description: Create an Alembic migration for model changes on a scratch database and run CI's round trip, keeping one head.
argument-hint: "\"Title (#N): tables\""
disable-model-invocation: true
---

# New migration: $ARGUMENTS

Work in your worktree, never the main checkout. Bash keeps no variables between calls, so every command below sets
`M` itself. `<you>` is a short name for this worktree.

1. Up to date first: `git fetch -q origin && git merge origin/main` (never rebase), then `uv run alembic heads` must
   print exactly one head.
2. Scratch database built by Alembic (a pytest-built one has no `alembic_version`):
   `dropdb --if-exists finresearch_<you>_mig_test; createdb finresearch_<you>_mig_test && psql -d finresearch_<you>_mig_test -c 'create extension if not exists vector'`
3. Generate:
   `M=postgresql+psycopg://localhost/finresearch_<you>_mig_test; export FINRESEARCH_DATABASE_URL=$M; uv run alembic upgrade head && uv run alembic revision --autogenerate -m "$ARGUMENTS"`
4. Read the generated file in `migrations/versions/`. Autogenerate misses or mangles: server defaults, enum changes,
   renamed columns (drop + add loses data: write `op.alter_column` by hand), pgvector types and partial indexes.
   Make `downgrade()` a true inverse.
5. `uv run ruff check --fix migrations && uv run ruff format migrations` (the template's output fails CI's ruff).
6. CI's gate on a fresh database:
   `dropdb finresearch_<you>_mig_test; createdb finresearch_<you>_mig_test && psql -d finresearch_<you>_mig_test -c 'create extension if not exists vector'`
   `M=postgresql+psycopg://localhost/finresearch_<you>_mig_test; export FINRESEARCH_DATABASE_URL=$M; uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade -1 && uv run alembic upgrade head && uv run alembic check`
7. `uv run alembic heads` prints one head. If main gained a migration meanwhile, re-point this file's
   `down_revision` to main's head and repeat step 6.
8. `dropdb finresearch_<you>_mig_test`. The live database is migrated only by `merge_deploy.sh`, after a backup.
