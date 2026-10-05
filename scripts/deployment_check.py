"""Record binding presence on the real runner. Never record values or credentials."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def readiness(mode, environ):
    required = ["MIKI_OPENAI_API_KEY"]
    if mode == "send":
        required += ["GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO"]
    missing = [name for name in required if not environ.get(name, "").strip()]
    return {"status": "credentials_missing" if missing else "credentials_present",
            "missing_requirements": missing, "checked_requirements": required}


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
