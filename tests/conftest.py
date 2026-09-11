"""Test-wide setup.

The only thing needed here is `.env`, and only by the `-m llm` gate: the
default suite makes no network call. Loading it through python-dotenv
rather than sourcing it in a shell is not a convenience — the file has
CRLF line endings, and a shell `source` carries the `\r` into the value,
which Anthropic's client then reports by printing the whole API key into
the failure output.
"""

from dotenv import load_dotenv

load_dotenv()
