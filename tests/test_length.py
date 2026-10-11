from essay_grader.ingest import apply_length_policy
from essay_grader.rubric import LengthPolicy

TEXT = "one two three\nfour five six seven"


def test_no_penalty():
    t, i = apply_length_policy(TEXT, LengthPolicy())
    assert t == TEXT and i["words"] == 7 and not i["truncated"]


def test_truncate():
    t, i = apply_length_policy(TEXT, LengthPolicy(mode="grade_first_n_words", word_cap=4))
    assert t == "one two three\nfour" and i["truncated"] and i["words_graded"] == 4 and i["words"] == 7


def test_under_cap_untouched():
    t, i = apply_length_policy(TEXT, LengthPolicy(mode="grade_first_n_words", word_cap=50))
    assert t == TEXT and not i["truncated"]


def test_deduct_with_max():
    p = LengthPolicy(mode="deduct", word_cap=100, deduct_points_per_100_words=2, max_deduction=3)
    _, i = apply_length_policy("w " * 150, p)
    assert i["deduction"] == 1.0
    _, i = apply_length_policy("w " * 1000, p)
    assert i["deduction"] == 3
