import base64
from copy import deepcopy
from datetime import date
import json

import pytest
import requests

from reportbot import summarize
from reportbot.models import Report


API_KEY = "fake-gemini-summary-key"
PDF = b"%PDF-1.7\ncomplete original report bytes\n%%EOF"
REPORT = Report("naver", "100", "반도체 산업 구조 변화", "미래증권", date(2026, 10, 26), "https://example.com/100")


def summary_items(count=5, points=3):
    return [{"heading": f"산업 핵심 주제 {section}", "points": [
        {"text": f"수요 증가가 생산 확대를 견인하는 근거 {section}-{point}", "pages": [3, 1, 3]}
        for point in range(1, points + 1)
    ]} for section in range(1, count + 1)]


def normalized(items=None):
    items = summary_items() if items is None else items
    return [{"heading": section["heading"], "points": [point["text"] for point in section["points"]]}
            for section in items]


def body(items=None, finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [
        {"thought": True, "text": "Do not display internal reasoning"},
        {"text": json.dumps({"sections": summary_items() if items is None else items}, ensure_ascii=False)},
    ]}}]}


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = body() if data is None else data
        self.ok = status < 400

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(API_KEY)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", API_KEY)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("SUMMARY_MODE", raising=False)
    summarize._CACHE.clear()
    summarize._QUOTA_BLOCKED.clear()
    calls, responses = [], [Response()]

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(summarize.requests, "Session", Session)
    return calls, responses


def test_summary_sends_whole_pdf_and_header_key_with_grounded_explanatory_sections(provider):
    calls, _ = provider
    sections = summarize.summarize_pdf(REPORT, PDF, 20)
    assert sections == normalized()
    assert len(sections) == 5 and all(len(section["points"]) == 3 for section in sections)
    [(url, options)] = calls
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
    assert API_KEY not in url
    assert options["headers"]["x-goog-api-key"] == API_KEY
    assert options["allow_redirects"] is False and options["timeout"] == (10, 120)
    payload = options["json"]
    inline = payload["contents"][0]["parts"][0]["inlineData"]
    assert inline["mimeType"] == "application/pdf" and base64.b64decode(inline["data"]) == PDF
    schema = payload["generationConfig"]["responseSchema"]["properties"]["sections"]
    assert schema["items"]["properties"]["points"]["minItems"] == 3
    assert "원문 문장을 발췌" in payload["systemInstruction"]["parts"][0]["text"]
    assert "원인·근거·의미" in payload["systemInstruction"]["parts"][0]["text"]
    assert "Internal reasoning" not in json.dumps(sections)
    assert "pages" not in json.dumps(sections) and "(p." not in json.dumps(sections)


@pytest.mark.parametrize("count,points", [(3, 3), (3, 5), (5, 3)])
def test_three_topic_fallback_and_three_to_five_support_lines_are_accepted(provider, count, points):
    calls, responses = provider
    items = summary_items(count, points)
    responses[:] = [Response(data=body(items))]
    assert summarize.summarize_pdf(REPORT, PDF, 20) == normalized(items)
    assert len(calls) == 1


def test_no_key_or_off_does_not_contact_provider(provider, monkeypatch):
    calls, _ = provider
    monkeypatch.delenv("GEMINI_API_KEY")
    assert summarize.summarize_pdf(REPORT, PDF, 20) is None
    monkeypatch.setenv("GEMINI_API_KEY", API_KEY)
    monkeypatch.setenv("SUMMARY_MODE", "off")
    assert summarize.summarize_pdf(REPORT, PDF, 20) is None
    assert calls == []


def test_nested_cache_returns_deep_copies_and_is_scoped_to_key_and_model(provider, monkeypatch):
    calls, responses = provider
    first = summarize.summarize_pdf(REPORT, PDF, 20)
    first[0]["heading"] = "caller mutation"
    first[0]["points"][0] = "nested caller mutation"
    first[1]["points"].append("extra")
    assert summarize.summarize_pdf(REPORT, PDF, 20) == normalized()
    assert len(calls) == 1
    monkeypatch.setenv("GEMINI_API_KEY", "different-fake-key")
    responses.append(Response())
    assert len(summarize.summarize_pdf(REPORT, PDF, 20)) == 5
    monkeypatch.setenv("GEMINI_MODEL", "different-verified-model")
    responses.append(Response())
    summarize.summarize_pdf(REPORT, PDF, 20)
    assert len(calls) == 3 and API_KEY not in repr(summarize._CACHE)


