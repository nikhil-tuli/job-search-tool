from __future__ import annotations

import pytest

from jobscan.keywords import Corpus, CvTerms, coverage, terms

DOCS = [
    "Lead payment gateway integration programs with engineering teams",
    "Manage warehouse operations and inventory planning for retail stores",
    "Own cloud cost optimization and vendor management for platform teams",
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Managers managed managing teams", ["manager", "manag", "manag"]),
        ("Delivers delivered delivering", ["deliver", "deliver", "deliver"]),
        ("C++ and C# and R", ["c++", "c#", "r"]),
        ("the 2026 plan, with 10 years", ["plan"]),
        ("Strategies", ["strategy"]),
        ("", []),
    ],
)
def test_terms(text: str, expected: list[str]) -> None:
    assert terms(text) == expected


def test_a_rare_word_weighs_more_than_a_common_one() -> None:
    corpus = Corpus(["alpha beta", "alpha gamma", "alpha delta"])
    assert corpus.idf("beta") > corpus.idf("alpha")
    assert corpus.idf("never-seen") > corpus.idf("beta")


def test_idf_is_never_negative() -> None:
    corpus = Corpus(["alpha beta"] * 5)
    assert corpus.idf("alpha") > 0


def test_coverage_is_zero_without_overlap_and_grows_with_it() -> None:
    corpus = Corpus(DOCS)
    chunks = [(1.0, DOCS[0])]
    none = coverage(CvTerms("gardening and cooking"), corpus, chunks)
    some = coverage(CvTerms("payment integration"), corpus, chunks)
    more = coverage(CvTerms("payment gateway integration programs engineering"), corpus, chunks)
    assert none == 0.0
    assert 0 < some < more <= 1.0


def test_repeating_a_word_helps_but_saturates() -> None:
    corpus = Corpus(DOCS)
    chunks = [(1.0, "payment")]
    once = coverage(CvTerms("payment"), corpus, chunks)
    twice = coverage(CvTerms("payment payment"), corpus, chunks)
    thrice = coverage(CvTerms("payment payment payment"), corpus, chunks)
    assert once < twice < thrice < 1.0
    assert thrice - twice < twice - once


def test_requirement_words_count_more_than_responsibility_words() -> None:
    corpus = Corpus(DOCS)
    cv = CvTerms("kubernetes")
    chunks_a = [(2.0, "kubernetes"), (1.0, "negotiation")]
    chunks_b = [(1.0, "kubernetes"), (2.0, "negotiation")]
    assert coverage(cv, corpus, chunks_a) > coverage(cv, corpus, chunks_b)


def test_zero_weight_chunks_are_ignored() -> None:
    corpus = Corpus(DOCS)
    cv = CvTerms("payment")
    assert coverage(cv, corpus, [(1.0, "payment"), (0.0, "fiercely unrelated text")]) == coverage(
        cv, corpus, [(1.0, "payment")]
    )


def test_no_words_gives_zero() -> None:
    assert coverage(CvTerms("payment"), Corpus(DOCS), []) == 0.0
    assert coverage(CvTerms("payment"), Corpus(DOCS), [(1.0, "the and of")]) == 0.0


def test_a_word_takes_its_highest_chunk_weight_once() -> None:
    corpus = Corpus(DOCS)
    cv = CvTerms("kubernetes")
    twice = coverage(cv, corpus, [(2.0, "kubernetes"), (1.0, "kubernetes"), (1.0, "negotiation")])
    once = coverage(cv, corpus, [(2.0, "kubernetes"), (1.0, "negotiation")])
    assert twice == pytest.approx(once)
