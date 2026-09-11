"""What survives the registry.

Phase 1 makes no LLM call, so there is no schema and no prompt to assert
on. What is left is the corpus of rules — accumulated judgement about
real messages — and the version marker that stamps every fact.
"""

from telegrind.llm import EXTRACTION_RULES, PROMPT_VERSION, current_model


def test_prompt_version_is_set() -> None:
    assert PROMPT_VERSION


def test_the_rules_state_the_loan_sign_convention() -> None:
    assert "a loan given out is negative" in EXTRACTION_RULES


def test_the_rules_keep_the_leading_amount_judgement() -> None:
    assert "444 куколд" in EXTRACTION_RULES


def test_the_rules_separate_the_date_of_a_fact_from_a_mentioned_date() -> None:
    assert "билеты на 15 октября" in EXTRACTION_RULES


def test_the_rules_carry_no_timestamp_and_no_currency() -> None:
    """They are the cached prefix in Phase 2; both of those vary per call."""
    assert "Current time" not in EXTRACTION_RULES
    assert "KZT" not in EXTRACTION_RULES


def test_the_model_comes_from_the_environment_or_a_default(
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5")
    assert current_model() == "claude-sonnet-5"
    monkeypatch.delenv("LLM_MODEL")
    assert current_model() == "claude-haiku-4-5"
