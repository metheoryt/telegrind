# This package deliberately registers nothing on import.
#
# Importing a handler module is what registers its handlers, and this
# __init__ used to do those imports. It cannot any more: `routing` needs
# `handlers.receipts` (for the RECEIPT_EMOJI default on `route`), and
# importing anything from this package runs this file — so an __init__ that
# imported `query` made `import telegrind.bot.routing` re-enter `routing`
# before `route` existed. `handlers/__init__ → query` was the only edge in
# that cycle that existed purely for sequencing, so the sequencing moved to
# the one place that actually performs registration.
#
# Registration, and the order it has to happen in, now live in
# `telegrind.bot.setup.setup_dispatcher`. Importing this package gives you
# a namespace, not a wired-up bot.
