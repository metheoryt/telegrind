"""The live gate (SPEC REQ-TEST-4): a real agent on a fresh person's directory seeded from tests/fixtures/entries.yaml.

  TELEGRIND_LIVE_HOME=~/.claude-telegrind uv run pytest -m live -v
Optional: TELEGRIND_LIVE_TOKEN (a CLAUDE_CODE_OAUTH_TOKEN; without it the account's own login is used),
TELEGRIND_LIVE_RUNS (3), TELEGRIND_LIVE_PASS_RATE (0.67).

Stochastic: the corpus is run TELEGRIND_LIVE_RUNS times on fresh directories (a pass, then a question) and each rated
check must hold in that share of runs. The deletion and correction scenarios run once each. Every run's directory is
left under pytest's basetemp with a summary.json beside it.

Claims section 8: REQ-FACTS-1, REQ-FACTS-2, REQ-FACTS-3, REQ-CONTRACT-1, REQ-CONTRACT-2, REQ-CONTRACT-3,
REQ-CONTRACT-4, REQ-CONTRACT-5, REQ-CONTRACT-6, REQ-CONTRACT-7, REQ-CONTRACT-8, REQ-CONTRACT-9, REQ-CONTRACT-10,
REQ-CONTRACT-11, REQ-CONTRACT-12, REQ-CONTRACT-13, REQ-CONTRACT-14, REQ-CONTRACT-15, REQ-DEPLOY-5, and REQ-REACT-3.
The ones no run below checks yet (CONTRACT-9, 13, 14, 15, DEPLOY-5) are listed so REQ-TEST-1 holds and are marked
`xfail(run=False)` below until a fixture exists: claimed, not exercised.
"""

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from televibe import Account, ClaudeCode, Engine
from tgkit import FakeBot, reaction, user_message

from telegrind.agent import Agent, make_provider
from telegrind.app import App
from telegrind.config import Config
from telegrind.incoming import incoming
from telegrind.layout import Layout

TZ = ZoneInfo("Asia/Almaty")
CORPUS = yaml.safe_load((Path(__file__).parent / "fixtures" / "entries.yaml").read_text())
ENTRIES: list[dict] = CORPUS["entries"]
QUESTION: dict = CORPUS["question"]
RUNS = int(os.environ.get("TELEGRIND_LIVE_RUNS", "3"))
RATE = float(os.environ.get("TELEGRIND_LIVE_PASS_RATE", "0.67"))
PASS_TIMEOUT_S = 900  # a 25-entry corpus in one pass; production's 600 s is sized for the backlog limit
QUESTION_TIMEOUT_S = 300  # production's default (REQ-TURN-10)
# The module fixture runs every corpus run before the first test that uses it, inside that test's timeout.
pytestmark = [pytest.mark.live, pytest.mark.timeout(RUNS * (PASS_TIMEOUT_S + QUESTION_TIMEOUT_S) + 600)]
UID = 7
FIRST_ID = 10
QUESTION_ID = 900
PYTHON_TABLES = {"processed", "keyterms", "settings", "accounts", "sqlite_sequence"}
NOT_A_QUANTITY = {"id", "at", "due", "revision"}  # and every *_at / *_id column: times and references
# What the agent may leave in the person's directory (REQ-CONTRACT-12).
ALLOWED_FILES = re.compile(r"(log|facts)\.sqlite(-wal|-shm|-journal)?|CLAUDE\.md|media")


def _texts(case: dict) -> list[str]:
    return case["texts"] if "texts" in case else [case["text"]]


def _home() -> str:
    home = os.environ.get("TELEGRIND_LIVE_HOME")
    if not home:
        pytest.skip("set TELEGRIND_LIVE_HOME to run the live gate")
    return home


