"""What the code may not do (SPEC REQ-SCOPE-1, REQ-SCOPE-2)."""

import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "telegrind"
FORBIDDEN = {"anthropic", "openai", "sqlalchemy", "asyncpg", "psycopg", "psycopg2", "alembic", "pymysql", "aiosqlite"}
SOURCES = sorted(PKG.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _docstrings(tree: ast.Module) -> set[int]:
    """ids of the Constant nodes that are docstrings: prose, not code."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _strings(path: Path) -> list[str]:
    """Every string literal of the module that is not a docstring, f-string pieces included."""
    tree = _tree(path)
    docs = _docstrings(tree)
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    """Calls whose callee ends in `name` (`f(...)`, `x.f(...)`, `x.y.f(...)`)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if callee == name:
                out.append(node)
    return out


def test_no_model_sdk_and_no_other_database_driver():
    """REQ-SCOPE-1: sqlite3 is the only driver; the agent is reached through televibe only."""
    for path in SOURCES:
        assert not (_imports(path) & FORBIDDEN), path


def test_facts_sqlite_is_opened_read_only_only():
    """REQ-SCOPE-2: the path is built in layout.py alone, every use of it goes to Facts, and Facts opens it mode=ro.

    Prose that names the file (a docstring, the doctor's «facts.sqlite present» label) is not a path; a string
    literal that is the file name or ends in /facts.sqlite is."""
    named = [p.name for p in SOURCES if any(s == "facts.sqlite" or s.endswith("/facts.sqlite") for s in _strings(p))]
    assert named == ["layout.py"], f"facts.sqlite is built as a path outside layout.py: {named}"
    for path in SOURCES:
        if path.name == "layout.py":
            continue
        tree = _tree(path)
        for call in _calls(tree, "facts_path"):
            users = [c for c in _calls(tree, "Facts") if c.args and c.args[0] is call]
            assert users, f"{path.name}:{call.lineno}: facts_path() handed to something other than Facts"
    connecting = sorted(p.name for p in SOURCES if _calls(_tree(p), "connect"))
    assert connecting == ["facts.py", "log.py"], connecting
    facts = (PKG / "facts.py").read_text()
    assert facts.count("sqlite3.connect") == 1
    assert re.search(r"sqlite3\.connect\(f\"file:\{self\.path\}\?mode=ro\", uri=True", facts)
    tables = set(re.findall(r"\bFROM\s+(\w+)", facts))
    assert tables == {"processed", "keyterms", "settings", "reminders"}, tables
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b", facts)
