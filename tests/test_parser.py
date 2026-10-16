"""Runs against a real PST when one is available:
PST_SAMPLE=path/to/file.pst pytest tests/test_parser.py"""
import os

import pytest

from app.pst_parser import iter_messages, open_and_count

SAMPLE = os.environ.get("PST_SAMPLE", os.path.join(os.path.dirname(__file__), "fixtures", "enron.pst"))
pytestmark = pytest.mark.skipif(not os.path.exists(SAMPLE), reason="no sample PST available")


def test_parses_every_item():
    total = open_and_count(SAMPLE)
    emails = [e for e, _ in iter_messages(SAMPLE, 50 * 1024 * 1024) if e is not None]
    assert total > 0 and len(emails) > 0
    for e in emails:
        assert e.folder_path.startswith("/")
        assert e.sent_at is None or e.sent_at.tzinfo is not None
        assert "\x00" not in e.subject
        for r in e.recipients:
            assert r["type"] in ("to", "cc", "bcc")
    assert any(e.attachments for e in emails)
    assert any(e.sender_email for e in emails)
