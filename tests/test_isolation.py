"""Regression: tests must never write into the repository's real data/ directory."""

from finresearch.config import REPO_ROOT, get_settings


def test_settings_point_at_tmp_dirs_during_tests():
    s = get_settings()
    real = (REPO_ROOT / "data").resolve()
    for d in (s.runs_dir, s.state_dir, s.docs_dir):
        assert real not in d.resolve().parents and d.resolve() != real, f"{d} is inside the real data/ folder"