def _config(tmp_path: Path, home: str) -> Config:
    env = {
        "BOT_TOKEN": "t",
        "ALLOWED_USER_IDS": str(UID),
        "DATA_DIR": str(tmp_path / "data"),
        "ACCOUNT_HOME": home,
        "PASS_TIMEOUT_S": str(PASS_TIMEOUT_S),
        "QUESTION_TIMEOUT_S": str(QUESTION_TIMEOUT_S),
    }
    if token := os.environ.get("TELEGRIND_LIVE_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return Config.from_env(env)


def _provider(cfg: Config, home: str) -> ClaudeCode:
    """The token when one is given (production's path), otherwise the account directory's own login."""
    provider = make_provider(cfg)
    return provider if provider is not None else ClaudeCode(Account(home))


def _seed(app: App, uid: int) -> dict[str, int]:
    """Entries into a fresh log, each at its own `sent`; returns text → message_id."""
    store = app.log_for(uid)
    ids, message_id = {}, FIRST_ID
    for case in ENTRIES:
        sent = datetime.fromisoformat(case.get("sent", "2026-09-11 12:00")).replace(tzinfo=TZ)
        for text in _texts(case):
            msg = user_message(uid=uid, message_id=message_id, text=text, date=sent)
            store.record_received(incoming(msg, now=int(sent.timestamp())))
            ids[text] = message_id
            message_id += 1
    return ids


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _tables(path: Path) -> list[str]:
    if not path.is_file():
        return []
    conn = _ro(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    finally:
        conn.close()


def _fact_tables(path: Path) -> list[str]:
    return [t for t in _tables(path) if t not in PYTHON_TABLES]


def _rows_for(path: Path, entry_id: int) -> list[tuple[str, dict]]:
    """Every fact row derived from `entry_id`, as (table, row)."""
    out = []
    if not path.is_file():
        return out
    conn = _ro(path)
    conn.row_factory = sqlite3.Row
    try:
        for table in _fact_tables(path):
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if "entry_id" not in cols:
                continue
            for row in conn.execute(f'SELECT * FROM "{table}" WHERE entry_id = ?', (entry_id,)):
                out.append((table, dict(row)))
    finally:
        conn.close()
    return out


def _numbers(row: dict) -> list[float]:
    """The quantities a row states: number-typed values, not ids, references or times."""
    return [
        float(v)
        for k, v in row.items()
        if isinstance(v, int | float)
        and not isinstance(v, bool)
        and k not in NOT_A_QUANTITY
        and not k.endswith(("_at", "_id"))
    ]


def _day(at: object) -> str | None:
    return datetime.fromtimestamp(at, TZ).date().isoformat() if isinstance(at, int | float) else None


def _count(path: Path, sql: str) -> int:
    if not path.is_file():
        return 0
    conn = _ro(path)
    try:
        return int(conn.execute(sql).fetchone()[0])
    except sqlite3.OperationalError:  # the agent never made the table
        return 0
    finally:
        conn.close()


# -- log.sqlite, compared by content ------------------------------------------------------------
# A byte hash of log.sqlite proves nothing: the log is in WAL mode and Python's connection stays open, so every
# write, the agent's included, lands in log.sqlite-wal and the main file's bytes do not move. And Python itself
# writes during a pass (the turn ledger, the digest) and before a question (the question's own entry).


def _log_snapshot(path: Path) -> dict[str, tuple[list[str], set[tuple]]]:
    conn = _ro(path)
    try:
        out = {}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cur = conn.execute(f'SELECT * FROM "{table}"')
            out[table] = ([d[0] for d in cur.description], set(cur.fetchall()))
        return out
    finally:
        conn.close()


def _log_changes(before: dict, after: dict, seeded: set[int]) -> list[str]:
    """What changed in the log beyond Python's own appends: an empty list means the agent wrote nothing."""
    problems = [f"new table {t}" for t in sorted(set(after) - set(before))]
    for table, (cols, rows) in before.items():
        if table not in after:
            problems.append(f"table {table} dropped")
            continue
        _, now = after[table]
        problems += [f"{table}: row changed or deleted: {row}" for row in sorted(rows - now, key=repr)]
        if "message_id" in cols:
            at = cols.index("message_id")
            problems += [f"{table}: new row for a seeded entry: {r}" for r in now - rows if r[at] in seeded]
    return problems


async def _one_run(tmp_path: Path, home: str) -> dict:
    cfg = _config(tmp_path, home)
    layout = Layout(cfg.data_dir)
    layout.install_contract()
    async with Engine(tmp_path / "state") as engine:
        app = App(config=cfg, bot=FakeBot(), layout=layout)
        agent = Agent(config=cfg, app=app, engine=engine, provider=_provider(cfg, home))
        app.answerer = agent
        ids = _seed(app, UID)
        log_path, facts_path = layout.log_path(UID), layout.facts_path(UID)
        seeded = set(ids.values())
        before = _log_snapshot(log_path)
        t0 = time.monotonic()
        await agent.run_pass(UID)
        pass_s = time.monotonic() - t0
        after_pass = _log_snapshot(log_path)
        texts = app.bot.texts()
        digest = texts[-1] if texts else ""
        t0 = time.monotonic()
        await app.on_message(user_message(uid=UID, text=QUESTION["text"], message_id=QUESTION_ID))
        await app.drain()
        question_s = time.monotonic() - t0
        after_q = _log_snapshot(log_path)
        texts = app.bot.texts()
        answer = texts[-1] if len(texts) > (1 if digest else 0) else ""
        app.close()
    user_dir = layout.user_dir(UID)
    stray = sorted(str(p.relative_to(user_dir)) for p in user_dir.iterdir() if not ALLOWED_FILES.fullmatch(p.name))
    stray += sorted(str(p.relative_to(user_dir)) for p in layout.media_dir(UID).rglob("*"))
    stray += sorted(p.name for p in cfg.data_dir.iterdir() if p.name not in ("CLAUDE.md", "users"))
    run = {
        "dir": str(tmp_path),
        "ids": ids,
        "facts": facts_path,
        "log_changes": (_log_changes(before, after_pass, seeded), _log_changes(after_pass, after_q, seeded)),
        "stray": stray,
        "digest": digest,
        "answer": answer,
        "pass_s": round(pass_s),
        "question_s": round(question_s),
    }
    summary = {k: v for k, v in run.items() if k != "facts"}
    summary["tables"] = {t: _count(facts_path, f'SELECT count(*) FROM "{t}"') for t in _tables(facts_path)}
    summary["rows"] = {text: _rows_for(facts_path, i) for text, i in ids.items()}
    (tmp_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return run


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> list[dict]:
    home = _home()
    return [asyncio.run(_one_run(tmp_path_factory.mktemp(f"run{i}"), home)) for i in range(RUNS)]


def _rate(checks: list[bool], detail: object = "") -> None:
    ok = sum(checks)
    assert ok / len(checks) >= RATE, f"{ok}/{len(checks)} runs passed (need {RATE:.0%}): {checks} {detail}"


def test_log_is_never_written_by_the_agent(runs):
    """REQ-FACTS-1, REQ-CONTRACT-12: no row of log.sqlite the agent could reach changes in a pass or a question turn,
    and no table appears; Python's own appends (the turn ledger, the digest, the question and its answer) are allowed.
    Every run, not a share of them."""
    for r in runs:
        in_pass, in_question = r["log_changes"]
        assert in_pass == [], f"the pass changed log.sqlite ({r['dir']}): {in_pass}"
        assert in_question == [], f"the question turn changed log.sqlite ({r['dir']}): {in_question}"


def test_the_agent_writes_nothing_outside_its_two_files(runs):
    """REQ-CONTRACT-12: after a pass and a question, the person's directory holds the log, facts.sqlite, CLAUDE.md
    and an empty media/; DATA_DIR holds the contract and users/."""
    _rate([r["stray"] == [] for r in runs], [r["stray"] for r in runs])


def test_the_three_tables_exist_and_processed_covers_every_entry(runs):
    """REQ-FACTS-2, REQ-CONTRACT-2, REQ-CONTRACT-3: processed has a row per seeded entry at revision 1."""
    checks, detail = [], []
    for r in runs:
        tables = set(_tables(r["facts"]))
        processed = {}
        if "processed" in tables:
            conn = _ro(r["facts"])
            processed = dict(conn.execute("SELECT entry_id, revision FROM processed"))
            conn.close()
        missing = sorted(i for i in r["ids"].values() if processed.get(i) != 1)
        checks.append({"processed", "keyterms", "settings"} <= tables and missing == [])
        detail.append({"tables": sorted(tables), "missing": missing})
    _rate(checks, detail)


def test_every_fact_table_has_entry_id_and_at(runs):
    """REQ-FACTS-3, REQ-CONTRACT-1: typed columns; entry_id and at, INTEGER NOT NULL, everywhere but the Python
    tables; no JSON column. Every run."""
    for r in runs:
        assert _fact_tables(r["facts"]), f"no fact table at all ({r['dir']})"
        conn = _ro(r["facts"])
        try:
            for table in _fact_tables(r["facts"]):
                info = {row[1]: (row[2].upper(), row[3]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
                assert {"entry_id", "at"} <= set(info), (r["dir"], table, info)
                assert info["entry_id"] == ("INTEGER", 1) and info["at"] == ("INTEGER", 1), (r["dir"], table, info)
                assert not any("JSON" in t for t, _ in info.values()), (r["dir"], table, info)
        finally:
            conn.close()


def _case_ok(r: dict, case: dict) -> tuple[bool, list]:
    expect = case["expect"]
    per_entry = [_rows_for(r["facts"], r["ids"][t]) for t in _texts(case)]
    rows = [x for rs in per_entry for x in rs]
    ok = bool(rows)
    if "rows_per_entry" in expect:
        ok = ok and [len(rs) for rs in per_entry] == expect["rows_per_entry"]
    if "amount" in expect:
        ok = ok and any(abs(n - expect["amount"]) < 1e-6 for _, row in rows for n in _numbers(row))
    if expect.get("numeric"):
        ok = ok and any(_numbers(row) for _, row in rows)
    if expect.get("not_notes"):
        ok = ok and not any(re.search(r"note|замет|запис", table, re.I) for table, _ in rows)
    if "date" in expect:
        ok = ok and any(_day(row.get("at")) == expect["date"] for _, row in rows)
    return ok, rows


@pytest.mark.parametrize("case", [c for c in ENTRIES if c.get("expect")], ids=lambda c: " + ".join(_texts(c)))
def test_each_entry_lands_as_the_corpus_expects(runs, case):
    """REQ-CONTRACT-4, REQ-CONTRACT-2: amounts as numbers, the loan sign, dates against the entry's own day,
    measurements out of notes, context never moving a fact onto a neighbour."""
    results = [_case_ok(r, case) for r in runs]
    _rate([ok for ok, _ in results], [rows for _, rows in results])


def test_pass_ends_with_one_line_digest(runs):
    """REQ-CONTRACT-11, REQ-TURN-7: the posted digest is one line naming counts."""
    checks = [bool(r["digest"]) and "\n" not in r["digest"].strip() and re.search(r"\d", r["digest"]) for r in runs]
    _rate([bool(c) for c in checks], [r["digest"] for r in runs])


def test_question_number_matches_sql(runs):
    """REQ-CONTRACT-6, REQ-CONTRACT-7: the answer's number is the amount SQL finds for the такси entry."""
    checks = []
    for r in runs:
        rows = _rows_for(r["facts"], r["ids"][QUESTION["entry"]])
        amounts = [n for _, row in rows for n in _numbers(row)]
        digits = re.sub(r"\s", "", r["answer"])
        checks.append(bool(amounts) and any(str(int(a)) in digits for a in amounts if a == int(a)))
    _rate(checks, [r["answer"] for r in runs])


def test_keyterms_are_maintained(runs):
    """REQ-CONTRACT-10: after a pass over the corpus, keyterms holds the person's words."""
    _rate([_count(r["facts"], "SELECT count(*) FROM keyterms") > 0 for r in runs])


def test_deleted_entry_has_no_rows_after_a_pass(tmp_path):
    """REQ-CONTRACT-5, REQ-REACT-3: a deleted entry's rows vanish on the next pass, not at once; a restored one
    comes back."""
    home = _home()

    async def run() -> tuple[list, list, list, list]:
        cfg = _config(tmp_path, home)
        layout = Layout(cfg.data_dir)
        layout.install_contract()
        async with Engine(tmp_path / "state") as engine:
            app = App(config=cfg, bot=FakeBot(), layout=layout)
            agent = Agent(config=cfg, app=app, engine=engine, provider=_provider(cfg, home))
            await app.on_message(user_message(uid=UID, message_id=FIRST_ID, text="4500 такси"))
            await agent.run_pass(UID)
            had = _rows_for(layout.facts_path(UID), FIRST_ID)
            await app.on_reaction(reaction(uid=UID, message_id=FIRST_ID, emoji="👍"))
            at_once = _rows_for(layout.facts_path(UID), FIRST_ID)
            await agent.run_pass(UID)
            gone = _rows_for(layout.facts_path(UID), FIRST_ID)
            await app.on_reaction(reaction(uid=UID, message_id=FIRST_ID, emoji=None))
            await agent.run_pass(UID)
            back = _rows_for(layout.facts_path(UID), FIRST_ID)
            app.close()
        return had, at_once, gone, back

    had, at_once, gone, back = asyncio.run(run())
    assert had, "the first pass derived nothing from «4500 такси»"
    assert at_once == had, "the reaction alone must not touch facts.sqlite"
    assert gone == [], f"the deleted entry kept its rows: {gone}"
    assert back, "the restored entry was not derived again"


def test_correction_becomes_a_rule(tmp_path):
    """REQ-CONTRACT-8: «это займ, не трата» changes the rows and writes the person's CLAUDE.md."""
    home = _home()

    async def run() -> tuple[str, list, list]:
        cfg = _config(tmp_path, home)
        layout = Layout(cfg.data_dir)
        layout.install_contract()
        async with Engine(tmp_path / "state") as engine:
            app = App(config=cfg, bot=FakeBot(), layout=layout)
            agent = Agent(config=cfg, app=app, engine=engine, provider=_provider(cfg, home))
            app.answerer = agent
            await app.on_message(user_message(uid=UID, message_id=FIRST_ID, text="Вася 5000"))
            await agent.run_pass(UID)
            first = _rows_for(layout.facts_path(UID), FIRST_ID)
            text = "Вася 5000 — это займ Васе, не трата. Вася всегда займы?"
            await app.on_message(user_message(uid=UID, message_id=FIRST_ID + 1, text=text))
            await app.drain()
            rules_path = layout.user_dir(UID) / "CLAUDE.md"
            rules = rules_path.read_text() if rules_path.exists() else ""
            rows = _rows_for(layout.facts_path(UID), FIRST_ID)
            app.close()
        return rules, first, rows

    rules, first, rows = asyncio.run(run())
    (tmp_path / "summary.json").write_text(
        json.dumps({"rules": rules, "first": first, "rows": rows}, ensure_ascii=False)
    )
    assert "Вася" in rules, f"no rule about Вася in the person's CLAUDE.md: {rules!r}"
    assert rows, "the corrected entry has no rows"
    assert not any(re.search(r"expense|spend|трат|расход", t, re.I) for t, _ in rows), (first, rows)


@pytest.mark.xfail(run=False, reason="needs a receipt photo fixture; REQ-CONTRACT-9 is measured on the dev bot first")
def test_receipt_photo_yields_line_items():
    """REQ-CONTRACT-9 (SPEC section 12): claimed, not exercised."""


@pytest.mark.xfail(run=False, reason="needs sanitized Kaspi/Freedom statements (SPEC section 12)")
def test_statement_import_reconciliation_and_transfers():
    """REQ-CONTRACT-13, REQ-CONTRACT-14, REQ-CONTRACT-15, REQ-DEPLOY-5: claimed, not exercised."""
