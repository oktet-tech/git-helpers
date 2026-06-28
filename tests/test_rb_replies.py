from gg import rb_replies


class _FakeReply:
    def __init__(self): self.diff = []; self.general = []; self.published = False
    def get_diff_comments(self): return _FakeList(self.diff)
    def get_general_comments(self): return _FakeList(self.general)
    def update(self, **kw): self.published = kw.get("public", False)


class _FakeList:
    def __init__(self, sink): self.sink = sink
    def create(self, **kw): self.sink.append(kw)


class _FakeReplies:
    def __init__(self, reply): self.reply = reply
    def create(self, **kw): return self.reply


class _FakeReview:
    def __init__(self, reply): self._reply = reply
    def get_replies(self): return _FakeReplies(self._reply)


def test_post_replies_for_review_shapes_calls(monkeypatch):
    reply = _FakeReply()
    monkeypatch.setattr(rb_replies, "_get_review", lambda rr, oid, cwd: _FakeReview(reply))
    targets = [
        {"kind": "diff", "comment_id": 1, "text": "fixed it"},
        {"kind": "general", "comment_id": 2, "text": "answered"},
    ]
    rb_replies.post_replies_for_review("1000", 55, targets, cwd=None)
    assert reply.diff == [{"reply_to_id": 1, "text": "fixed it"}]
    assert reply.general == [{"reply_to_id": 2, "text": "answered"}]
    assert reply.published is True
