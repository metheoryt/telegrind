# Importing a handler module is what registers it, and registering an
# observer is also what subscribes its update type — aiogram derives
# allowed_updates from the handlers that exist.
#
# Order matters only *within* an observer. `handlers` ends in a filterless
# catch-all message handler, so `query` must come first or /q is swallowed
# and stored as an ordinary message. The decorator-free halves of
# `handlers` live in `receipts` and `routing` precisely so that importing
# `query` does not drag `handlers` in ahead of itself. `reactions` is a
# different observer and does not compete.
from . import query as query  # noqa: I001
from . import handlers as handlers
from . import reactions as reactions
