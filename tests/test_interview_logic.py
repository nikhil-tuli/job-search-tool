"""Interview Prep logic: ratings, metrics, and the checks on model output."""

from __future__ import annotations

import json

import pytest

from jobscan.interview import feedback as fb
from jobscan.interview import metrics as mx
from jobscan.interview import rules as r

# ---- ratings at every boundary --------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("pace", 99.9, r.WEAK), ("pace", 100, r.NEEDS_WORK), ("pace", 119.9, r.NEEDS_WORK),
        ("pace", 120, r.GOOD), ("pace", 160, r.GOOD), ("pace", 160.1, r.NEEDS_WORK),
        ("pace", 180, r.NEEDS_WORK), ("pace", 180.1, r.WEAK),
        ("thinking", 3, r.GOOD), ("thinking", 3.1, r.NEEDS_WORK), ("thinking", 6, r.NEEDS_WORK),
        ("thinking", 6.1, r.WEAK),
        ("fillers", 2.9, r.GOOD), ("fillers", 3, r.NEEDS_WORK), ("fillers", 5, r.NEEDS_WORK),
        ("fillers", 5.1, r.WEAK),
        ("hedging", 1.9, r.GOOD), ("hedging", 2, r.NEEDS_WORK), ("hedging", 4, r.NEEDS_WORK),
        ("hedging", 4.1, r.WEAK),
        ("pauses", 0, r.GOOD), ("pauses", 1, r.NEEDS_WORK), ("pauses", 2, r.NEEDS_WORK),
        ("pauses", 3, r.WEAK),
        ("length", 29.9, r.WEAK), ("length", 30, r.NEEDS_WORK), ("length", 59.9, r.NEEDS_WORK),
        ("length", 60, r.GOOD), ("length", 120, r.GOOD), ("length", 120.1, r.NEEDS_WORK),
        ("length", 180, r.NEEDS_WORK), ("length", 180.1, r.WEAK),
        ("ownership", 50, r.GOOD), ("ownership", 49.9, r.NEEDS_WORK),
        ("ownership", 30, r.NEEDS_WORK), ("ownership", 29.9, r.WEAK),
    ],
)  # fmt: skip
def test_spoken_rating_boundaries(key: str, value: float, expected: str) -> None:
    assert r.ASPECT_BY_KEY[key].rate(value) == expected


def test_bands_and_rounding() -> None:
    assert [r.band_of(s) for s in (100, 80, 79, 65, 64, 50, 49, 0)] == [
        "Strong", "Strong", "Good", "Good", "Needs work", "Needs work", "Weak", "Weak",
    ]  # fmt: skip
    assert r.average_score([70, 82, 64, 55]) == 68  # the worked example in the requirements
    assert r.average_score([]) is None
    assert r.round_half_up(77.14) == 77


# ---- word counts ----------------------------------------------------------------------------


def test_fillers_and_hedges() -> None:
    text = "So basically I think we, um, kind of delivered it, you know. Maybe like twice."
    assert mx.count_fillers_and_hedges(text) == (4, 3)  # basically, um, you know, like; hedges


def test_like_is_not_a_filler_as_a_verb_or_comparison() -> None:
    assert mx.count_fillers_and_hedges("I like it. It looks like rain. I would like to go.")[0] == 0
    assert mx.count_fillers_and_hedges("It was, like, very big.")[0] == 1


def test_sort_of_counts_once_as_a_hedge_only() -> None:
    assert mx.count_fillers_and_hedges("It was sort of fine.") == (0, 1)


def test_ownership_words() -> None:
    assert mx.count_ownership("I led it. We shipped. I'm sure we're done. I've, I'd.") == (4, 2)
    assert mx.count_ownership("Our team did it.") == (0, 0)


# ---- one answer -----------------------------------------------------------------------------


def _timed(*pairs: tuple[float, float]) -> list[mx.TimedWord]:
    return [mx.TimedWord(a, b) for a, b in pairs]