@pytest.mark.parametrize("invalid,code", [
    ("count", "validation_items"), ("points-count", "validation_items"),
    ("empty", "validation_text"), ("english", "validation_text"),
    ("multiline", "validation_text"), ("long", "validation_text"),
    ("wide", "validation_text"), ("heading", "validation_text"),
    ("duplicate", "validation_duplicate"), ("duplicate-heading", "validation_duplicate"),
    ("boolean-page", "validation_pages"), ("zero-page", "validation_pages"),
    ("outside-page", "validation_pages"), ("no-page", "validation_pages"),
    ("many-pages", "validation_pages"), ("string-page", "validation_pages"),
    ("not-object", "validation_item"), ("not-section", "validation_item"),
])
def test_invalid_sections_are_rejected_without_cutting_their_contents(provider, invalid, code):
    calls, responses = provider
    items = deepcopy(summary_items())
    point = items[0]["points"][0]
    if invalid == "count": items.pop()
    elif invalid == "points-count": items[0]["points"].pop()
    elif invalid == "empty": point["text"] = " "
    elif invalid == "english": point["text"] = "Only English sentence"
    elif invalid == "multiline": point["text"] = "수요 증가\n생산 확대"
    elif invalid == "long": point["text"] = "가" + "A" * 42
    elif invalid == "wide": point["text"] = "가" * 39
    elif invalid == "heading": items[0]["heading"] = "가" * 19
    elif invalid == "duplicate": items[1]["points"][0]["text"] = point["text"]
    elif invalid == "duplicate-heading": items[1]["heading"] = items[0]["heading"]
    elif invalid == "boolean-page": point["pages"] = [True]
    elif invalid == "zero-page": point["pages"] = [0]
    elif invalid == "outside-page": point["pages"] = [21]
    elif invalid == "no-page": point["pages"] = []
    elif invalid == "many-pages": point["pages"] = [1, 2, 3, 4]
    elif invalid == "string-page": point["pages"] = ["1"]
    elif invalid == "not-object": items[0]["points"][0] = "수요 증가"
    elif invalid == "not-section": items[0] = "수요 증가"
    response = Response(data=body(items))
    responses[:] = [response, response]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert error.value.code == code
    assert len(calls) == (2 if code in {"validation_text", "validation_duplicate", "validation_items"} else 1)


def test_total_body_budget_rejects_oversized_sections_without_truncation():
    sections = normalized(summary_items(5, 5))
    assert all(len(point) <= summarize.MAX_POINT_CHARS for section in sections for point in section["points"])
    with pytest.raises(summarize.SummaryError) as error:
        summarize.validate_summary_sections(sections)
    assert error.value.code == "validation_budget"


@pytest.mark.parametrize("point", ["가" * 38, "가" + "A" * 41])
def test_complete_explanation_is_kept_at_character_and_display_width_limits(point):
    sections = normalized(summary_items(3, 3))
    sections[0]["points"][0] = point
    assert summarize.validate_summary_sections(sections)[0]["points"][0] == point


@pytest.mark.parametrize("point", ["성장\u2028설명", "성장\u200b설명", "1. 수요 증가"])
def test_hidden_line_breaks_and_pre_numbered_explanations_are_rejected(point):
    sections = normalized(summary_items(3, 3))
    sections[0]["points"][0] = point
    with pytest.raises(summarize.SummaryError) as error:
        summarize.validate_summary_sections(sections)
    assert error.value.code == "validation_text"


@pytest.mark.parametrize("problem", ["long", "duplicate", "count", "budget"])
def test_one_ai_rewrite_can_fix_section_shape_or_length_semantically(provider, problem):
    calls, responses = provider
    draft = summary_items(5, 5) if problem == "budget" else summary_items()
    if problem == "long": draft[0]["points"][0]["text"] = "가" * 39
    elif problem == "duplicate": draft[1]["heading"] = draft[0]["heading"]
    elif problem == "count": draft.pop()
    responses[:] = [Response(data=body(draft)), Response()]
    assert summarize.summarize_pdf(REPORT, PDF, 20) == normalized()
    assert len(calls) == 2
    first, second = [options["json"] for _, options in calls]
    assert second["contents"][0] == first["contents"][0]
    assert json.loads(second["contents"][1]["parts"][0]["text"]) == {"sections": draft}
    assert "기계적으로 자르지" in second["contents"][2]["parts"][0]["text"]
    assert "원인·근거·의미" in second["contents"][2]["parts"][0]["text"]


