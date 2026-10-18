"""Flask application: roles list and edit, run log, configuration, CSV export."""

from __future__ import annotations

import math
import secrets
from collections.abc import Collection, Mapping
from typing import Any
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.wrappers import Response as WerkzeugResponse

from ..config import Config, ConfigError
from ..matching import MATCH_MODES
from ..models import SORTED_STATUSES, STATUSES, Role, RoleFilter
from ..store import Store, StoreError
from ..timeutil import format_display, has_time_of_day, is_day, now_ist, weekday_short
from .forms import (
    NEW_PHRASE_ROWS,
    parse_config_form,
    parse_role_form,
    roles_to_csv,
    safe_next,
)

PAGE_SIZE = 100
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
SAFE_SITES = frozenset({"same-origin", "none"})

# (field, label, hint) in the order they appear on the Config page.
SETTING_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Search",
        (
            (
                "match_mode",
                "Title match mode",
                "contains: phrase anywhere in the title; exact: whole title",
            ),
            ("location_name", "Location", "Search location text, for example India"),
            ("geo_id", "Geo ID", "LinkedIn geography ID (India is 102713980)"),
            (
                "time_window_seconds",
                "Posted within (seconds)",
                "86400 is the only value verified to work",
            ),
            ("max_pages_per_keyword", "Page cap per keyword", "10 results per page"),
            (
                "stop_after_empty_pages",
                "Stop after empty pages",
                "Consecutive pages with no title match",
            ),
        ),
    ),
    (
        "Pacing",
        (
            ("delay_seconds_min", "Delay between requests, min (s)", ""),
            ("delay_seconds_max", "Delay between requests, max (s)", ""),
            ("keyword_pause_min", "Pause between keywords, min (s)", ""),
            ("keyword_pause_max", "Pause between keywords, max (s)", ""),
        ),
    ),
    (
        "Throttling",
        (
            ("retry_count", "Retries per page", ""),
            ("retry_wait_min", "Retry wait, min (s)", ""),
            ("retry_wait_max", "Retry wait, max (s)", ""),
            ("abort_after_consecutive_throttles", "Abort after throttled responses in a row", ""),
        ),
    ),
    (
        "Runs and retention",
        (
            ("min_gap_between_runs_minutes", "Minimum gap between runs (min)", "--force overrides"),
            ("skipped_titles_retention_days", "Keep skipped titles (days)", ""),
        ),
    ),
)


def _host_name(host: str) -> str:
    """Host without the port, keeping IPv6 brackets."""
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.split(":")[0]


