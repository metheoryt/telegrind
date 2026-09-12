"""The Claude meta layer: a handler, not a second process.

It sees the update, it owns the conversation, it spawns `claude -p` and it
sends the reply. It is a guest in whatever bot registers it — it takes a
config object and callables, and reaches into none of the host's internals.
"""
