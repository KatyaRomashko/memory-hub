"""Who is calling the BFF (#671).

The dashboard runs behind an oauth-proxy sidecar that authenticates the user
against OpenShift and forwards the identity in ``X-Forwarded-*`` headers. The
BFF trusts those headers only when ``MEMORYHUB_UI_TRUST_PROXY_HEADERS`` is on,
because anything that can reach the BFF directly could forge them.

With trust off, the BFF keeps its single-operator behaviour: every request
acts as ``MEMORYHUB_UI_OPERATOR_NAME`` with platform-admin rights.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from src.config import Settings, get_settings


@dataclass(frozen=True)
class Caller:
    username: str
    is_admin: bool
    # False when the identity is the configured operator, not a signed-in user.
    authenticated: bool


def _csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def get_caller(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Caller:
    if not settings.ui_trust_proxy_headers:
        return Caller(username=settings.ui_operator_name, is_admin=True, authenticated=False)

    username = (
        request.headers.get("x-forwarded-preferred-username") or request.headers.get("x-forwarded-user") or ""
    ).strip()
    if not username:
        raise HTTPException(status_code=401, detail="Sign in to manage projects.")
    groups = _csv(request.headers.get("x-forwarded-groups", ""))
    is_admin = username in _csv(settings.ui_admin_users) or (
        bool(settings.ui_admin_group) and settings.ui_admin_group in groups
    )
    return Caller(username=username, is_admin=is_admin, authenticated=True)


CallerDep = Annotated[Caller, Depends(get_caller)]
