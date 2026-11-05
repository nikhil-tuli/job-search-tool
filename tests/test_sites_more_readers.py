"""SmartRecruiters, Oracle Recruiting and Keka: recognising their links and reading their lists."""

from __future__ import annotations

from typing import Any

import pytest

from jobscan.sites_readers import (
    Detected,
    KekaReader,
    OracleReader,
    SiteError,
    SmartRecruitersReader,
    board_label,
    detect,
    detect_from_page,
    make_reader,
    page_candidates,
    valid_board,
)

TPM = "Technical Program Manager"
ORACLE_HOST = "eeho.fa.us2.oraclecloud.com"


class Net:
    """A fake network: a URL maps to JSON, text or an exception to raise."""

    def __init__(self, gets: dict[str, Any]) -> None:
        self.gets = gets
        self.calls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.calls.append(url)
        if url not in self.gets:
            raise SiteError("missing")
        value = self.gets[url]
        if isinstance(value, Exception):
            raise value
        return value

    def get_text(self, url: str) -> str:
        return str(self.get_json(url))

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        raise SiteError("missing")


@pytest.mark.parametrize(
    ("url", "system", "board"),
    [
        ("https://jobs.smartrecruiters.com/Experian", "smartrecruiters", "Experian"),
        ("https://careers.smartrecruiters.com/Freshworks/", "smartrecruiters", "Freshworks"),
        (
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Nielsen/publication/1",
            "smartrecruiters",
            "Nielsen",
        ),
        (
            "https://api.smartrecruiters.com/v1/companies/Experian/postings",
            "smartrecruiters",
            "Experian",
        ),
        ("https://api.smartrecruiters.com/v1/", "", ""),
        (
            f"https://{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_45001/jobs",
            "oracle",
            f"{ORACLE_HOST}|CX_45001",
        ),
        (f"https://{ORACLE_HOST}/", "", ""),
        ("https://gokwik.keka.com/careers/", "keka", "gokwik.keka.com"),
        ("https://www.keka.com/careers", "", ""),
        ("https://help.keka.com/admin/x", "", ""),
    ],
)
def test_detect_new_systems(url: str, system: str, board: str) -> None:
    found = detect(url)
    assert (found.system, found.board) == (system, board)


def test_board_labels_and_validity() -> None:
    assert board_label("smartrecruiters", "Experian") == "Experian"
    assert board_label("oracle", f"{ORACLE_HOST}|CX_45001") == "eeho, CX_45001"
    assert board_label("keka", "gokwik.keka.com") == "gokwik"
    assert valid_board("oracle", f"{ORACLE_HOST}|CX_45001")
    assert not valid_board("smartrecruiters", "users")


def test_company_page_with_these_systems_is_recognised() -> None:
    page = (
        '<a href="https://jobs.smartrecruiters.com/Experian">Jobs</a>'
        f'<a href="https://{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/">Jobs</a>'
        '<a href="https://www.keka.com/careers">Keka itself</a>'
        '<a href="https://jupiter.keka.com/careers/">Careers</a>'
    )
    net = Net({"https://co.example/careers": page})
    found = page_candidates("https://co.example/careers", net)
    assert [(c.system, c.board) for c in found] == [
        ("smartrecruiters", "Experian"),
        ("oracle", f"{ORACLE_HOST}|CX_1"),
        ("keka", "jupiter.keka.com"),
    ]
    assert detect_from_page("https://co.example/careers", net).system == "smartrecruiters"


# ---- SmartRecruiters --------------------------------------------------------------------------

SR_API = "https://api.smartrecruiters.com/v1/companies/Acme/postings"


def sr_item(job_id: str, name: str = TPM, **extra: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name,
        "releasedDate": "2026-10-20T08:00:00.000Z",
        "company": {"identifier": "Acme"},
        "location": {"city": "Bengaluru", "region": "KA", "country": "in", "remote": False},
        **extra,
    }


