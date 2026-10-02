"""Microsoft Entra ID sign-in (OpenID Connect authorization code flow, via MSAL).

The signed-in user is kept in the signed session cookie, so either node behind
the VIP can serve any request.
"""
import logging

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings

log = logging.getLogger("portal.auth")
router = APIRouter()

ANONYMOUS = {"name": "Anonymous (sign-in disabled)", "email": "anonymous", "oid": ""}


class LoginRequired(Exception):
    pass


def _msal_app(settings: Settings):
    import msal

    return msal.ConfidentialClientApplication(
        settings.entra_client_id,
        authority=f"https://login.microsoftonline.com/{settings.entra_tenant_id}",
        client_credential=settings.entra_client_secret,
    )


def current_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or LoginRequired."""
    settings: Settings = request.app.state.settings
    if settings.auth_mode == "none":
        return ANONYMOUS
    user = request.session.get("user")
    if not user:
        raise LoginRequired()
    return user


def _authorised(claims: dict, allowed: set[str]) -> bool:
    if not allowed:
        return True
    granted = set(claims.get("groups", [])) | set(claims.get("roles", []))
    return bool(granted & allowed)


@router.get("/login")
async def login(request: Request):
    settings: Settings = request.app.state.settings
    if settings.auth_mode == "none":
        return RedirectResponse("/")
    flow = await anyio.to_thread.run_sync(
        lambda: _msal_app(settings).initiate_auth_code_flow(scopes=[], redirect_uri=settings.entra_redirect_uri)
    )
    request.session["auth_flow"] = flow
    return RedirectResponse(flow["auth_uri"])


@router.get("/auth/callback")
async def auth_callback(request: Request):
    settings: Settings = request.app.state.settings
    flow = request.session.pop("auth_flow", None)
    if not flow:
        return RedirectResponse("/login")
    params = dict(request.query_params)
    result = await anyio.to_thread.run_sync(
        lambda: _msal_app(settings).acquire_token_by_auth_code_flow(flow, params)
    )
    if "error" in result:
        log.warning("sign-in failed: %s", result.get("error_description", result["error"]))
        return HTMLResponse(f"<p>Sign-in failed: {result.get('error', 'unknown error')}.</p>"
                            "<p><a href='/login'>Try again</a></p>", status_code=401)
    claims = result.get("id_token_claims", {})
    if "_claim_names" in claims and "groups" in claims.get("_claim_names", {}):
        log.warning("group overage for %s; use app roles instead of group claims", claims.get("preferred_username"))
    if not _authorised(claims, settings.allowed_group_set):
        log.info("denied %s: not in an allowed group/role", claims.get("preferred_username"))
        return HTMLResponse("<p>You are signed in but not authorised to request servers. "
                            "Ask for access to the provisioning portal group.</p>", status_code=403)
    request.session["user"] = {
        "name": claims.get("name", ""),
        "email": claims.get("preferred_username") or claims.get("email", ""),
        "oid": claims.get("oid", ""),
    }
    log.info("signed in %s", request.session["user"]["email"])
    return RedirectResponse("/")


@router.get("/logout")
async def logout(request: Request):
    settings: Settings = request.app.state.settings
    request.session.clear()
    if settings.auth_mode == "none":
        return RedirectResponse("/")
    return RedirectResponse(f"https://login.microsoftonline.com/{settings.entra_tenant_id}/oauth2/v2.0/logout")
