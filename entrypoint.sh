#!/bin/sh
set -e

if [ $# -eq 0 ]; then
    alembic upgrade head
    exec python main.py
fi

exec "$@"
