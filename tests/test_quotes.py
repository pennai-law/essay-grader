from essay_grader.graders.claude_grader import validate_quotes

ANS = "The UCC applies\nbecause it is a sale of goods.  Quill’s reply changed the date."


def test_valid_quotes_normalized():
    assert validate_quotes(ANS, ["the ucc applies because it is a sale", "Quill's reply changed"]) == []


def test_ellipsis_fragments():
    assert validate_quotes(ANS, ["The UCC applies ... sale of goods"]) == []


def test_invalid_quote_flagged():
    assert validate_quotes(ANS, ["The UCC applies", "Marlow never accepted"]) == ["Marlow never accepted"]
    assert validate_quotes(ANS, [""]) == [""]
