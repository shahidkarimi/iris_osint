"""FastAPI dashboard + JSON/GeoJSON API over ``iris_staging``.

Server-rendered (Jinja2 + Tailwind CSS v4, built at image build time), no third-party CDNs:
fonts and MapLibre GL are self-hosted; the only external requests are basemap tiles.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .. import __version__
from ..config import Settings, get_settings
from ..db import queries
from ..report import build_report, render_markdown, signals_csv
from .i18n import LOCALES, fmt_area, fmt_date, fmt_number, translate

PACKAGE_DIR = Path(str(resources.files("jll_harvester")))
WEB_DIR = PACKAGE_DIR / "web"
PAGE_SIZE = 25


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    pool = ConnectionPool(
        settings.dsn,
        min_size=1,
        max_size=6,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 5},
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool.open(wait=False)
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(
        title="IRIS-SIG-45 · JLL DE Signal Harvester",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.globals.update(
        fmt_number=fmt_number,
        fmt_area=fmt_area,
        fmt_date=fmt_date,
        version=__version__,
        task_id=settings.task_id,
        map_style=settings.map_style_url,
        map_style_dark=settings.map_style_url_dark,
    )
    templates.env.filters["tojson_pretty"] = lambda v: json.dumps(
        v, indent=2, ensure_ascii=False, default=_json_default
    )

    # -- middleware --------------------------------------------------------------------------
    @app.middleware("http")
    async def security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        user, password = settings.web_basic_auth_user, settings.web_basic_auth_password
        needs_auth = bool(user and password) and request.url.path != "/healthz"
        if needs_auth and not _authorised(
            request.headers.get("authorization"), user or "", password.get_secret_value() if password else ""
        ):
            return PlainTextResponse(
                "Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="IRIS"'}
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob: https://tiles.openfreemap.org; "
            "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; child-src blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; font-src 'self' https://tiles.openfreemap.org; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    def locale_of(request: Request) -> str:
        lang = request.query_params.get("lang") or request.cookies.get("lang") or settings.default_locale
        return lang if lang in LOCALES else "de"

    def render(request: Request, template: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        locale = locale_of(request)
        ctx.update(
            request=request,
            locale=locale,
            t=lambda key: translate(key, locale),
            path=request.url.path,
        )
        return templates.TemplateResponse(request, template, ctx, status_code=status_code)

    def db() -> psycopg.Connection[Any]:
        return pool.getconn(timeout=5)

    class _Conn:
        def __enter__(self) -> psycopg.Connection[Any]:
            try:
                self.conn = db()
            except Exception as exc:
                raise HTTPException(503, f"database unavailable: {exc}") from exc
            return self.conn

        def __exit__(self, *exc: object) -> None:
            pool.putconn(self.conn)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)

    @app.exception_handler(psycopg.errors.UndefinedTable)
    async def not_migrated(request: Request, exc: Exception) -> Response:
        return render(
            request, "error.html", status_code=503, code=503, detail="Schema missing — run `jll-harvester migrate`."
        )

    # -- pages -------------------------------------------------------------------------------
    @app.get("/lang/{code}")
    def set_lang(code: str, request: Request) -> RedirectResponse:
        target = request.headers.get("referer") or "/"
        if not target.startswith(str(request.base_url)):
            target = "/"
        resp = RedirectResponse(target, status_code=303)
        if code in LOCALES:
            resp.set_cookie("lang", code, max_age=365 * 86400, samesite="lax", httponly=True)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        with _Conn() as conn:
            summary = queries.summary(conn)
            latest, _ = queries.list_signals(conn, queries.SignalFilter(), limit=6)
            run = queries.latest_run(conn)
            good_run = queries.latest_successful_run(conn)
            corpus = queries.corpus_stats(conn)
        return render(
            request, "dashboard.html", summary=summary, latest=latest, run=run, good_run=good_run, corpus=corpus
        )

    @app.get("/signale", response_class=HTMLResponse)
    def signal_list(
        request: Request,
        typ: str | None = None,
        geo: str | None = None,
        q: str | None = None,
        jahr: str | None = None,
        ort: str | None = None,
        sort: str = "published_desc",
        seite: int = Query(1, ge=1),
    ) -> HTMLResponse:
        # the filter form submits "jahr=" for "all years"; an int param would turn that into a JSON 422
        year = int(jahr) if jahr and jahr.isdigit() else None
        flt = queries.SignalFilter(
            signal_type=typ or None, geocode_status=geo or None, q=q or None, year=year, municipality=ort or None
        )
        with _Conn() as conn:
            rows, total = queries.list_signals(conn, flt, sort=sort, limit=PAGE_SIZE, offset=(seite - 1) * PAGE_SIZE)
            years = queries.summary(conn)["years"]
        params = {
            k: v for k, v in {"typ": typ, "geo": geo, "q": q, "jahr": year, "ort": ort, "sort": sort}.items() if v
        }
        pages = max(1, -(-total // PAGE_SIZE))
        return render(
            request,
            "signals.html",
            rows=rows,
            total=total,
            page=seite,
            pages=pages,
            filters=params,
            years=years,
            qs=lambda **kw: urlencode({**params, **{k: v for k, v in kw.items() if v is not None}}),
        )

    @app.get("/signale/{signal_id}", response_class=HTMLResponse)
    def signal_detail(request: Request, signal_id: int) -> HTMLResponse:
        with _Conn() as conn:
            row = queries.get_signal(conn, signal_id)
            if row is None:
                raise HTTPException(404, "Signal nicht gefunden")
            near = queries.nearby(conn, signal_id)
        return render(request, "signal_detail.html", s=row, nearby=near)

    @app.get("/karte", response_class=HTMLResponse)
    def map_page(request: Request) -> HTMLResponse:
        with _Conn() as conn:
            summary = queries.summary(conn)
        return render(request, "map.html", summary=summary)

    @app.get("/qualitaet", response_class=HTMLResponse)
    def quality(request: Request) -> HTMLResponse:
        with _Conn() as conn:
            checks = queries.run_checks(conn)
            runs = queries.runs(conn, 15)
            run = runs[0] if runs else None
            source_run = next((r for r in runs if r.get("source_check", {}).get("brief_endpoint")), run)
            issues = queries.run_issues(conn, str(run["run_id"]), limit=300) if run else []
            corpus = queries.corpus_stats(conn)
            no_point, _ = queries.list_signals(conn, queries.SignalFilter(), limit=1000)
            rejections = queries.recent_rejections(conn, limit=25)
            summary = queries.summary(conn)
        return render(
            request,
            "quality.html",
            checks=checks,
            runs=runs,
            run=run,
            source_run=source_run,
            issues=[i for i in issues if i["severity"] != "info"],
            infos=[i for i in issues if i["severity"] == "info"],
            corpus=corpus,
            no_point=[r for r in no_point if r["geocode_status"] != "resolved"],
            rejections=rejections,
            summary=summary,
        )

    @app.get("/methodik", response_class=HTMLResponse)
    def methodology(request: Request) -> HTMLResponse:
        return render(request, "method.html", settings=settings)

    # -- API ---------------------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        try:
            with pool.connection(timeout=3) as conn:
                conn.execute("SELECT 1")
            return {"status": "ok", "db": "ok", "version": __version__, "revision": settings.revision}
        except Exception as exc:  # pragma: no cover - exercised in deployment
            raise HTTPException(503, f"db: {exc}") from exc

    @app.get("/api/summary")
    def api_summary() -> JSONResponse:
        with _Conn() as conn:
            data = queries.summary(conn)
        return JSONResponse(json.loads(json.dumps(data, default=_json_default)))

    @app.get("/api/signals")
    def api_signals(
        typ: str | None = None,
        geo: str | None = None,
        q: str | None = None,
        jahr: int | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        flt = queries.SignalFilter(signal_type=typ, geocode_status=geo, q=q, year=jahr)
        with _Conn() as conn:
            rows, total = queries.list_signals(conn, flt, limit=limit, offset=offset)
        return JSONResponse(json.loads(json.dumps({"total": total, "items": rows}, default=_json_default)))

    @app.get("/api/signals/{signal_id}")
    def api_signal(signal_id: int) -> JSONResponse:
        with _Conn() as conn:
            row = queries.get_signal(conn, signal_id)
        if row is None:
            raise HTTPException(404, "not found")
        return JSONResponse(json.loads(json.dumps(row, default=_json_default)))

    @app.get("/api/signals.geojson")
    def api_geojson(typ: str | None = None, jahr: int | None = None) -> Response:
        with _Conn() as conn:
            fc = queries.geojson(conn, queries.SignalFilter(signal_type=typ, year=jahr))
        return Response(json.dumps(fc, default=_json_default, ensure_ascii=False), media_type="application/geo+json")

    @app.get("/api/checks")
    def api_checks() -> JSONResponse:
        with _Conn() as conn:
            results = queries.run_checks(conn)
        return JSONResponse([{k: v for k, v in r.__dict__.items() if k != "sql"} for r in results])

    @app.get("/export/signals.csv")
    def export_csv() -> Response:
        with _Conn() as conn:
            rows, _ = queries.list_signals(conn, queries.SignalFilter(), limit=100_000)
        return Response(
            signals_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="iris_sig45_signals.csv"'},
        )

    @app.get("/export/quality-report.md")
    def export_report() -> Response:
        with _Conn() as conn:
            text = render_markdown(build_report(conn))
        return Response(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="quality_report.md"'},
        )

    return app


def _authorised(header: str | None, user: str, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    given_user, _, given_pw = decoded.partition(":")
    return secrets.compare_digest(given_user, user) and secrets.compare_digest(given_pw, password)