def test_smartrecruiters_reads_list_per_keyword_and_describes() -> None:
    net = Net(
        {
            f"{SR_API}?limit=100&offset=0&q=Technical%20Program%20Manager": {
                "totalFound": 2,
                "content": [sr_item("1"), sr_item("2", location={"remote": True})],
            },
            f"{SR_API}?limit=100&offset=0&q=Delivery%20Manager": {
                "totalFound": 1,
                "content": [sr_item("1")],
            },
            f"{SR_API}/1": {
                "jobAd": {
                    "sections": {
                        "jobDescription": {"title": "Job", "text": "<p>Run programs</p>"},
                        "qualifications": {"title": "You", "text": "<ul><li>10 years</li></ul>"},
                    }
                }
            },
        }
    )
    reader = SmartRecruitersReader("Acme", net)
    jobs = reader.list_jobs([TPM, "Delivery Manager"], 100)
    assert [j.job_id for j in jobs] == ["1", "2"]
    first, second = jobs
    assert first.link == "https://jobs.smartrecruiters.com/Acme/1"
    assert first.location == "Bengaluru, KA, in"
    assert first.posted_on is not None
    assert first.posted_on.startswith("2026-10-20")
    assert second.location == "Remote"
    text = reader.describe(first).description or ""
    assert "Run programs" in text
    assert "- 10 years" in text


def test_smartrecruiters_pages_until_the_total() -> None:
    page_one = [sr_item(str(n)) for n in range(100)]
    net = Net(
        {
            f"{SR_API}?limit=100&offset=0": {"totalFound": 101, "content": page_one},
            f"{SR_API}?limit=100&offset=100": {"totalFound": 101, "content": [sr_item("x")]},
        }
    )
    assert len(SmartRecruitersReader("Acme", net).list_jobs([], 500)) == 101


def test_smartrecruiters_unknown_company_is_an_error_but_no_match_is_not() -> None:
    q = f"{SR_API}?limit=100&offset=0&q=Technical%20Program%20Manager"
    unknown = Net(
        {
            q: {"totalFound": 0, "content": []},
            f"{SR_API}?limit=1&offset=0": {"totalFound": 0, "content": []},
        }
    )
    with pytest.raises(SiteError, match="no jobs under this company name"):
        SmartRecruitersReader("Acme", unknown).list_jobs([TPM], 100)
    quiet = Net(
        {
            q: {"totalFound": 0, "content": []},
            f"{SR_API}?limit=1&offset=0": {"totalFound": 40, "content": [sr_item("9")]},
        }
    )
    assert SmartRecruitersReader("Acme", quiet).list_jobs([TPM], 100) == []


def test_smartrecruiters_bad_shapes_are_plain_errors() -> None:
    net = Net({f"{SR_API}?limit=100&offset=0": {"oops": 1}, f"{SR_API}/1": {"x": 1}})
    with pytest.raises(SiteError, match="expected form"):
        SmartRecruitersReader("Acme", net).list_jobs([], 10)
    with pytest.raises(SiteError, match="expected form"):
        SmartRecruitersReader("Acme", net).describe(
            SmartRecruitersReader("Acme", net)._job(sr_item("1"))  # type: ignore[arg-type]
        )


# ---- Oracle Recruiting ------------------------------------------------------------------------


def oracle_url(keyword: str = "", offset: int = 0) -> str:
    finder = f"findReqs;siteNumber=CX_1,limit=25,offset={offset},sortBy=POSTING_DATES_DESC"
    if keyword:
        finder += f",keyword={keyword}"
    return (
        f"https://{ORACLE_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&expand=requisitionList&finder={finder}"
    )


def oracle_item(job_id: str) -> dict[str, Any]:
    return {
        "Id": job_id,
        "Title": TPM,
        "PostedDate": "2026-10-21",
        "PrimaryLocation": "Pune, India",
    }


def test_oracle_reads_list_and_describes() -> None:
    detail_url = (
        f"https://{ORACLE_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        "?onlyData=true&expand=all&finder=ById;Id=%2277%22,siteNumber=CX_1"
    )
    net = Net(
        {
            oracle_url("Technical%20Program%20Manager"): {
                "items": [
                    {"TotalJobsCount": 2, "requisitionList": [oracle_item("77"), oracle_item("78")]}
                ]
            },
            detail_url: {
                "items": [
                    {
                        "ExternalDescriptionStr": "<p>Lead delivery</p>",
                        "PrimaryLocation": "Pune, Maharashtra, India",
                        "ExternalPostedStartDate": "2026-10-19",
                    }
                ]
            },
        }
    )
    reader = OracleReader(f"{ORACLE_HOST}|CX_1", net)
    jobs = reader.list_jobs([TPM], 100)
    assert [j.job_id for j in jobs] == ["77", "78"]
    first = jobs[0]
    assert first.title == TPM
    assert first.location == "Pune, India"
    assert first.link == f"https://{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/77"
    assert first.posted_on is not None
    assert first.posted_on.startswith("2026-10-21")
    detail = reader.describe(first.__class__(**{**first.__dict__, "job_id": "77"}))
    assert detail.description == "Lead delivery"
    assert detail.location == "Pune, Maharashtra, India"
    assert (detail.posted_on or "").startswith("2026-10-19")


