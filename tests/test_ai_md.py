from gg.ai_md import Tag, parse


_DOC = """\
# Open review issues — branch ai (x open across y reviews)

## abc123 fix: thing  —  r/1000
  https://rb/r/1000/
- a.py:10 (by rev): please rename <!-- gg diff 55 777 -->
  quoted continuation from the reviewer
  [FIXED] fixup d -> abc123. Renamed as suggested.

## def456 feat: other  —  r/1001
- b.py:3 (by rev): is this needed? <!-- gg diff 66 888 -->
  ### ANSWER
  Yes — keeping it on purpose.
- c.py:9 (by rev): refactor later <!-- gg diff 66 889 -->
  [DECISION]
  needs your call
- d.py:1 (by rev): no marker here <!-- gg general 66 890 -->
"""


def test_parse_fixed_resolves_and_strips_marker():
    comments = parse(_DOC)
    c = next(c for c in comments if c.comment_id == 777)
    assert c.review_request_id == "1000"
    assert c.tag == Tag(kind="diff", review_oid=55, comment_id=777)
    assert c.action == "resolve"
    assert c.response == "fixup d -> abc123. Renamed as suggested."
    assert "quoted continuation" not in c.response


def test_answer_drops_and_strips_heading():
    c = next(c for c in parse(_DOC) if c.comment_id == 888)
    assert c.action == "drop"
    assert c.response == "Yes — keeping it on purpose."


def test_decision_is_skipped():
    c = next(c for c in parse(_DOC) if c.comment_id == 889)
    assert c.action == "skip-decision"
    assert c.response is None


def test_no_marker_is_skip_noresponse():
    c = next(c for c in parse(_DOC) if c.comment_id == 890)
    assert c.action == "skip-noresponse"


def test_fixed_and_answer_combine_resolve():
    doc = (
        "## h s  —  r/2\n"
        "- a.py:1 (by r): t <!-- gg diff 9 1 -->\n"
        "  [FIXED] did it in c0.\n\n"
        "  ### ANSWER\n"
        "  but note the nuance.\n"
    )
    c = parse(doc)[0]
    assert c.action == "resolve"
    assert c.response == "did it in c0.\n\nbut note the nuance."


def test_untagged_bullet_has_no_tag_but_parses_location():
    doc = "## h s  —  r/3\n- a.py:5 (by rev): hi\n  [FIXED] done.\n"
    c = parse(doc)[0]
    assert c.tag is None
    assert c.file == "a.py"
    assert c.line == "5"
    assert c.text_first_line == "hi"
    assert c.action == "resolve"
