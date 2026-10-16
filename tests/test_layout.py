"""Layout on disk (SPEC section 2)."""

from telegrind.layout import CONTRACT_SOURCE, Layout


def test_user_dir_is_created_with_media_and_nothing_else(tmp_path):
    """REQ-LAYOUT-1: the person's directory and media/ appear; facts.sqlite and CLAUDE.md are never made by Python."""
    layout = Layout(tmp_path)
    d = layout.ensure_user(42)
    assert d == tmp_path / "users" / "42" and d.is_dir() and (d / "media").is_dir()
    assert not layout.facts_path(42).exists() and not (d / "CLAUDE.md").exists()
    assert layout.log_path(42) == d / "log.sqlite" and layout.media_dir(42) == d / "media"
    assert layout.users() == [42]


def test_contract_is_overwritten_from_the_repo_copy(tmp_path):
    """REQ-LAYOUT-2: DATA_DIR/CLAUDE.md is a copy of agent/CLAUDE.md, rewritten on every start."""
    layout = Layout(tmp_path)
    layout.contract.parent.mkdir(parents=True, exist_ok=True)
    layout.contract.write_text("stale")
    layout.install_contract(CONTRACT_SOURCE)
    assert layout.contract.read_text() == CONTRACT_SOURCE.read_text() and "telegrind" in layout.contract.read_text()


def test_contract_and_person_rules_are_on_the_cwd_parent_chain(tmp_path):
    """REQ-LAYOUT-3: Claude Code walks cwd and its parents for CLAUDE.md; both files sit on that chain."""
    layout = Layout(tmp_path)
    cwd = layout.ensure_user(7)
    layout.install_contract(CONTRACT_SOURCE)
    (cwd / "CLAUDE.md").write_text("# my rules")  # what the agent would write
    chain = [cwd, *cwd.parents]
    assert layout.contract.parent in chain and cwd in chain
    assert [p for p in chain if (p / "CLAUDE.md").exists()] == [cwd, layout.contract.parent]


def test_contract_source_exists():
    """REQ-LAYOUT-2: agent/CLAUDE.md is in the repository."""
    assert CONTRACT_SOURCE.is_file() and CONTRACT_SOURCE.parts[-2:] == ("agent", "CLAUDE.md")
