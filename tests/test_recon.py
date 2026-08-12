"""Recon helper tests. No browser, no network.

These cover the analysis logic that turns a capture into the answers we need.
They cannot validate the browser driving itself — that is what the rehearsal
run against a public site is for.
"""

from __future__ import annotations

from shift_agent.recon import (
    cookie_verdict,
    detect_captcha,
    detect_mfa,
    infer_sso,
    looks_like_shift_data,
    rank_json_endpoints,
    redact,
)


# --- SSO inference -----------------------------------------------------------

def test_detects_okta_in_redirect_chain():
    assert infer_sso([
        "https://crew.airline.example/login",
        "https://airline.okta.com/oauth2/v1/authorize?x=1",
    ]) == "okta"


def test_detects_azure_ad():
    assert infer_sso(["https://login.microsoftonline.com/common/oauth2/authorize"]) == "azure-ad"


def test_detects_ping():
    assert infer_sso(["https://sso.pingone.com/idp/SSO.saml2"]) == "ping"


def test_direct_login_reports_no_sso():
    assert infer_sso(["https://crew.airline.example/login", "https://crew.airline.example/home"]) is None


# --- captcha and MFA ---------------------------------------------------------

def test_detects_recaptcha_from_markup():
    html = '<div class="g-recaptcha" data-sitekey="abc"></div>'
    assert "recaptcha" in detect_captcha(html)


def test_detects_turnstile_from_script_url():
    assert "turnstile" in detect_captcha("https://challenges.cloudflare.com/turnstile/v0/api.js")


def test_clean_page_reports_no_captcha():
    assert detect_captcha("<html><body>Welcome back</body></html>") == []


def test_detects_mfa_prompt_language():
    found = detect_mfa("<p>Enter the verification code from your authenticator app</p>")
    assert "verification code" in found and "authenticator" in found


def test_no_mfa_language_on_plain_page():
    assert detect_mfa("<p>Open trips for August</p>") == []


# --- JSON endpoint ranking ---------------------------------------------------

def test_scores_a_shift_list_highly():
    payload = [
        {"shiftId": "1", "startTime": "2026-08-12T09:00", "endTime": "2026-08-12T15:00"},
        {"shiftId": "2", "startTime": "2026-08-13T09:00", "endTime": "2026-08-13T15:00"},
    ]
    assert looks_like_shift_data(payload) >= 5


def test_finds_shift_list_nested_under_a_key():
    payload = {"status": "ok", "openTrips": [
        {"tripId": "A", "departTime": "x", "arriveTime": "y"},
        {"tripId": "B", "departTime": "x", "arriveTime": "y"},
    ]}
    assert looks_like_shift_data(payload) >= 4


def test_unrelated_json_scores_zero():
    assert looks_like_shift_data({"theme": "dark", "locale": "en-US"}) == 0
    assert looks_like_shift_data(["a", "b", "c"]) == 0
    assert looks_like_shift_data(None) == 0


def test_ranking_puts_shift_endpoint_first():
    records = [
        {"url": "/api/prefs", "method": "GET", "status": 200, "json": {"theme": "dark"}},
        {"url": "/api/open-shifts", "method": "GET", "status": 200, "json": [
            {"shiftId": 1, "startTime": "a", "endTime": "b"},
            {"shiftId": 2, "startTime": "a", "endTime": "b"},
        ]},
        {"url": "/api/ping", "method": "GET", "status": 200},
    ]
    ranked = rank_json_endpoints(records)
    assert ranked[0]["url"] == "/api/open-shifts"
    assert all(r["url"] != "/api/prefs" for r in ranked)


# --- cookie survival verdict -------------------------------------------------

def test_401_reads_as_cookies_failing():
    assert cookie_verdict(401, "").startswith("LIKELY FAILS")


def test_redirect_reads_as_cookies_failing():
    assert cookie_verdict(302, "").startswith("LIKELY FAILS")


def test_login_page_body_reads_as_failing_even_with_200():
    """The subtle case: a 200 that is actually the login page."""
    assert cookie_verdict(200, "<form>Please sign in with your password</form>").startswith("LIKELY FAILS")


def test_authenticated_content_reads_as_working():
    assert cookie_verdict(200, "<table id='open-trips'><tr>...</tr></table>").startswith("LIKELY WORKS")


def test_failed_request_is_inconclusive():
    assert cookie_verdict(None, None).startswith("INCONCLUSIVE")


def test_server_error_is_inconclusive():
    assert cookie_verdict(503, "oops").startswith("INCONCLUSIVE")


# --- redaction ---------------------------------------------------------------

def test_redacts_email_and_phone():
    out = redact("Contact jane.doe@airline.example or +1 (555) 867-5309 today")
    assert "jane.doe@airline.example" not in out
    assert "867-5309" not in out
    assert "[EMAIL]" in out and "[PHONE]" in out


def test_redacts_jwt_and_ssn():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijklmnop"
    out = redact(f"token={jwt} ssn=123-45-6789")
    assert jwt not in out
    assert "123-45-6789" not in out


def test_redacts_long_hex_tokens():
    out = redact("session=0123456789abcdef0123456789abcdef")
    assert "0123456789abcdef0123456789abcdef" not in out


def test_redaction_preserves_surrounding_markup():
    out = redact("<td class='crew'>jane@x.com</td>")
    assert out.startswith("<td class='crew'>") and out.endswith("</td>")
