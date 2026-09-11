# Importing a handler module is what registers it, and registering an
# observer is also what subscribes its update type — aiogram derives
# allowed_updates from the handlers that exist.
#
# Order matters only *within* an observer. `handlers` ends in a catch-all
# message handler AND in a slash catch-all ahead of it, so `query` must
# come first or /q is swallowed and stored as an ordinary command. The
# decorator-free half of `handlers` lives in `receipts` precisely so that
# importing `query` does not drag `handlers` in ahead of itself.
# `reactions` is a different observer and does not compete.
from . import query as query  # noqa: I001
from . import handlers as handlers
from . import reactions as reactions
