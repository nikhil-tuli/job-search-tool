"""Flask application: roles list and edit, run log, configuration, CSV export."""

from __future__ import annotations

import math
import os
import re
import secrets
from collections.abc import Callable, Collection, Mapping
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

from ..config import LIST_SETTINGS, Config, ConfigError, split_list
from ..cv import MAX_UPLOAD_BYTES, CvError, extract_text, sha_of
from ..embeddings import API_KEY_ENV, Embedder
from ..evaluation import build_report, load_labelled
from ..fit_filters import FitResult, FitRules, evaluate
from ..matching import MATCH_MODES
from ..models import (
    LABEL_GOOD,
    SORTED_STATUSES,
    STATUSES,
    Cv,
    Role,
    RoleFilter,
)
from ..scoring import (
    DAILY_WINDOW,
    TRIGGER_ROLE,
    Scorer,
    ScoringUnavailable,
    cv_chunks,
    gemini_from_env,
    plan_scoring,
)
from ..store import Store, StoreError
from ..timeutil import (
    format_display,
    from_iso,
    has_time_of_day,
    is_day,
    now_ist,
    to_iso,
    weekday_short,
)
from .forms import (
    NEW_PHRASE_ROWS,
    parse_config_form,
    parse_role_form,
    roles_to_csv,
    safe_next,
)
from .scores import ScoreView, score_views

PAGE_SIZE = 100
RATING_CHOICES = ("good", "bad", "clear")
# What the Eval page can add to the Fit filters: (setting, allowed value, how to say it).
EXCLUDE_KINDS = {
    "title": ("fit_deny_keywords", re.compile(r"[a-z][a-z0-9]{2,}"), "a title deny word"),
    "industry": (
        "fit_excluded_industries",
        re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'/().-]{1,78}[A-Za-z0-9).]"),
        "an excluded industry",
    ),
}
FIT_CHOICES = ("eligible", "excluded")
SCORE_CHOICES = ("strong", "partial", "weak", "unscored")
SORT_CHOICES = ("score",)
SCORE_WAIT_SECONDS = 15.0  # the app handles one request at a time, so a button must not wait long
TRIGGER_LABELS = {"scan": "After a scan", "manual": "jobscan score", "role": "Score this role"}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
SAFE_SITES = frozenset({"same-origin", "none"})