def test_failed_rewrite_stops_after_two_requests_and_caches_nothing(provider):
    calls, responses = provider
    invalid = summary_items()
    invalid[0]["points"][0]["text"] = "가" * 39
    responses[:] = [Response(data=body(invalid)), Response(data=body(invalid))]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert error.value.code == "validation_text" and len(calls) == 2
    assert not summarize._CACHE


@pytest.mark.parametrize("response", [Response(401), Response(503), Response(429), requests.ReadTimeout(API_KEY)])
def test_http_quota_and_transport_failures_never_trigger_ai_rewrite(provider, response):
    calls, responses = provider
    responses[:] = [response]
    with pytest.raises(summarize.SummaryError):
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert len(calls) == 1


def test_invalid_page_evidence_never_triggers_a_rewrite(provider):
    calls, responses = provider
    invalid = summary_items()
    invalid[0]["points"][0]["pages"] = [21]
    responses[:] = [Response(data=body(invalid))]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert error.value.code == "validation_pages" and len(calls) == 1


@pytest.mark.parametrize("response", [
    Response(401, {"error": API_KEY}), Response(500, {"error": API_KEY}),
    Response(data=ValueError(API_KEY)), Response(data={}),
    Response(data=body(finish="MAX_TOKENS")),
    requests.ReadTimeout(f"provider URL contains {API_KEY}"),
])
def test_provider_failure_is_sanitized(provider, response, caplog):
    _, responses = provider
    responses[:] = [response]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert API_KEY not in str(error.value) and API_KEY not in caplog.text


def test_rate_limit_stops_further_calls_for_same_key_in_process(provider, monkeypatch):
    calls, responses = provider
    responses[:] = [Response(429, {"error": "quota exhausted"})]
    for data in [PDF, PDF + b"\nsecond document"]:
        with pytest.raises(summarize.SummaryError):
            summarize.summarize_pdf(REPORT, data, 20)
    assert len(calls) == 1 and API_KEY not in repr(summarize._QUOTA_BLOCKED)
    monkeypatch.setenv("GEMINI_API_KEY", "new-fake-key")
    responses.append(Response())
    assert len(summarize.summarize_pdf(REPORT, PDF, 20)) == 5
    assert len(calls) == 2


@pytest.mark.parametrize("data,pages", [(b"<html>not PDF</html>", 20), (PDF, None), (PDF, 0), (PDF, 1001), (PDF, True)])
def test_invalid_input_never_calls_provider(provider, data, pages):
    calls, _ = provider
    with pytest.raises(summarize.SummaryError):
        summarize.summarize_pdf(REPORT, data, pages)
    assert calls == []


@pytest.mark.parametrize("name,value", [("SUMMARY_MODE", "unexpected"), ("GEMINI_MODEL", "../../other")])
def test_invalid_settings_never_contact_provider(provider, monkeypatch, name, value):
    calls, _ = provider
    monkeypatch.setenv(name, value)
    with pytest.raises(summarize.SummaryError):
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert calls == []


@pytest.mark.parametrize("reason", [
    "API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED",
    "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
    "SERVICE_DISABLED", "BILLING_DISABLED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    "CONSUMER_INVALID",
])
def test_http_diagnostics_only_retain_allowlisted_error_info_reason(provider, reason):
    _, responses = provider
    responses[:] = [Response(403, {"error": {
        "message": f"Private provider message contains {API_KEY}",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                     "metadata": {"key": API_KEY}}],
    }})]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert error.value.code == "http_403"
    assert error.value.status_code == 403
    assert error.value.reason == reason
    assert API_KEY not in str(error.value)
    assert API_KEY not in repr(vars(error.value))


