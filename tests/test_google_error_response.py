from bot import is_google_error_response


def test_detects_google_http_error_page_text():
    html = "<html><title>Error 429</title><body>Too Many Requests</body></html>"
    assert is_google_error_response(html) is True


def test_detects_http_status_text_in_error_page():
    html = "<html><body>429 Too Many Requests</body></html>"
    assert is_google_error_response(html) is True


def test_ignores_normal_translation_text():
    text = "hello world"
    assert is_google_error_response(text) is False
