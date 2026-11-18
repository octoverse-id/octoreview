import json
import logging

import pytest

from octoreview.core.logging import JsonFormatter, scrub


@pytest.mark.parametrize(
    "raw, secret",
    [
        ("GET /api/v1/media-files/abc.def.ghi HTTP/1.1", "abc.def.ghi"),
        ("PUT /api/v1/media-uploads/tok123?x=1", "tok123"),
        ("https://b.s3.amazonaws.com/k?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA", "deadbeef"),
        ("https://storage.googleapis.com/b/k?X-Goog-Signature=cafe", "cafe"),
        ("Authorization: Bearer ocr_abcd1234_supersecret", "supersecret"),
        ("key ocr_abcd1234_supersecret leaked", "supersecret"),
    ],
)
def test_scrub_removes_tokens(raw, secret):
    assert secret not in scrub(raw)


def test_formatter_scrubs_exception_text():
    try:
        raise RuntimeError("failed GET /api/v1/media-files/SECRET-TOKEN")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, None)
        import sys

        record.exc_info = sys.exc_info()
    line = JsonFormatter().format(record)
    assert "SECRET-TOKEN" not in line
    assert json.loads(line)["message"] == "boom"


@pytest.mark.django_db
def test_request_log_uses_the_route_template(api_client, caplog):
    with caplog.at_level(logging.INFO, logger="octoreview.requests"):
        api_client.get("/api/v1/health/live")
        api_client.get("/api/v1/no-such-route/with-a-token")
    routes = [getattr(record, "route", None) for record in caplog.records]
    assert "api/v1/health/live" in routes
    assert "<unresolved>" in routes
    assert not any("with-a-token" in JsonFormatter().format(record) for record in caplog.records)