def test_measure_answer_with_word_times() -> None:
    # 10 words, a 2.5 s gap (not speaking time, not a long pause) and a 3.5 s gap (both).
    times = _timed(
        (2.0, 2.4), (2.4, 2.8), (2.8, 3.2), (3.2, 3.6), (6.1, 6.5),  # gap 2.5
        (6.5, 6.9), (6.9, 7.3), (10.8, 11.2), (11.2, 11.6), (11.6, 12.0),  # gap 3.5
    )  # fmt: skip
    a = mx.measure_answer("I led the team and we did well but I think it ended.", times,
                          duration_s=13.0, exact=True)  # fmt: skip
    assert a.first_word_s == 2.0
    assert a.long_pauses == 1
    assert a.speaking_s == pytest.approx(10.0 - 2.5 - 3.5)
    assert a.hedges == 1
    assert a.i_count == 2
    assert a.we_count == 1


def test_measure_answer_without_times_keeps_counts_only() -> None:
    a = mx.measure_answer("um I did it", [], duration_s=20.0, exact=False)
    assert a.speaking_s is None
    assert a.long_pauses is None
    assert a.first_word_s is None
    assert a.fillers == 1
    assert a.words == 4


def test_answer_measure_round_trips() -> None:
    a = mx.measure_answer("I did it well", _timed((1, 1.5), (1.5, 2)), duration_s=5, exact=True)
    assert mx.AnswerMeasure.from_dict(a.to_dict()) == a


def test_scorable_needs_ten_words() -> None:
    assert not mx.is_scorable(9)
    assert mx.is_scorable(10)


# ---- the spoken rubric ----------------------------------------------------------------------


def _answer(**kw: object) -> mx.AnswerMeasure:
    base: dict[str, object] = {
        "words": 100, "fillers": 0, "hedges": 0, "i_count": 5, "we_count": 0, "duration_s": 85.0,
        "speaking_s": 40.0, "first_word_s": 2.5, "long_pauses": 0, "exact": True,
    }  # fmt: skip
    base.update(kw)
    return mx.AnswerMeasure(**base)  # type: ignore[arg-type]


def test_spoken_result_matches_the_worked_example() -> None:
    # pace 175 wpm, thinking 2.5 s, fillers 4.2 per 100, hedging 0.4, pauses 1, length 85 s,
    # ownership 40%.
    a = _answer(
        words=250, fillers=10, hedges=1, i_count=2, we_count=3, long_pauses=1,
        speaking_s=250 / 175 * 60,
    )  # fmt: skip
    result = mx.spoken_result([a], reading_aloud=True, question_words=[10])
    ratings = {i.key: i.rating for i in result.items}
    assert ratings == {
        "pace": r.NEEDS_WORK, "thinking": r.GOOD, "fillers": r.NEEDS_WORK, "hedging": r.GOOD,
        "pauses": r.NEEDS_WORK, "length": r.GOOD, "ownership": r.NEEDS_WORK,
    }  # fmt: skip
    assert result.score == 77  # (60+100+60+100+60+100+60) / 7


def test_spoken_result_needs_fifty_words() -> None:
    assert not mx.spoken_result([_answer(words=49)], reading_aloud=True, question_words=[8]).enough
    assert mx.spoken_result([_answer(words=50)], reading_aloud=True, question_words=[8]).enough


def test_short_answers_are_left_out_of_the_values() -> None:
    long = _answer(words=100, duration_s=90.0)
    short = _answer(words=5, duration_s=4.0, fillers=5)  # under ten words
    result = mx.spoken_result([long, short], reading_aloud=True, question_words=[8, 8])
    length = next(i for i in result.items if i.key == "length")
    assert length.value == 90.0
    assert next(i for i in result.items if i.key == "fillers").value == 0


