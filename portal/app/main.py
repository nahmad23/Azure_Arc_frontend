"""Self-service server provisioning portal (FastAPI + HTMX).

Submitting the form validates the request and queues the Azure DevOps
provisioning pipeline, which runs the existing Terraform code.
"""
import asyncio
import logging
import socket
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .ado import AdoClient, AdoError, DryRunClient
from .catalog import load_catalog
from .config import Settings, get_settings
from .models import check_hostname, parse_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("portal")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
NODE = socket.gethostname()
MULTI_VALUE_FIELDS = ("ad_groups", "security", "automation")


def form_values(data) -> dict:
    """Submitted values as a dict, with the multi-value fields as lists."""
    v = {k: data.get(k) for k in data.keys() if k not in MULTI_VALUE_FIELDS}
    for k in MULTI_VALUE_FIELDS:
        v[k] = data.getlist(k)
    return v


def create_app(settings: Settings | None = None, ado=None) -> FastAPI:
    settings = settings or get_settings()
    settings.check()
    catalog = load_catalog(settings.catalog_path)
    ado = ado or (DryRunClient() if settings.dry_run else AdoClient(settings))

    app = FastAPI(title=settings.app_title, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="portal_session",
        same_site="lax",
        https_only=settings.session_cookie_secure,
        max_age=8 * 3600,
    )
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(auth.router)
    app.state.settings = settings

    def render(request: Request, name: str, user: dict | None = None, **ctx) -> HTMLResponse:
        ctx.update(settings=settings, catalog=catalog, user=user, node=NODE,
                   today=date.today().isoformat(), signed_in=bool(user and user.get("oid")))
        ctx.setdefault("errors", {})
        return templates.TemplateResponse(request, name, ctx)

    def form_defaults(user: dict) -> dict:
        return {
            "requester_name": user.get("name", "") if user.get("oid") else "",
            "requester_email": user.get("email", "") if user.get("oid") else "",
            "request_type": "new",
            "cpu": "4",
            "memory_gb": "16",
            "os_disk_gb": "100",
            "disk_type": catalog.disk_types[0].id if catalog.disk_types else "",
            "ip_assignment": "dhcp",
            "domain_join": "yes",
            "domain": catalog.domains[0].id if len(catalog.domains) == 1 else "",
            "ad_groups": [],
            "security": [o.id for o in catalog.security_options if o.default],
            "automation": [o.id for o in catalog.automation_options if o.default],
            "planned_date": date.today().isoformat(),
        }

    @app.exception_handler(auth.LoginRequired)
    async def _login_required(request: Request, exc: auth.LoginRequired):
        if request.headers.get("HX-Request"):
            # Let HTMX do a full-page navigation instead of swapping a login page into a fragment.
            return HTMLResponse("", headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login")

    # HEAD too, so `curl -I` and load-balancer / monitoring probes work.
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz():
        return JSONResponse({"status": "ok", "node": NODE, "dry_run": settings.dry_run})

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def index(request: Request, user: dict = Depends(auth.current_user)):
        return render(request, "index.html", user, v=form_defaults(user))

    # --- HTMX fragments: parts of the form that depend on earlier answers ----
    def fragment(name: str):
        async def handler(request: Request, user: dict = Depends(auth.current_user)):
            return render(request, name, user, v=dict(request.query_params))
        return handler

    for path, template in {
        "/form/datacenters": "_datacenters.html",
        "/form/vlans": "_vlans.html",
        "/form/cpu-custom": "_cpu_custom.html",
        "/form/ip-fields": "_ip_fields.html",
        "/form/domain-fields": "_domain_fields.html",
        "/form/source-server": "_source_server.html",
        "/form/platforms": "_platform_rows.html",
    }.items():
        app.add_api_route(path, fragment(template), methods=["GET"], response_class=HTMLResponse)

    @app.get("/form/check-hostname", response_class=HTMLResponse)
    async def hostname_check(request: Request, hostname: str = "", os: str = "",
                             user: dict = Depends(auth.current_user)):
        hostname = hostname.strip()
        os_obj = catalog.os(os)
        error = check_hostname(hostname, os_obj.family if os_obj else "linux")
        warning = None
        if not error:
            try:
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(loop.getaddrinfo(hostname, None), timeout=2)
                warning = f"'{hostname}' already resolves in DNS. Make sure it is not in use."
            except (OSError, asyncio.TimeoutError):
                pass
        return render(request, "_hostname_msg.html", user, error=error, warning=warning)

    @app.post("/requests", response_class=HTMLResponse)
    async def submit(request: Request, user: dict = Depends(auth.current_user)):
        # Browsers cannot add this header to a cross-site form post, so requiring
        # it blocks CSRF in addition to the SameSite session cookie.
        if not request.headers.get("HX-Request"):
            return HTMLResponse("Bad request", status_code=400)
        form = await request.form()
        req, errors = parse_request(form, catalog, user)
        if errors:
            return render(request, "_form.html", user, v=form_values(form), errors=errors)
        try:
            run = await ado.queue_run(req)
        except AdoError as exc:
            log.error("queueing pipeline failed: %s", exc)
            return render(request, "_form.html", user, v=form_values(form),
                          submit_error=f"The request could not be submitted: {exc}")
        return render(request, "_submitted.html", user, req=req, run=run)

    @app.get("/requests/{run_id}/status", response_class=HTMLResponse)
    async def run_status(request: Request, run_id: int, user: dict = Depends(auth.current_user)):
        try:
            run = await ado.get_run(run_id)
        except AdoError as exc:
            return render(request, "_run_status.html", user, run=None, run_id=run_id, error=str(exc))
        return render(request, "_run_status.html", user, run=run, run_id=run_id, error=None)

    @app.get("/requests", response_class=HTMLResponse)
    async def run_list(request: Request, user: dict = Depends(auth.current_user)):
        partial = bool(request.headers.get("HX-Request"))
        try:
            runs, error = await ado.list_runs(), None
        except AdoError as exc:
            runs, error = [], str(exc)
        return render(request, "_runs_table.html" if partial else "requests.html", user, runs=runs, error=error)

    return app
