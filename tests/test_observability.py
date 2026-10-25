"""Homework 2, Part D: authentication tests for the session endpoints.

These tests exercise identity binding and token scoping only. They do not
require Langfuse, Docker, or a model provider key: they call the endpoint
functions directly and inspect the signed tokens and the in-memory session
store.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server import app as server_app


def test_create_session_rejects_role_mismatch() -> None:
    """A request whose claimed role differs from the stored role is 403."""
    server_app._SESSIONS.clear()
    # User 9002 is a merchant in the seeded database; claiming "shopper" must
    # be rejected with 403, and no session may be created.
    with pytest.raises(HTTPException) as exc:
        server_app.create_session(
            server_app.SessionCreate(user_id=9002, role="shopper")
        )
    assert exc.value.status_code == 403
    assert server_app._SESSIONS == {}


def test_token_cannot_authorize_a_different_session() -> None:
    """A token issued for one session cannot authorize another session."""
    server_app._SESSIONS.clear()
    first = server_app.create_session(
        server_app.SessionCreate(user_id=9002, role="merchant")
    )
    second = server_app.create_session(
        server_app.SessionCreate(user_id=9002, role="merchant")
    )
    assert first["session_id"] != second["session_id"]

    # The first token is valid for its own session.
    ctx = server_app._authorize(
        first["session_id"], f"Bearer {first['token']}"
    )
    assert ctx.user_id == 9002

    # The same token must not authorize the second session.
    with pytest.raises(HTTPException) as exc:
        server_app._authorize(second["session_id"], f"Bearer {first['token']}")
    assert exc.value.status_code == 403