def test_reading_time_comes_off_thinking_time_when_not_read_aloud() -> None:
    a = _answer(first_word_s=6.0)
    aloud = mx.spoken_result([a], reading_aloud=True, question_words=[33])
    silent = mx.spoken_result([a], reading_aloud=False, question_words=[33])  # 33 / 3.3 = 10 s
    assert next(i for i in aloud.items if i.key == "thinking").value == 6.0
    assert next(i for i in silent.items if i.key == "thinking").value == 0.0


def test_sentence_times_mark_timing_values_approximate() -> None:
    result = mx.spoken_result([_answer(exact=False)], reading_aloud=True, question_words=[8])
    flags = {i.key: i.approximate for i in result.items}
    assert flags["thinking"]
    assert flags["pauses"]
    assert not flags["fillers"]


def test_no_times_falls_back_to_recording_length_for_pace() -> None:
    a = _answer(speaking_s=None, first_word_s=None, long_pauses=None, exact=False, duration_s=60.0)
    result = mx.spoken_result([a], reading_aloud=True, question_words=[8])
    keys = {i.key for i in result.items}
    assert "pace" in keys
    assert "thinking" not in keys
    assert "pauses" not in keys
    assert next(i for i in result.items if i.key == "pace").approximate


def test_ownership_left_out_without_i_or_we() -> None:
    result = mx.spoken_result(
        [_answer(i_count=0, we_count=0)], reading_aloud=True, question_words=[8]
    )
    assert "ownership" not in {i.key for i in result.items}


def test_worst_tip_is_the_lowest_rating() -> None:
    weak = mx.spoken_result([_answer(fillers=10)], reading_aloud=True, question_words=[8])
    assert next(i for i in weak.items if i.key == "fillers").rating == r.WEAK
    assert weak.worst_tip() == r.ASPECT_BY_KEY["fillers"].tip
    good = mx.spoken_result([_answer()], reading_aloud=True, question_words=[8])
    assert good.score == 100
    assert good.worst_tip() == ""


# ---- model output checks --------------------------------------------------------------------

CV = "Led a team of 12 engineers. Cut checkout failures by 30% in 2023. Budget 1,50,000 USD."
TRANSCRIPT = "I led the lender integration. We moved 14 lenders onto one API in six weeks."


def _reply(**over: object) -> str:
    rating = {"score": 85, "reason": "clear", "quote": "I led the lender integration"}
    item: dict[str, object] = {
        "q": 1,
        "ratings": {k: dict(rating) for k in r.ANSWER_ASPECT_KEYS},
        "verdict": "Good answer.",
        "worked": ["Named the work."],
        "next": [{"title": "Add a result", "why": "Shows impact", "fix": "Say the days saved."}],
        "improved": "I led it. We moved 14 lenders in six weeks, a 30% gain.",
    }
    item.update(over)
    return json.dumps({"answers": [item], "top_tips": ["Add one number."]})


ANSWERS = [fb.AnswerText(1, "Tell me about a project.", TRANSCRIPT)]


def test_quote_check_ignores_case_and_punctuation() -> None:
    assert fb.quote_found("moved 14 lenders, onto ONE api!", TRANSCRIPT)
    assert not fb.quote_found("moved 40 lenders", TRANSCRIPT)
    assert not fb.quote_found("", TRANSCRIPT)
    assert not fb.quote_found(" ".join(["I"] * 16), "I " * 20)  # over fifteen words


def test_a_quote_that_is_not_in_the_answer_caps_the_rating_at_sixty() -> None:
    bad = {"score": 95, "reason": "great", "quote": "I invented this"}
    ratings = {k: {"score": 85, "reason": "x", "quote": "I led the lender integration"}
               for k in r.ANSWER_ASPECT_KEYS}  # fmt: skip
    ratings["structure"] = bad
    result = fb.parse_feedback(_reply(ratings=ratings), ANSWERS, CV)
    got = {x.key: (x.score, x.capped) for x in result.answers[0].ratings}
    assert got["structure"] == (60, True)
    assert got["relevance"] == (85, False)
    assert result.answers[0].score == round((60 + 85 * 4) / 5)  # 80