# (field, label, hint) in the order they appear on the Config page.
GROUP_INFO = {
    "Scoring": (
        "Scores are saved as raw keyword and meaning values. The weights, floors and bands here "
        "apply as soon as you save and refresh, with no new run. Chunk weights, top matches, "
        "chunk size and the chunk cap change the saved values, so roles are re-scored at the "
        "next scan or jobscan score, using stored embeddings (no new API calls)."
    ),
    "Fit filters": (
        "Fit filters apply immediately. Save, then refresh the Roles tab and every role is "
        "re-checked against these values. No scan is needed, and nothing is stored or hidden. "
        "Search and pacing settings still apply from the next scan."
    ),
}

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
        "Job descriptions",
        (
            (
                "detail_max_per_run",
                "Descriptions to fetch per scan",
                "One extra request each. 0 turns description fetching off",
            ),
            (
                "detail_max_age_days",
                "Only for roles fetched in the last (days)",
                "Older roles without a description are left alone",
            ),
        ),
    ),
    (
        "Fit filters",
        (
            (
                "fit_recency_days",
                "Only roles fetched in the last (days)",
                "Older roles are tagged Excluded: recency",
            ),
            (
                "fit_min_years",
                "Minimum years of experience asked",
                "Roles asking for fewer are excluded. 0 turns it off. Roles naming no years stay",
            ),
            (
                "fit_junior_words",
                "Junior words in the title",
                "Comma separated. Associate Director and similar senior titles are not caught",
            ),
            (
                "fit_junior_levels",
                "Junior LinkedIn levels",
                "Comma separated, matched against the Seniority level shown on the role",
            ),
            (
                "fit_excluded_companies",
                "Excluded companies",
                "Comma separated. Amazon also catches Amazon Development Centre",
            ),
            (
                "fit_skip_statuses",
                "Skip roles with these statuses",
                "Comma separated, from the status list",
            ),
            (
                "fit_deny_keywords",
                "Deny keywords in the title",
                "Comma separated. Whole words or phrases, title only",
            ),
            (
                "fit_excluded_industries",
                "Excluded industries",
                "Comma separated, as LinkedIn names them (for example Staffing and Recruiting). "
                "Needs the role's description to have been fetched",
            ),
        ),
    ),
    (
        "Scoring",
        (
            (
                "score_after_scan",
                "Score after each scan",
                "1 scores new roles at the end of a scan, 0 only on demand",
            ),
            (
                "score_weight_keyword",
                "Keyword share of the score (%)",
                "The rest is the meaning (embedding) score. 0 to 100",
            ),
            (
                "score_weight_requirement",
                "Chunk weight: requirements",
                "Matches on requirement text count this much",
            ),
            (
                "score_weight_responsibility",
                "Chunk weight: responsibilities",
                "Also used for text before any heading",
            ),
            (
                "score_weight_company",
                "Chunk weight: company text",
                "0 skips company text. Above 0 embeds it too",
            ),
            (
                "score_top_k",
                "Top CV matches per chunk",
                "1 uses only the closest CV chunk. 2 or 3 averages the closest few",
            ),
            (
                "score_chunk_words",
                "Target chunk size (words)",
                "Neighbouring bullets are packed to about this size. Smaller means more API calls",
            ),
            ("score_max_chunks", "Most chunks per role", "Requirement chunks are kept first"),
            (
                "score_kw_floor",
                "Keyword coverage worth 0",
                "Raw coverage at or below this scores 0",
            ),
            (
                "score_kw_ceiling",
                "Keyword coverage worth 100",
                "Raw coverage at or above this scores 100",
            ),
            (
                "score_sem_floor",
                "Similarity worth 0",
                "Raw cosine similarity at or below this scores 0",
            ),
            (
                "score_sem_ceiling",
                "Similarity worth 100",
                "Raw cosine similarity at or above this scores 100",
            ),
            ("score_strong_min", "Strong fit from", "Scores at or above this are Strong"),
            (
                "score_partial_min",
                "Partial fit from",
                "Scores from here up to Strong are Partial, below are Weak",
            ),
        ),
    ),
    (
        "Embeddings (Gemini)",
        (
            (
                "embed_model",
                "Model",
                "Changing it re-embeds everything. Vectors of different models are never mixed",
            ),
            (
                "embed_dims",
                "Vector size",
                "Numbers kept per vector (768 of 3072). Changing it re-embeds",
            ),
            (
                "embed_texts_per_minute",
                "Texts per minute",
                "Google counts each text as one request. Free tier shows 100 a minute, kept below",
            ),
            (
                "embed_tokens_per_minute",
                "Tokens per minute",
                "Free tier shows 30,000; kept below it",
            ),
            (
                "embed_daily_limit",
                "Texts per 24 hours",
                "Google counts each text as one request. Free tier shows 1,000 a day",
            ),
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


def label_note(role: Role) -> str:
    """Says whether the eval counts this role as good or bad, and why."""
    verdict = "a good fit" if role.label == LABEL_GOOD else "a poor fit"
    if role.fit_rating is not None:
        return f"Counted in the eval as {verdict}, from your rating."
    if role.label is not None:
        return f"Counted in the eval as {verdict}, from its status ({role.status})."
    return "Not counted in the eval. Rate it, or set a status such as Applied or Not relevant."


def create_app(
    store: Store,
    *,
    secret_key: str | None = None,
    allowed_hosts: Collection[str] = LOOPBACK_HOSTS,
    make_embedder: Callable[[Config], Embedder] = gemini_from_env,
) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secret_key or secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 512 * 1024  # the file plus form fields
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
    @app.errorhandler(413)
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

    @app.template_filter("duration")
    def duration(run: Any) -> str:
        if not run.finished_on:
            return "running"
        seconds = round((from_iso(run.finished_on) - from_iso(run.started_on)).total_seconds())
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"

    @app.template_filter("trigger")
    def trigger(value: str) -> str:
        return TRIGGER_LABELS.get(value, value)

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
        fit = args.get("fit", "")
        score = args.get("score", "")
        sort = args.get("sort", "")
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
            "fit": fit if fit in FIT_CHOICES else "",
            "score": score if score in SCORE_CHOICES else "",
            "sort": sort if sort in SORT_CHOICES else "",
        }
        return flt, {k: v for k, v in shown.items() if v}

    @app.get("/")
    def index() -> WerkzeugResponse:
        return redirect(url_for("roles"))

    def current_config() -> Config:
        try:
            return Config.from_raw(store.get_config_raw())
        except ConfigError:
            return Config()

    def fit_filtered(rows: list[Role], fit: str) -> tuple[list[Role], dict[str, FitResult]]:
        """Fit result for every row, and only the rows matching the Fit choice when one is set."""
        rules, now = FitRules.from_config(current_config()), now_ist()
        results = {r.key: evaluate(r, rules, now) for r in rows}
        if fit in FIT_CHOICES:
            wanted = fit == "eligible"
            rows = [r for r in rows if results[r.key].eligible == wanted]
        return rows, results

    def scored_filtered(
        rows: list[Role], views: Mapping[str, ScoreView], choice: str, sort: str
    ) -> list[Role]:
        """Rows with the chosen score band (or none), best fit first when asked."""
        if choice == "unscored":
            rows = [r for r in rows if r.key not in views]
        elif choice:
            rows = [r for r in rows if r.key in views and views[r.key].band == choice]
        if sort == "score":
            # Stable, so equal scores keep the newest-first order. Unscored roles go last.
            rows = sorted(rows, key=lambda r: -views[r.key].final if r.key in views else 1)
        return rows

    @app.get("/roles")
    def roles() -> str:
        flt, active = read_filter(request.args)
        fit, score_choice, sort = (active.get(k, "") for k in ("fit", "score", "sort"))
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        config, cv = current_config(), store.get_cv()
        if fit or score_choice or sort:
            # Fit and score are worked out in Python, so filter and sort first, then cut the page.
            filtered, fits = fit_filtered(store.list_roles(flt), fit)
            views = score_views(store, config, cv, filtered)
            matching = scored_filtered(filtered, views, score_choice, sort)
            total = len(matching)
            pages = max(1, math.ceil(total / PAGE_SIZE))
            page = min(max(page, 1), pages)
            rows = matching[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        else:
            total = store.count_roles(flt)
            pages = max(1, math.ceil(total / PAGE_SIZE))
            page = min(max(page, 1), pages)
            rows, fits = fit_filtered(
                store.list_roles(flt, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE), fit
            )
            views = score_views(store, config, cv, rows)
        here_args: dict[str, Any] = {**active}
        if page > 1:
            here_args["page"] = page
        here = url_for("roles", **here_args)
        return render_template(
            "roles.html",
            roles=rows,
            fits=fits,
            scores=views,
            has_cv=cv is not None,
            total=total,
            page=page,
            pages=pages,
            filters=active,
            advanced_open=any(k in active for k in ("fit", "score", "sort")),
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

    def role_context(role: Role) -> dict[str, Any]:
        config, cv = current_config(), store.get_cv()
        return {
            "role": role,
            "fit": evaluate(role, FitRules.from_config(config), now_ist()),
            "score": score_views(store, config, cv, [role]).get(role.key),
            "has_cv": cv is not None,
            "label_note": label_note(role),
        }

    def score_one(role: Role) -> tuple[str, ScoreView | None]:
        """Score one role now, whatever its fit result. Returns a message and the new score."""
        config = current_config()
        scorer = Scorer(
            store=store,
            config=config,
            make_embedder=make_embedder,
            emit=lambda _line: None,
            max_wait=SCORE_WAIT_SECONDS,
        )
        try:
            run = scorer.run(TRIGGER_ROLE, only=[role])
        except ScoringUnavailable:
            return "Upload your CV on the CV page first.", None
        if run.scored:
            view = score_views(store, config, store.get_cv(), [role])[role.key]
            return f"Scored {view.final} ({view.band} fit).", view
        if run.no_description:
            return "This role has no description yet, so there is nothing to score.", None
        return run.note or "Could not score this role.", None

    @app.get("/roles/<portal>/<job_id>")
    def role_page(portal: str, job_id: str) -> str:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        # ?partial=1 returns just the form, for the side panel on the roles list.
        template = "_role_form.html" if request.args.get("partial") else "role.html"
        return render_template(
            template,
            **role_context(role),
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
                    template,
                    **role_context(role),
                    form=dict(request.form),
                    errors=errors,
                    next=target,
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
            return jsonify(
                status=saved.status,
                applied_on=_role_values(saved)["applied_on"],
                note=label_note(saved),
            )
        flash("Saved.", "ok")
        return redirect(target)

    @app.post("/roles/<portal>/<job_id>/score")
    def role_score(portal: str, job_id: str) -> WerkzeugResponse | tuple[Response, int] | Response:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        message, view = score_one(role)
        if request.headers.get("X-Panel") == "1":
            if view is None:
                return jsonify(message=message), 400
            return jsonify(
                message=message,
                score={
                    "final": view.final,
                    "band": view.band,
                    "detail": f"Keywords {view.keyword}, meaning {view.semantic}, "
                    f"{view.chunks} chunks, scored {format_display(view.scored_on)}",
                },
            )
        flash(message, "ok" if view else "error")
        return redirect(safe_next(request.form.get("next"), url_for("roles")))

    @app.post("/roles/<portal>/<job_id>/rating")
    def role_rating(portal: str, job_id: str) -> WerkzeugResponse | Response:
        role = store.get_role(portal, job_id)
        if role is None:
            abort(404, "Role not found")
        choice = request.form.get("rating", "")
        if choice not in RATING_CHOICES:
            abort(400, "Unknown rating")
        store.set_fit_rating(portal, job_id, None if choice == "clear" else choice)
        saved = store.get_role(portal, job_id)
        assert saved is not None
        if request.headers.get("X-Panel") == "1":
            return jsonify(rating=saved.fit_rating or "", note=label_note(saved))
        flash(label_note(saved), "ok")
        return redirect(safe_next(request.form.get("next"), url_for("roles")))

    # ---- eval -----------------------------------------------------------------------------

    @app.get("/eval")
    def eval_page() -> str:
        config, cv = current_config(), store.get_cv()
        phrases = [p.phrase for p in store.list_phrases() if p.use_for_match]
        report = build_report(load_labelled(store, config, cv), config, phrases)
        return render_template("eval.html", report=report, has_cv=cv is not None)

    @app.post("/eval/exclude")
    def eval_exclude() -> WerkzeugResponse:
        """Add a suggested title word or industry to the matching Fit filter list."""
        kind = request.form.get("kind", "")
        if kind not in EXCLUDE_KINDS:
            abort(400, "Unknown kind")
        setting, pattern, noun = EXCLUDE_KINDS[kind]
        value = " ".join(request.form.get("value", "").split())
        if not pattern.fullmatch(value):
            abort(400, "Not a usable value")
        config = current_config()
        items = list(split_list(getattr(config, setting)))
        if value.casefold() not in {item.casefold() for item in items}:
            raw = config.to_raw()
            raw[setting] = ", ".join([*items, value])
            try:
                store.set_config_raw(Config.from_raw(raw).to_raw())
            except ConfigError as exc:
                flash(next(iter(exc.errors.values())), "error")
                return redirect(url_for("eval_page"))
        flash(f'"{value}" is now {noun}. Fit filters apply at once.', "ok")
        return redirect(url_for("eval_page"))

    @app.get("/export.csv")
    def export_csv() -> Response:
        flt, active = read_filter(request.args)
        rows, _ = fit_filtered(store.list_roles(flt), active.get("fit", ""))
        views = score_views(store, current_config(), store.get_cv(), rows)
        rows = scored_filtered(rows, views, active.get("score", ""), active.get("sort", ""))
        body = roles_to_csv(rows, views)
        stamp = now_ist().strftime("%Y-%m-%d_%H%M")
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="roles_{stamp}.csv"'},
        )

    # ---- CV -------------------------------------------------------------------------------

    def render_cv(error: str = "", status: int = 200) -> tuple[str, int]:
        config, cv = current_config(), store.get_cv()
        pending = plan_scoring(store, config, cv, now_ist()) if cv else None
        return (
            render_template(
                "cv.html",
                cv=cv,
                words=len(cv.text.split()) if cv else 0,
                chunks=[text for text, _ in store.get_cv_chunks()],
                pending=len(pending.selected) if pending else 0,
                key_set=bool(os.environ.get(API_KEY_ENV)),
                key_name=API_KEY_ENV,
                max_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
                error=error,
            ),
            status,
        )

    @app.get("/cv")
    def cv_page() -> tuple[str, int]:
        return render_cv()

    @app.post("/cv")
    def cv_upload() -> tuple[str, int] | WerkzeugResponse:
        upload = request.files.get("cv")
        if upload is None or not upload.filename:
            return render_cv("Choose a file to upload.", 400)
        name = os.path.basename(upload.filename.replace("\\", "/"))[:200]
        try:
            text = extract_text(name, upload.read(MAX_UPLOAD_BYTES + 1))
            chunks = cv_chunks(text)
        except CvError as exc:
            return render_cv(str(exc), 400)
        if not chunks:
            return render_cv("No usable text was found in this file.", 400)
        existing = store.get_cv()
        if existing is not None and existing.sha == sha_of(text):
            flash("This is the same CV as before, so nothing changed.", "ok")
            return redirect(url_for("cv_page"))
        cv = Cv(filename=name, text=text, sha=sha_of(text), uploaded_on=to_iso(now_ist()))
        store.save_cv(cv, chunks)
        waiting = len(plan_scoring(store, current_config(), cv, now_ist()).selected)
        flash(
            f"CV saved as {len(chunks)} chunks. {waiting} roles will be scored at the next scan, "
            "or run jobscan score in a terminal to do it now.",
            "ok",
        )
        return redirect(url_for("cv_page"))

    # ---- summary --------------------------------------------------------------------------

    def scoring_context() -> dict[str, Any]:
        config, cv = current_config(), store.get_cv()
        history = store.list_scoring_runs(10)
        waiting = plan_scoring(store, config, cv, now_ist()) if cv else None
        return {
            "cv": cv,
            "score_runs": history,
            "score_latest": history[0] if history else None,
            "score_waiting": len(waiting.selected) if waiting else 0,
            "daily_used": store.texts_embedded_since(to_iso(now_ist() - DAILY_WINDOW)),
            "daily_limit": config.embed_daily_limit,
        }

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
            **scoring_context(),
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
                group_info=GROUP_INFO,
                list_settings=LIST_SETTINGS,
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
