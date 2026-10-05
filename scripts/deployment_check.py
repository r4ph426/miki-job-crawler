"""Record binding presence on the real runner. Never record values or credentials."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def readiness(mode, environ):
    required = ["MIKI_OPENAI_API_KEY"]
    if mode == "send":
        required += ["GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO"]
    missing = [name for name in required if not environ.get(name, "").strip()]
    result = {"status": "credentials_missing" if missing else "credentials_present",
              "missing_requirements": missing, "checked_requirements": required}
    if mode == "send":
        def plain_addresses(value, required=True):
            if not value.strip():
                return not required
            return all(re.fullmatch(r"[^@\s<>\"'`]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", part.strip())
                       for part in value.split(","))
        password = "".join(environ.get("GMAIL_APP_PASSWORD", "").split())
        user = environ.get("GMAIL_USER", "").strip()
        result["mail_format_checks"] = {
            "GMAIL_USER": "," not in user and plain_addresses(user),
            "GMAIL_APP_PASSWORD": len(password) == 16 and password.isascii() and password.isalnum(),
            "MAIL_TO": plain_addresses(environ.get("MAIL_TO", "")),
            "MAIL_CC": plain_addresses(environ.get("MAIL_CC", ""), required=False),
        }
        result["invalid_mail_formats"] = [name for name, valid in result["mail_format_checks"].items() if not valid]
    return result


def main():
    result = readiness(os.environ.get("RUN_MODE", "send"), os.environ)
    result.update({"checked_at": datetime.now(timezone.utc).isoformat(),
                   "runner": "GitHub Actions", "run_id": os.environ.get("GITHUB_RUN_ID")})
    path = Path(__file__).resolve().parent.parent / "state/deployment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