def test_a_low_rating_with_a_bad_quote_is_not_raised() -> None:
    ratings = {k: {"score": 40, "reason": "x", "quote": "not said"} for k in r.ANSWER_ASPECT_KEYS}
    result = fb.parse_feedback(_reply(ratings=ratings), ANSWERS, CV)
    assert all(x.score == 40 for x in result.answers[0].ratings)


def test_numbers_not_in_the_cv_or_answer_are_replaced() -> None:
    text, count = fb.guard_numbers(
        "We moved 14 lenders in 6 weeks, a 30% gain, saving $9,000 [add 25 here].", CV, TRANSCRIPT
    )
    assert "14" in text
    assert "30%" in text
    assert "[add 25 here]" in text
    assert "9,000" not in text
    assert fb.NUMBER_PLACEHOLDER in text
    assert count == 2  # "6" is not in the sources either: the answer says "six"


def test_parse_feedback_scores_and_replaces_numbers() -> None:
    result = fb.parse_feedback(_reply(improved="I led 99 people."), ANSWERS, CV)
    assert "99" not in result.answers[0].improved
    assert result.answers[0].numbers_replaced == 1
    assert result.score == result.answers[0].score == 85
    assert result.top_tips == ("Add one number.",)


def test_missing_answers_are_listed() -> None:
    two = [*ANSWERS, fb.AnswerText(2, "Another?", "I did another thing entirely and well.")]
    result = fb.parse_feedback(_reply(), two, CV)
    assert result.missing == (2,)
    assert len(result.answers) == 1


def test_duplicate_and_unknown_questions_are_ignored() -> None:
    data = json.loads(_reply())
    extra = dict(data["answers"][0], q=7)
    data["answers"] += [dict(data["answers"][0]), extra]
    result = fb.parse_feedback(json.dumps(data), ANSWERS, CV)
    assert len(result.answers) == 1


@pytest.mark.parametrize(
    "reply",
    [
        "no json here",
        "{not valid}",
        "[1, 2]",
        json.dumps({"answers": "x"}),
        json.dumps({"answers": []}),
        json.dumps({"answers": [{"q": 1}]}),
    ],
)
def test_unusable_replies_raise(reply: str) -> None:
    with pytest.raises(fb.FeedbackError):
        fb.parse_feedback(reply, ANSWERS, CV)


def test_rating_out_of_range_or_not_a_number_raises() -> None:
    for bad in (101, -1, "90", True):
        ratings = {k: {"score": bad, "reason": "x", "quote": "I led"} for k in r.ANSWER_ASPECT_KEYS}
        with pytest.raises(fb.FeedbackError):
            fb.parse_feedback(_reply(ratings=ratings), ANSWERS, CV)


def test_reply_in_a_code_fence_is_read() -> None:
    result = fb.parse_feedback("Here you go:\n```json\n" + _reply() + "\n```", ANSWERS, CV)
    assert result.score == 85


def test_feedback_round_trips_through_json() -> None:
    answer = fb.parse_feedback(_reply(), ANSWERS, CV).answers[0]
    assert fb.AnswerFeedback.from_dict(answer.to_dict()) == answer


def test_combine_tips_puts_the_spoken_tip_first_without_repeats() -> None:
    assert fb.combine_tips("A", ["B", "A", "C", "D"]) == ("A", "B", "C")
    assert fb.combine_tips("", ["B"]) == ("B",)


def test_user_message_has_cv_role_and_answers() -> None:
    msg = fb.build_user_message(cv_text="CV text", role_lines=["Title: TPM"], answers=ANSWERS)
    assert "CV text" in msg
    assert "Title: TPM" in msg
    assert "[Question 1]" in msg
    assert "[Answer 1] " + TRANSCRIPT in msg