def test_oracle_pages_and_empty_and_broken_answers() -> None:
    full = [oracle_item(str(n)) for n in range(25)]
    net = Net(
        {
            oracle_url(): {"items": [{"TotalJobsCount": 26, "requisitionList": full}]},
            oracle_url(offset=25): {
                "items": [{"TotalJobsCount": 26, "requisitionList": [oracle_item("z")]}]
            },
        }
    )
    assert len(OracleReader(f"{ORACLE_HOST}|CX_1", net).list_jobs([], 100)) == 26
    empty = Net({oracle_url(): {"items": []}})
    assert OracleReader(f"{ORACLE_HOST}|CX_1", empty).list_jobs([], 10) == []
    refused = Net({oracle_url(): SiteError("The company site refused the request (HTTP 400).")})
    with pytest.raises(SiteError, match="400"):
        OracleReader(f"{ORACLE_HOST}|CX_1", refused).list_jobs([], 10)
    broken = Net({oracle_url(): {"items": "nope"}})
    with pytest.raises(SiteError, match="expected form"):
        OracleReader(f"{ORACLE_HOST}|CX_1", broken).list_jobs([], 10)


# ---- Keka -------------------------------------------------------------------------------------

KEKA_URL = "https://gokwik.keka.com/careers/api/jobs/default/active"


def test_keka_reads_a_plain_list_or_a_wrapped_one() -> None:
    rows = [
        {
            "id": 39543,
            "title": TPM,
            "jobLocations": [{"city": "Bengaluru", "country": "India"}, "Remote"],
            "publishedOn": "2026-09-21T11:36:17.773Z",
            "description": "<p>Lead</p>",
        },
        {"jobId": "A1", "jobTitle": "Delivery Manager", "location": "Pune"},
        {"id": 5},  # no title: skipped
    ]
    for payload in (rows, {"data": rows}):
        reader = KekaReader("gokwik.keka.com", Net({KEKA_URL: payload}))
        jobs = reader.list_jobs([TPM], 100)
        assert [j.job_id for j in jobs] == ["39543", "A1"]
        assert jobs[0].location == "Bengaluru, India; Remote"
        assert jobs[0].link == "https://gokwik.keka.com/careers/39543"
        assert jobs[0].description == "Lead"
        assert (jobs[0].posted_on or "").startswith("2026-09-21")
        assert jobs[1].location == "Pune"
        assert reader.describe(jobs[0]).description == "Lead"


def test_keka_empty_is_fine_but_unreadable_rows_are_an_error() -> None:
    assert (
        KekaReader(
            "a.keka.com", Net({"https://a.keka.com/careers/api/jobs/default/active": []})
        ).list_jobs([], 10)
        == []
    )
    odd = Net({"https://a.keka.com/careers/api/jobs/default/active": [{"x": 1}]})
    with pytest.raises(SiteError, match="expected form"):
        KekaReader("a.keka.com", odd).list_jobs([], 10)
    html_page = Net({"https://a.keka.com/careers/api/jobs/default/active": "<html>"})
    with pytest.raises(SiteError, match="expected form"):
        KekaReader("a.keka.com", html_page).list_jobs([], 10)


def test_keka_portal_in_the_board() -> None:
    url = "https://a.keka.com/careers/api/jobs/campus/active"
    assert KekaReader("a.keka.com|campus", Net({url: []})).list_jobs([], 10) == []


def test_make_reader_knows_the_new_systems() -> None:
    net = Net({})
    assert isinstance(make_reader("smartrecruiters", "Acme", net), SmartRecruitersReader)
    assert isinstance(make_reader("oracle", f"{ORACLE_HOST}|CX_1", net), OracleReader)
    assert isinstance(make_reader("keka", "a.keka.com", net), KekaReader)
    assert Detected("keka", "a.keka.com").system == "keka"
