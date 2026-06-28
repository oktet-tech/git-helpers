from gg import reply


def _doc(tag="<!-- gg diff 55 777 -->"):
    return (
        "## abc fix: x  —  r/1000\n"
        f"- a.py:10 (by rev): rename {tag}\n"
        "  [FIXED] did it.\n"
    )


def test_plan_targets_from_tag(monkeypatch, tmp_path):
    items = reply.build_plan(reply.parse_input(_doc()), fetch=None)
    [it] = [i for i in items if i.action == "resolve"]
    assert it.review_request_id == "1000"
    assert it.review_oid == 55 and it.comment_id == 777 and it.kind == "diff"
    assert it.text == "did it."


def test_dry_run_prints_plan_and_posts_nothing(monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(reply.rb_replies, "post_replies_for_review",
                        lambda *a, **k: posted.append(a))
    rc = reply.run_text(_doc(), post=False, cwd=None)
    assert rc == 0
    assert posted == []
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "a.py:10" in ln)
    assert line.startswith("r/1000")
    assert "reply + RESOLVE" in line
    assert line.rstrip().endswith("a.py:10")  # variable-width path is last column


def test_post_calls_write_layer(monkeypatch):
    calls = {"reply": [], "status": []}
    monkeypatch.setattr(reply.rb_replies, "post_replies_for_review",
                        lambda rr, oid, targets, *, cwd: calls["reply"].append((rr, oid, targets)))
    monkeypatch.setattr(reply.rb_replies, "set_issue_status",
                        lambda *a, **k: calls["status"].append(a))
    rc = reply.run_text(_doc(), post=True, cwd=None)
    assert rc == 0
    assert calls["reply"] == [("1000", 55, [{"kind": "diff", "comment_id": 777, "text": "did it."}])]
    assert calls["status"] == [("1000", 55, "diff", 777, "resolve")]


def test_untagged_uses_fetch_fallback(monkeypatch):
    doc = "## abc fix  —  r/1000\n- a.py:10 (by rev): rename\n  [FIXED] did it.\n"

    class _Iss:
        review_oid = 99; comment_id = 5; kind = "diff"; file = "a.py"
        first_line = 10; text = "rename"
    monkeypatch.setattr(reply, "_fetch_open_issues", lambda rid, cwd: [_Iss()])
    items = reply.build_plan(reply.parse_input(doc), fetch=reply._fetch_open_issues, cwd=None)
    [it] = [i for i in items if i.action == "resolve"]
    assert it.review_oid == 99 and it.comment_id == 5