@pytest.mark.parametrize("data", [
    {"error": {"reason": "API_KEY_INVALID", "message": API_KEY}},
    {"error": {"details": [{"reason": "API_KEY_INVALID", "message": API_KEY}]}},
    {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": API_KEY}]}},
    {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": [API_KEY]}]}},
    {"error": API_KEY},
    ValueError(API_KEY),
])
def test_http_diagnostics_discard_unrecognized_and_malformed_provider_details(provider, data):
    _, responses = provider
    responses[:] = [Response(400, data)]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert (error.value.code, error.value.status_code, error.value.reason) == ("http_400", 400, None)
    assert API_KEY not in str(error.value)
    assert API_KEY not in repr(vars(error.value))


@pytest.mark.parametrize("response,expected_code,expected_status", [
    (Response(404, {"error": API_KEY}), "http_404", 404),
    (Response(429, {"error": API_KEY}), "quota_429", 429),
    (Response(data=ValueError(API_KEY)), "response_json", None),
    (Response(data={}), "response_schema", None),
    (Response(data=body(finish="MAX_TOKENS")), "response_incomplete", None),
    (Response(data=body(items=[])), "validation_items", None),
    (requests.ReadTimeout(API_KEY), "transport_error", None),
])
def test_failures_report_fixed_stage_without_exposing_provider_contents(provider, response, expected_code, expected_status):
    calls, responses = provider
    responses[:] = [response, response] if expected_code in {"validation_items", "validation_text", "validation_duplicate"} else [response]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert error.value.code == expected_code
    assert error.value.status_code == expected_status
    assert error.value.reason == ("ReadTimeout" if isinstance(response, requests.ReadTimeout) else None)
    assert len(calls) == (2 if expected_code in {"validation_items", "validation_text", "validation_duplicate"} else 1)
    assert API_KEY not in str(error.value)
    assert API_KEY not in repr(vars(error.value))


@pytest.mark.parametrize("code,status_code,reason,expected", [
    (API_KEY, True, API_KEY, ("summary_error", None, None)),
    ({"private": API_KEY}, 600, [API_KEY], ("summary_error", None, None)),
    ("http_099", 99, None, ("summary_error", None, None)),
    ("http_600", "403", None, ("summary_error", None, None)),
    ("http_401", 401, "API_KEY_INVALID", ("http_401", 401, "API_KEY_INVALID")),
])
def test_summary_error_normalizes_public_diagnostic_fields(code, status_code, reason, expected):
    error = summarize.SummaryError("안전한 오류 안내", code=code, status_code=status_code, reason=reason)
    assert (error.code, error.status_code, error.reason) == expected
    assert API_KEY not in repr(vars(error))


@pytest.mark.parametrize("error_type", [
    requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout,
    requests.exceptions.SSLError, requests.exceptions.ProxyError,
    requests.exceptions.ConnectionError, requests.exceptions.Timeout,
    requests.exceptions.RequestException,
])
def test_transport_diagnostic_uses_only_static_request_class_name(provider, error_type):
    _, responses = provider
    responses[:] = [error_type(f"Private error URL contains {API_KEY}")]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.summarize_pdf(REPORT, PDF, 20)
    assert error.value.code == "transport_error"
    assert error.value.reason == error_type.__name__
    assert API_KEY not in repr(vars(error.value))
    assert API_KEY not in str(error.value)


@pytest.fixture
def metadata_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", API_KEY)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    calls, responses = [], [Response(data={"supportedGenerationMethods": ["generateContent", "countTokens"]})]

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def post(self, *args, **kwargs):
            pytest.fail("The connection check must never generate content")

    monkeypatch.setattr(summarize.requests, "Session", Session)
    return calls, responses


def test_connection_probe_checks_model_with_header_key_and_no_generation(metadata_provider):
    calls, _ = metadata_provider
    assert summarize.check_provider_connection() is None
    assert calls == [("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite", {
        "headers": {"x-goog-api-key": API_KEY}, "timeout": (10, 15), "allow_redirects": False,
    })]


def test_connection_probe_preserves_safe_http_diagnostics(metadata_provider):
    calls, responses = metadata_provider
    responses[:] = [Response(400, {"error": {"message": API_KEY, "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"},
    ]}})]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.check_provider_connection()
    assert (error.value.code, error.value.status_code, error.value.reason) == ("http_400", 400, "API_KEY_INVALID")
    assert len(calls) == 1
    assert API_KEY not in str(error.value)
    assert API_KEY not in repr(vars(error.value))


@pytest.mark.parametrize("response,code,reason", [
    (requests.ReadTimeout(API_KEY), "transport_error", "ReadTimeout"),
    (Response(data=ValueError(API_KEY)), "response_json", None),
    (Response(data=[API_KEY]), "response_schema", None),
    (Response(data={"supportedGenerationMethods": ["countTokens"]}), "model_not_supported", None),
])
def test_connection_probe_failures_are_bounded_and_redacted(metadata_provider, response, code, reason):
    calls, responses = metadata_provider
    responses[:] = [response]
    with pytest.raises(summarize.SummaryError) as error:
        summarize.check_provider_connection()
    assert (error.value.code, error.value.reason) == (code, reason)
    assert len(calls) == 1
    assert API_KEY not in str(error.value)
    assert API_KEY not in repr(vars(error.value))


def test_connection_probe_missing_key_never_contacts_service(metadata_provider, monkeypatch):
    calls, _ = metadata_provider
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(summarize.SummaryError) as error:
        summarize.check_provider_connection()
    assert error.value.code == "config_key"
    assert calls == []
