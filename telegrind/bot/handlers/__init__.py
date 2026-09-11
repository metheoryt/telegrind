# Importing a handler module is what registers it, and registering an
# observer is also what subscribes its update type — aiogram derives
# allowed_updates from the handlers that exist.
#
# Order matters only *within* an observer: `handlers` ends in a catch-all
# message handler, so any more selective message module must be imported
# ahead of it. `reactions` is a different observer and does not compete.
from . import handlers as handlers
from . import reactions as reactions
