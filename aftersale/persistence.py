# Adapted from agent-service-toolkit src/memory/sqlite.py, MIT.
# Copyright (c) 2024 Joshua Carroll. See LICENSES/agent-service-toolkit.txt.
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def get_sqlite_saver(path: Path):
    """Durable graph checkpoints. No InMemoryStore or global settings dependency."""
    return AsyncSqliteSaver.from_conn_string(str(path))
