# ruff: noqa: I001 — import order here IS aiogram's registration order, and
# aiogram stops at the first matching handler. `handlers` ends in a catch-all
# F.text, so it must import last; isort would sort it ahead of `start` and
# the catch-all would then swallow /start. Do not let isort touch this line.
from . import commands as commands, start as start, handlers as handlers
