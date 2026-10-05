"""Read-only Brevo authentication check. Save fixed diagnostics, never account data."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from miki_jobsearch.research import tls_context
from miki_jobsearch.service import NoMailRedirect


def check(environ, opener=None):
    base = {"required_secret": "BREVO_API_KEY", "operation": "read-only account authentication"}
    key = environ.get("BREVO_API_KEY", "").strip()
    if not key:
        return dict(base, status="missing", guidance="Save BREVO_API_KEY in GitHub Actions secrets.")
    if key.startswith("xsmtpsib-"):
        return dict(base, status="wrong_key_type", guidance="Use a Brevo API key from SMTP & API > API Keys, not an SMTP key.")
    if key.startswith(("sk-", "sk_")):
        return dict(base, status="wrong_provider_key", guidance="Replace BREVO_API_KEY with a Brevo API key, keeping the OpenAI key separate.")
    if not key.isascii() or not key.isprintable() or any(c.isspace() or c in "\"'`" for c in key):
        return dict(base, status="invalid_key_format", guidance="Paste the unquoted API key without labels or embedded spaces.")
    if opener is None:
        opener = urllib.request.build_opener(NoMailRedirect(), urllib.request.HTTPSHandler(context=tls_context())).open
    request = urllib.request.Request("https://api.brevo.com/v3/account", method="GET",
                                     headers={"api-key": key, "Accept": "application/json"})
    try:
        with opener(request, timeout=30) as response:
            code = response.status
            raw = response.read(16001)
        data = json.loads(raw) if len(raw) <= 16000 else None
        if code != 200 or not isinstance(data, dict):
            return dict(base, status="invalid_response", guidance="The account check returned an invalid acknowledgement.")
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read(16000))
            message = data.get("message", "") if isinstance(data, dict) else ""
            message = message.casefold() if isinstance(message, str) else ""
        except (ValueError, OSError):
            message = ""
        code = error.code
        error.close()
        # The body is inspected only to classify known causes. Never save or print it.
        ip_context = any(p in message for p in ["ip address", "ipaddress", "unrecognised ip", "unrecognized ip",
                                               "ip not allowed", "whitelist", "white list"])
        if code in {401, 403} and ip_context:
            status, guidance = "ip_access_denied", "Brevo API IP security rejected the GitHub runner. Review Authorized IPs in Brevo SMTP & API settings; hosted runners use changing IP addresses."
        elif code == 401 and ("key not found" in message or "invalid api key" in message):
            status, guidance = "invalid_api_key", "Brevo does not recognize this key. Replace the GitHub secret with a newly generated active Brevo API key."
        elif code == 401:
            status, guidance = "authentication_failed", "Brevo rejected authentication. Check that the secret is an active API key from this Brevo account."
        elif code == 403:
            status, guidance = "permission_denied", "Check Brevo API key permissions and account approval."
        elif code == 429:
            status, guidance = "rate_limited", "Retry the read-only check after the Brevo rate limit clears."
        else:
            status, guidance = "api_error", "Brevo rejected the read-only account check; inspect the HTTP status."
        return dict(base, status=status, http_status=code, guidance=guidance)
    except (urllib.error.URLError, OSError):
        return dict(base, status="connection_failed", guidance="Check outbound API access and TLS trust.")
    except ValueError:
        return dict(base, status="invalid_response", guidance="The account check returned unreadable data.")
    result = dict(base, status="ready", http_status=200,
                  guidance="Brevo authenticated the API key. Sender verification and delivery still require a send check.")
    relay = data.get("relay")
    enabled = relay.get("enabled") if isinstance(relay, dict) else None
    if type(enabled) is bool:
        result["transactional_email_enabled"] = enabled
        if not enabled:
            result.update(status="transactional_email_disabled", guidance="The key authenticated, but Brevo reports transactional email disabled. Activate/approve transactional email in Brevo.")
    return result


def main():
    result = check(os.environ)
    result.update(checked_at=datetime.now(timezone.utc).isoformat(),
                  runner="GitHub Actions", run_id=os.environ.get("GITHUB_RUN_ID"))
    path = Path(__file__).resolve().parent.parent / "state/brevo-key-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
