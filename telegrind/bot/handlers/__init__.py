# Importing a handler module is what registers it. The order of these
# imports IS aiogram's match order, and `handlers` ends in a catch-all, so
# anything more selective must be imported ahead of it.
from . import handlers as handlers
