"""Validate the configured research key with a small Responses request, never logging it."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from miki_jobsearch.research import tls_context


def check(environ, model, opener=urllib.request.urlopen):
    key = environ.get("MIKI_OPENAI_API_KEY") or environ.get("OPENAI_API_KEY")
    base = {"model": model, "required_secret": "MIKI_OPENAI_API_KEY"}
    if not key or not key.strip():
        return dict(base, status="missing", guidance="Save the API key as a GitHub Actions repository secret.")
    payload = {"model": model, "input": "Reply with exactly the word OK.",
               "store": False, "max_output_tokens": 256}
    if model == "gpt-5" or model.startswith("gpt-5-20"):
        payload["reasoning"] = {"effort": "minimal"}
    request = urllib.request.Request("https://api.openai.com/v1/responses",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with opener(request, timeout=90, context=tls_context()) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            code = json.loads(error.read(16000)).get("error", {}).get("code")
        except (ValueError, AttributeError):
            code = None
        # Ignore server messages: they may contain fragments of a rejected key.
        known = {"invalid_api_key", "insufficient_quota", "billing_hard_limit_reached",
                 "model_not_found", "rate_limit_exceeded", "account_deactivated"}
        code = code if code in known else "unknown"
        if error.code == 401:
            status, guidance = "authentication_failed", "Replace the GitHub secret with an active API key."
        elif code in {"insufficient_quota", "billing_hard_limit_reached"}:
            status, guidance = "billing_required", "Check OpenAI API billing, project budget and credits."
        elif error.code == 403:
            status, guidance = "permission_denied", "Check key permissions and project/model access."
        elif error.code == 404:
            status, guidance = "model_unavailable", "Check the configured RESEARCH_MODEL and model access."
        elif error.code == 429:
            status, guidance = "rate_limited", "Check API limits and retry after the limit clears."
        else:
            status, guidance = "api_error", "The API rejected the check; inspect the safe HTTP status."
        return dict(base, status=status, http_status=error.code, error_code=code, guidance=guidance)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return dict(base, status="connection_failed", error_type=type(error).__name__,
                    guidance="Check outbound API connectivity and TLS trust.")
    except ValueError:
        return dict(base, status="invalid_response", guidance="The API returned an unreadable response.")
    if result.get("status") != "completed":
        return dict(base, status="authenticated_response_incomplete", http_status=200,
                    guidance="Authentication succeeded, but the small Responses request did not complete.")
    return dict(base, status="ready", http_status=200,
                guidance="The key authenticated and a Responses request completed with the configured model.")


def main():
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "config/search.json").read_text())
    model = os.environ.get("RESEARCH_MODEL") or config["model"]
    result = check(os.environ, model)
    result.update(checked_at=datetime.now(timezone.utc).isoformat(),
                  runner="GitHub Actions", run_id=os.environ.get("GITHUB_RUN_ID"))
    path = root / "state/api-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
