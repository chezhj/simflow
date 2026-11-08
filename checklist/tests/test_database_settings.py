"""
The SQLite connection options that keep concurrent plugin writers from
failing with "database is locked" (docs/PRE_RELEASE_PLAN.md 4.1).
"""

import pytest
from django.conf import settings
from django.db import connection, transaction


def test_sqlite_waits_for_writers_and_takes_the_write_lock_up_front():
    options = settings.DATABASES["default"]["OPTIONS"]
    assert options["transaction_mode"] == "IMMEDIATE"
    assert options["timeout"] >= 20


@pytest.mark.django_db(transaction=True)
def test_atomic_block_sends_begin_immediate_to_sqlite():
    """The option reaches SQLite: atomic() opens with BEGIN IMMEDIATE."""
    connection.ensure_connection()
    statements = []
    connection.connection.set_trace_callback(statements.append)
    try:
        with transaction.atomic():
            pass
    finally:
        connection.connection.set_trace_callback(None)

    assert "BEGIN IMMEDIATE" in statements
