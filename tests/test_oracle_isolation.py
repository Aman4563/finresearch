"""The oracle libraries stay dev-only (#503): QuantLib, arch, scipy, statsmodels, mpmath and everything they pull in
are compared with the in-house maths in `oracle_tests/`, and nothing the app or the normal suite runs may import
them. Static checks on the source tree, pyproject.toml, uv.lock and the CI workflow; no library is needed to run them.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
GROUP = "oracle"
MERGE_GATES = {"test", "web", "secrets"}  # the jobs data/devtools/merge_deploy.sh waits on

# Every distribution that only the oracle group installs (uv.lock name -> the module it is imported as). The set is
# checked against uv.lock below, so adding a library to the group without naming its import here fails.
IMPORT_NAMES = {
    "quantlib": "QuantLib",
    "arch": "arch",
    "scipy": "scipy",
    "statsmodels": "statsmodels",
    "mpmath": "mpmath",
    "pandas": "pandas",  # arch, statsmodels
    "patsy": "patsy",  # statsmodels
    "formulaic": "formulaic",  # statsmodels
    "interface-meta": "interface_meta",  # formulaic
    "narwhals": "narwhals",  # formulaic
    "wrapt": "wrapt",  # formulaic
}


def _lock() -> tuple[dict[str, dict], dict]:
    packages = {p["name"]: p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    return packages, packages["finresearch"]


def _closure(packages: dict[str, dict], roots: list[dict]) -> set[str]:
    """Every distribution `roots` (uv.lock dependency entries) install, following extras; markers are ignored, so
    the result is every platform's union."""
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        dep = todo.pop()
        pkg = packages[dep["name"]]
        extras = [x for e in dep.get("extra", []) for x in pkg.get("optional-dependencies", {}).get(e, [])]
        new = dep["name"] not in seen
        seen.add(dep["name"])
        todo += extras + (pkg.get("dependencies", []) if new else [])
    return seen


def _oracle_only() -> set[str]:
    packages, project = _lock()
    groups = project["dev-dependencies"]
    default = _closure(packages, project["dependencies"] + groups["dev"])
    return _closure(packages, groups[GROUP]) - default


def _imports(path: Path) -> set[str]:
    """Top-level names of every absolute import in a file, including importlib.import_module("x") and
    __import__("x") with a literal name."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("import_module", "__import__", "importorskip") and isinstance(
                node.args[0].value, str
            ):
                found.add(node.args[0].value.split(".")[0])
    return found


def _offenders(folder: str) -> list[str]:
    banned = {m.lower() for m in IMPORT_NAMES.values()}
    return sorted(
        f"{f.relative_to(ROOT)}: {name}"
        for f in (ROOT / folder).rglob("*.py")
        for name in _imports(f)
        if name.lower() in banned
    )


def test_the_import_map_names_exactly_what_only_the_oracle_group_installs():
    assert _oracle_only() == set(IMPORT_NAMES)


def test_nothing_under_src_imports_an_oracle_library():
    assert len(list((ROOT / "src").rglob("*.py"))) > 300  # the scan really walked the package
    assert _offenders("src") == []


def test_nothing_the_default_environment_runs_imports_an_oracle_library():
    """The normal suite, the scripts and the migrations run on `uv sync --locked`, which has none of them."""
    assert [x for folder in ("tests", "scripts", "migrations", "evals") for x in _offenders(folder)] == []


def test_the_scan_sees_each_way_of_importing(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(
        "import scipy.stats as st\nfrom arch.bootstrap import SPA\nimport importlib\n"
        "ql = importlib.import_module('QuantLib')\nmp = __import__('mpmath')\nfrom . import pandas\n"
        "from .statsmodels import x\nimport numpy\n"
    )
    assert _imports(f) == {"scipy", "arch", "importlib", "QuantLib", "mpmath", "numpy"}


def test_the_oracle_group_is_opt_in_and_no_runtime_dependency():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    groups = cfg["dependency-groups"]
    assert GROUP in groups and GROUP not in cfg["tool"]["uv"]["default-groups"]
    assert all("==" in spec for spec in groups[GROUP])  # exact pins: a comparison names its oracle's version

    def names(specs: list[str]) -> set[str]:
        return {re.split(r"[\[<>=!~ ;]", s, maxsplit=1)[0].lower().replace("_", "-") for s in specs}

    default = names(cfg["project"]["dependencies"]) | names(groups["dev"])
    assert not default & set(IMPORT_NAMES)
    assert names(groups[GROUP]) <= set(IMPORT_NAMES)
    # the default pytest run collects tests/ only, so the oracle folder never changes its pass and skip counts
    assert cfg["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert (ROOT / "oracle_tests" / "conftest.py").is_file()


def test_ci_runs_the_oracle_suite_in_its_own_job_that_gates_no_merge():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    body = text.split("\njobs:\n", 1)[1]
    names = re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", body, flags=re.M)
    assert set(names) >= MERGE_GATES
    blocks = dict(zip(names, re.split(r"^  [A-Za-z0-9_-]+:\s*$", body, flags=re.M)[1:], strict=True))
    mine = [n for n, b in blocks.items() if "oracle_tests" in b]
    assert len(mine) == 1 and mine[0] not in MERGE_GATES
    assert f"--group {GROUP}" in blocks[mine[0]]
    for gate in MERGE_GATES:  # the merge gates install and run nothing of it
        assert GROUP not in blocks[gate] and "--all-groups" not in blocks[gate]
