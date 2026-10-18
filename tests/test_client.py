from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from jobscan.client import (
    SEARCH_URL,
    LinkedInClient,
    Outcome,
    build_params,
    classify_response,
)
from jobscan.config import Config
from tests.helpers import FakeCard, page_html, stub_html


@pytest.mark.parametrize("status", [429, 403, 999, 500, 502, 503])
def test_throttle_statuses(status: int) -> None:
    result = classify_response(status, stub_html())
    assert result.outcome is Outcome.THROTTLED
    assert str(status) in result.detail


@pytest.mark.parametrize("status", [301, 302, 401, 404])
def test_unexpected_statuses_are_treated_as_transient(status: int) -> None:
    assert classify_response(status, "").outcome is Outcome.THROTTLED


def test_400_is_end_of_results() -> None:
    assert classify_response(400, "").outcome is Outcome.END


@pytest.mark.parametrize("body", ["", "   \n"])
def test_empty_200_is_end(body: str) -> None:
    assert classify_response(200, body).outcome is Outcome.END


def test_200_with_cards_is_ok() -> None:
    result = classify_response(200, page_html([FakeCard("1", "TPM"), FakeCard("2", "TPM")]))
    assert result.outcome is Outcome.OK
    assert result.page is not None
    assert [c.job_id for c in result.page.cards] == ["1", "2"]


def test_200_stub_page_is_throttled() -> None:
    assert classify_response(200, stub_html()).outcome is Outcome.THROTTLED


def test_200_meta_refresh_small_page_is_throttled() -> None:
    body = '<html><head><meta http-equiv="refresh" content="1;url=https://x"></head></html>'
    assert classify_response(200, body).outcome is Outcome.THROTTLED


def test_200_job_ids_without_parseable_cards_is_layout_change() -> None:
    body = '<li><div class="job" data-entity-urn="urn:li:jobPosting:123">Role</div></li>'
    assert classify_response(200, body).outcome is Outcome.LAYOUT_CHANGED


def test_200_card_container_without_title_is_layout_change() -> None:
    body = page_html([FakeCard("5", "TPM")]).replace("TPM", "")
    assert classify_response(200, body).outcome is Outcome.LAYOUT_CHANGED


def test_small_page_without_cards_is_end_with_warning() -> None:
    result = classify_response(200, "<html><body>No matching jobs found</body></html>")
    assert result.outcome is Outcome.END
    assert "no cards" in result.warning


def test_build_params_matches_verified_request_shape() -> None:
    params = build_params(Config(), "Technical Program Manager", 3)
    assert params == {
        "keywords": "Technical Program Manager",
        "location": "India",
        "geoId": "102713980",
        "f_TPR": "r86400",
        "start": 30,
    }


# ---- real HTTP against a local server ---------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, list[str]]]] = []
    responses: ClassVar[dict[int, tuple[int, str]]] = {}

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        _Handler.seen.append(query)
        start = int(query.get("start", ["0"])[0])
        status, body = _Handler.responses.get(start, (400, ""))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.seen = []
    _Handler.responses = {
        0: (200, page_html([FakeCard("1", "Technical Program Manager")])),
        10: (429, stub_html()),
    }
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/search"
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_client_over_http(server: str) -> None:
    client = LinkedInClient(Config(), base_url=server)
    try:
        ok = client.fetch_page("Technical Program Manager", 0)
        throttled = client.fetch_page("Technical Program Manager", 1)
        end = client.fetch_page("Technical Program Manager", 2)
    finally:
        client.close()
    assert ok.outcome is Outcome.OK
    assert throttled.outcome is Outcome.THROTTLED
    assert end.outcome is Outcome.END
    first = _Handler.seen[0]
    assert first["keywords"] == ["Technical Program Manager"]
    assert first["geoId"] == ["102713980"]
    assert first["f_TPR"] == ["r86400"]
    assert [q["start"] for q in _Handler.seen] == [["0"], ["10"], ["20"]]


def test_network_error_is_throttled_not_raised() -> None:
    client = LinkedInClient(Config(), base_url="http://127.0.0.1:1/search", timeout=1)
    result = client.fetch_page("TPM", 0)
    client.close()
    assert result.outcome is Outcome.THROTTLED
    assert "network error" in result.detail


def test_timeout_is_throttled() -> None:
    class Slow(requests.Session):
        def get(self, *args: object, **kwargs: object) -> requests.Response:  # type: ignore[override]
            raise requests.Timeout("slow")

    client = LinkedInClient(Config(), session=Slow())
    assert client.fetch_page("TPM", 0).outcome is Outcome.THROTTLED


def test_default_url_is_the_guest_endpoint() -> None:
    assert SEARCH_URL.endswith("/jobs-guest/jobs/api/seeMoreJobPostings/search")
