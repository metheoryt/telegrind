"""Anthropic plumbing and the extraction rules corpus.

Phase 1 makes no LLM call. What is kept here is what Phase 2 needs and
cannot re-derive: the client, the model names, and the rules prose, which
is accumulated judgement about real messages rather than code.
"""

import logging
import os

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

PROMPT_VERSION = "2026-09-10.1"

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2048

#: Kept verbatim from the registry-era system prompt. Phase 2 composes this
#: with the observed taxonomy; the judgement in it does not depend on how
#: the categories are declared, so it outlives the registry.
EXTRACTION_RULES = """\
- One message may hold several facts. Return one array element each.
- Return an empty array only for a message that states no fact at all.
- Anything you cannot confidently place goes to the `facts` category,
  with the message text kept verbatim. Never drop a fact.
- When a message opens with an amount of money and no other category
  fits it, it is an `expense`, and the rest of the message is the
  comment: `4500 такси`, `444 куколд`, `300 фигня`, and `444` on its
  own with an empty comment. Do not fall back to `facts` because the
  comment names nothing you recognise as buyable — what it was spent
  on is not your judgement to make.
- Loan amounts carry a sign convention: a loan given out is negative
  (-100), a repayment received is positive (+100). A bare amount with
  no direction stated means a loan given out, so -100.
- Do not invent fields. Do not invent values. An unstated text field
  is an empty string.
- Keep the user's own wording in text fields; do not translate it.
- A date the message mentions *about* the thing is not the date of the
  fact. `билеты на 15 октября` was bought now and the flight is on the
  15th; `оплатил квартиру за октябрь` was paid now. Date the fact to
  when it happened, put the mentioned date in a `due` field if the
  category has one, and otherwise keep it in the text field.
"""


def client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def current_model() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)
