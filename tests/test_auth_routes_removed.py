"""Regression tests: the email-challenge auth endpoints must stay removed.

See the tombstone docstring in ``app/auth_routes.py`` for the full rationale.
These routes let an unauthenticated caller obtain a JWT that asserted the
``governor`` role, because ``auth.create_jwt`` defaults ``author_role`` to
``DEFAULT_AUTHOR_ROLE == "governor"``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_routes import router


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/auth/send-challenge", "/auth/verify-code"])
def test_challenge_endpoints_are_gone(client: TestClient, path: str) -> None:
    """Both removed endpoints must 404 (or 405 if a sibling method lingers)."""
    resp = client.post(path, json={"email": "pentest@example.com", "code": "123456"})
    assert resp.status_code in (404, 405), (
        f"{path} is reachable again. This route must not be re-registered — it "
        "allowed unauthenticated issuance of a governor-level JWT."
    )


def test_auth_router_registers_no_routes() -> None:
    """The auth router must stay empty; any new route needs review."""
    assert router.routes == [], (
        "The auth router gained routes. Review for the privilege-escalation "
        "class documented in app/auth_routes.py before shipping: do not rely on "
        "create_jwt's default author_role, validate the email, require a "
        "registered governor, and rate-limit."
    )