def create_app(
    store: Store,
    *,
    secret_key: str | None = None,
    allowed_hosts: Collection[str] = LOOPBACK_HOSTS,
) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secret_key or secrets.token_hex(32)
    allowed = frozenset(h.lower() for h in allowed_hosts)

    # ---- request guards -------------------------------------------------------------------

    @app.before_request
    def guard() -> None:
        if _host_name(request.host).lower() not in allowed:
            abort(400, "Unexpected Host header")
        if request.method == "POST":
            # Browsers state where a request came from in Sec-Fetch-Site; that is the most
            # reliable signal. Origin is the fallback for browsers that do not send it.
            site = request.headers.get("Sec-Fetch-Site")
            if site is not None:
                if site not in SAFE_SITES:
                    abort(403, "Cross-site request refused")
                return
            origin = request.headers.get("Origin")
            if origin is not None and urlparse(origin).netloc != request.host:
                abort(403, "Cross-site request refused")

    @app.after_request
    def secure_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def http_error(error: Any) -> tuple[str, int]:
        code = getattr(error, "code", 500)
        message = getattr(error, "description", "")
        return render_template("error.html", code=code, message=message), code

    # ---- template helpers -----------------------------------------------------------------

    @app.template_filter("ist")
    def ist(value: str | None) -> str:
        return format_display(value)

    @app.template_filter("weekday")
    def weekday(day: str) -> str:
        return weekday_short(day)

    @app.template_filter("ist_date")
    def ist_date(value: str | None) -> str:
        return format_display(value, with_time=False)

    @app.template_filter("posted")
    def posted(role: Role) -> str:
        if not role.posted_on:
            return "unknown"
        if has_time_of_day(role.posted_precision):
            return format_display(role.posted_on)
        text = format_display(role.posted_on, with_time=False)
        return text if role.posted_precision == "day" else f"~{text}"

    @app.template_filter("safe_link")
    def safe_link(url: str) -> str:
        return url if url.startswith("https://") else ""

    @app.template_global("without")
    def without(values: Mapping[str, str], key: str) -> dict[str, str]:
        return {k: v for k, v in values.items() if k != key}

    @app.context_processor
    def inject() -> dict[str, Any]:
        return {"statuses": SORTED_STATUSES, "endpoint": request.endpoint}

    # ---- roles ----------------------------------------------------------------------------

    def read_filter(args: Mapping[str, str]) -> tuple[RoleFilter, dict[str, str]]:
        status = args.get("status", "")
        query = args.get("q", "").strip()
        dup = args.get("dup", "") == "1"
        fetched = args.get("fetched", "")
        flt = RoleFilter(
            query=query,
            status=status if status in STATUSES else None,
            duplicates_only=dup,
            fetched_on=fetched if is_day(fetched) else None,
        )
        shown = {
            "q": flt.query,
            "status": flt.status or "",
            "dup": "1" if dup else "",
            "fetched": flt.fetched_on or "",
        }
        return flt, {k: v for k, v in shown.items() if v}

    @app.get("/")
    def index() -> WerkzeugResponse:
        return redirect(url_for("roles"))

    @app.get("/roles")
    def roles() -> str:
        flt, active = read_filter(request.args)
        total = store.count_roles(flt)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        page = min(max(page, 1), pages)
        rows = store.list_roles(flt, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        here_args: dict[str, Any] = {**active}
        if page > 1:
            here_args["page"] = page
        here = url_for("roles", **here_args)
        return render_template(
            "roles.html",
            roles=rows,
            total=total,
            page=page,
            pages=pages,
            filters=active,
            here=here,
            last_success=store.last_successful_run(),
        )

    @app.post("/roles/status")
    def set_status() -> WerkzeugResponse | tuple[Response, int] | Response:
        ids = request.form.getlist("ids")
        status = request.form.get("status", "")
        # A background save from the roles list asks for JSON so the page does not reload.
        background = request.headers.get("X-Panel") == "1"
        target = safe_next(request.form.get("next"), url_for("roles"))
        error = ""
        count = 0
        if not ids:
            error = "Select at least one role."
        else:
            try:
                count = store.update_status(ids, status)
            except StoreError as exc:
                error = str(exc)
        if background:
            if error:
                return jsonify(error=error), 400
            applied = {}
            for key in dict.fromkeys(ids):
                portal, _, job_id = key.partition("|")
                role = store.get_role(portal, job_id)
                if role is not None:
                    applied[key] = _role_values(role)["applied_on"]
            return jsonify(status=status, applied_on=applied)
        flash(error or f"{count} role(s) set to {status}.", "error" if error else "ok")
        return redirect(target)

    @app.get("/roles/<portal>/<job_id>")
    def role_page(portal: str, job_id: str) -> str:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        # ?partial=1 returns just the form, for the side panel on the roles list.
        template = "_role_form.html" if request.args.get("partial") else "role.html"
        return render_template(
            template,
            role=role,
            form=_role_values(role),
            errors={},
            next=safe_next(request.args.get("next"), url_for("roles")),
        )

    @app.post("/roles/<portal>/<job_id>")
    def role_save(portal: str, job_id: str) -> str | Response | WerkzeugResponse | tuple[str, int]:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        target = safe_next(request.form.get("next"), url_for("roles"))
        edit, errors = parse_role_form(request.form, now_ist())
        panel = request.headers.get("X-Panel") == "1"
        if edit is None:
            template = "_role_form.html" if panel else "role.html"
            return (
                render_template(
                    template, role=role, form=dict(request.form), errors=errors, next=target
                ),
                400,
            )
        store.update_details(
            portal,
            job_id,
            status=edit.status,
            applied_on=edit.applied_on,
            contact_person=edit.contact_person,
            comments=edit.comments,
        )
        if panel:
            saved = store.get_role(portal, job_id)
            assert saved is not None
            return jsonify(status=saved.status, applied_on=_role_values(saved)["applied_on"])
        flash("Saved.", "ok")
        return redirect(target)

    @app.get("/export.csv")
    def export_csv() -> Response:
        flt, _ = read_filter(request.args)
        body = roles_to_csv(store.list_roles(flt))
        stamp = now_ist().strftime("%Y-%m-%d_%H%M")
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="roles_{stamp}.csv"'},
        )

    # ---- summary --------------------------------------------------------------------------

    @app.get("/summary")
    def summary() -> str:
        days = store.fetch_summary()
        totals = {status: sum(d.by_status[status] for d in days) for status in STATUSES}
        return render_template(
            "summary.html",
            days=days,
            workflow=STATUSES,
            totals=totals,
            grand_total=sum(d.total for d in days),
            last_success=store.last_successful_run(),
        )

    # ---- run log --------------------------------------------------------------------------

    @app.get("/runs")
    def runs() -> str:
        all_runs = store.list_runs(30)
        selected = None
        raw = request.args.get("run", "")
        if raw.isdigit():
            selected = store.get_run(int(raw))
        if selected is None and all_runs:
            selected = all_runs[0]
        detail: dict[str, Any] = {}
        if selected is not None:
            depth = store.depth_stats(selected.id)
            top = max((d.cards for d in depth), default=0)
            detail = {
                "keywords": store.keyword_records(selected.id),
                "depth": depth,
                "depth_top": top,
                "phrases": store.phrase_matches(selected.id),
                "skipped": store.list_skipped(selected.id, limit=200),
                "precision": (selected.matched / selected.cards_seen)
                if selected.cards_seen
                else None,
            }
        return render_template(
            "runs.html",
            runs=all_runs,
            selected=selected,
            last_success=store.last_successful_run(),
            **detail,
        )

    # ---- config ---------------------------------------------------------------------------

    def render_config(
        raw: Mapping[str, str],
        phrases: list[tuple[str, bool, bool]],
        errors: Mapping[str, str],
        status: int = 200,
    ) -> tuple[str, int]:
        rows = [*phrases, *[("", True, True)] * NEW_PHRASE_ROWS]
        return (
            render_template(
                "config.html",
                groups=SETTING_GROUPS,
                values=raw,
                phrases=rows,
                errors=errors,
                match_modes=MATCH_MODES,
            ),
            status,
        )

    @app.get("/config")
    def config_page() -> tuple[str, int]:
        try:
            current = Config.from_raw(store.get_config_raw())
        except ConfigError:
            current = Config()
            flash("Stored settings were invalid; showing defaults. Saving replaces them.", "error")
        stored = [(p.phrase, p.use_as_keyword, p.use_for_match) for p in store.list_phrases()]
        return render_config(current.to_raw(), stored, {})

    @app.post("/config")
    def config_save() -> tuple[str, int] | WerkzeugResponse:
        form = parse_config_form(request.form)
        if form.errors:
            return render_config(form.raw, form.phrases, form.errors, 400)
        try:
            with store.transaction():
                store.set_config_raw(Config.from_raw(form.raw).to_raw())
                store.replace_phrases(form.phrases)
        except StoreError as exc:
            return render_config(form.raw, form.phrases, {"phrases": str(exc)}, 400)
        flash("Configuration saved. It applies from the next scan.", "ok")
        return redirect(url_for("config_page"))

    return app


def _role_values(role: Role) -> dict[str, str]:
    return {
        "status": role.status,
        "applied_on": format_display(role.applied_on, with_time=False),
        "contact_person": role.contact_person,
        "comments": role.comments,
    }
