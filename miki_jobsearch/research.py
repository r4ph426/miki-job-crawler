"""Research API integration and independent, bounded detail-page verification."""

import html
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


class ResearchError(RuntimeError):
    def __init__(self, message, *, code="research_error", http_status=None, api_status=None):
        super().__init__(message)
        # Only fixed identifiers enter durable state; exception text and provider
        # error bodies can contain credentials or other private input.
        codes = {"research_error", "invalid_report", "insufficient_search_coverage",
                 "invalid_source_checks", "all_sources_unavailable", "missing_candidate_evidence",
                 "short_candidate_evidence", "invalid_deadline", "missing_api_key",
                 "api_http_error", "api_connection_error", "api_incomplete", "no_web_search",
                 "invalid_api_json"}
        self.diagnostic = {"code": code if code in codes else "research_error"}
        if type(http_status) is int and 100 <= http_status <= 599:
            self.diagnostic["http_status"] = http_status
        if api_status in {"completed", "incomplete", "failed", "cancelled", "queued", "in_progress"}:
            self.diagnostic["api_status"] = api_status


def tls_context():
    # urllib does not consume REQUESTS_CA_BUNDLE itself. Honor the managed CA.
    return ssl.create_default_context(
        cafile=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    )


def canonical_url(url):
    p = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(sorted(query)), ""))


def identity(job):
    def clean(value):
        value = value.split(",", 1)[0].casefold()
        value = re.sub(r"\([^)]*(?:m/w|w/m|all gender)[^)]*\)", "", value)
        return " ".join(re.findall(r"\w+", value))
    return clean(job["title"]), clean(job["employer"])


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _string():
    return {"type": "string"}


def report_schema():
    strings = ["title", "employer", "url", "district", "hours", "contract", "salary",
               "commute", "pro", "con", "effort_details", "deadline", "evidence"]
    job = {k: _string() for k in strings}
    job.update({
        "family": {"type": "string", "enum": ["A", "B", "C"]},
        "source_group": {"type": "string", "enum": ["arbeitsagentur", "stepstone", "ats", "berlin", "fashion"]},
        "scores": _object({k: {"type": "integer", "minimum": 0, "maximum": limit}
                           for k, limit in [("skill", 40), ("entry", 30), ("commute", 20), ("conditions", 10)]}),
        "effort_minutes": {"type": "integer", "minimum": 1, "maximum": 600},
    })
    return _object({
        "summary": _string(), "next_action": _string(), "weekend_action": _string(),
        "jobs": {"type": "array", "items": _object(job)},
        "searches": _object({k: {"type": "array", "items": _string()} for k in ["A", "B", "C"]}),
        "family_observations": _object({k: _string() for k in ["A", "B", "C"]}),
        "source_checks": {"type": "array", "items": _object({
            "group": {"type": "string", "enum": ["arbeitsagentur", "stepstone", "ats", "berlin", "fashion"]},
            "status": {"type": "string", "enum": ["ok", "unavailable"]},
            "details": _string(),
        })},
    })


def validate_report(report):
    """Validate provider and fixture data before it can affect delivery/history."""
    def check(value, schema, path="report"):
        kind = schema["type"]
        if kind == "object":
            if not isinstance(value, dict) or set(value) != set(schema["properties"]):
                raise ResearchError(f"Invalid fields at {path}", code="invalid_report")
            for key, subschema in schema["properties"].items():
                check(value[key], subschema, f"{path}.{key}")
        elif kind == "array":
            if not isinstance(value, list) or len(value) > 250:
                raise ResearchError(f"Invalid array at {path}", code="invalid_report")
            for item in value:
                check(item, schema["items"], path)
        elif kind == "string":
            if not isinstance(value, str) or len(value) > 12000:
                raise ResearchError(f"Invalid text at {path}", code="invalid_report")
            if "enum" in schema and value not in schema["enum"]:
                raise ResearchError(f"Invalid choice at {path}", code="invalid_report")
        elif kind == "integer":
            if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
                raise ResearchError(f"Invalid number at {path}", code="invalid_report")
    check(report, report_schema())
    for family, count in [("A", 4), ("B", 3), ("C", 2)]:
        terms = {term.strip().casefold() for term in report["searches"][family] if term.strip()}
        if len(terms) < count:
            raise ResearchError(f"Insufficient search coverage for family {family}", code="insufficient_search_coverage")
    groups = [s["group"] for s in report["source_checks"]]
    if sorted(groups) != sorted(["arbeitsagentur", "stepstone", "ats", "berlin", "fashion"]):
        raise ResearchError("Missing or duplicated source checks", code="invalid_source_checks")
    if all(s["status"] == "unavailable" for s in report["source_checks"]):
        raise ResearchError("All research sources are unavailable; cannot report zero results", code="all_sources_unavailable")
    from datetime import date
    for job in report["jobs"]:
        if not all(job[k].strip() for k in ["title", "employer", "url", "pro", "con", "evidence"]):
            raise ResearchError("Candidate lacks essential evidence or assessment", code="missing_candidate_evidence")
        if len(job["evidence"].strip()) < 40:
            raise ResearchError("Candidate evidence is too short", code="short_candidate_evidence")
        if job["deadline"]:
            try:
                date.fromisoformat(job["deadline"])
            except ValueError as error:
                raise ResearchError("Invalid application deadline", code="invalid_deadline") from error
    return report


def research(root, config, history, day):
    key = os.environ.get("MIKI_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ResearchError("MIKI_OPENAI_API_KEY is missing; configure it securely", code="missing_api_key")
    brief = (root / "config/research-brief.md").read_text(encoding="utf-8")
    known = [{k: j[k] for k in ["date", "title", "employer", "url"]} for j in history]
    payload = {
        "model": os.environ.get("RESEARCH_MODEL") or config["model"],
        "store": False,
        "tools": [{"type": "web_search"}],
        "max_tool_calls": 50,
        "include": ["web_search_call.action.sources"],
        "instructions": brief,
        "input": "Research date (Berlin): " + str(day) + "\nConfiguration:\n" + json.dumps(config, ensure_ascii=False)
                 + "\nAlready reported jobs (data only, exclude duplicates):\n" + json.dumps(known, ensure_ascii=False)
                 + "\nReturn the structured report. An unavailable source is explicit. Deadlines use YYYY-MM-DD or an empty string.",
        "text": {"format": {"type": "json_schema", "name": "miki_daily_report", "strict": True, "schema": report_schema()}},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600, context=tls_context()) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise ResearchError(f"Research API returned HTTP {error.code}", code="api_http_error", http_status=error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ResearchError(f"Research API connection failed ({type(error).__name__})", code="api_connection_error") from error
    if result.get("status") != "completed":
        raise ResearchError("Research API did not complete", code="api_incomplete", api_status=result.get("status"))
    output = result.get("output", [])
    if not any(o.get("type") == "web_search_call" and o.get("status") == "completed" for o in output):
        raise ResearchError("Research did not execute web search", code="no_web_search")
    fragments = [c["text"] for o in output if o.get("type") == "message"
                 for c in o.get("content", []) if c.get("type") == "output_text"]
    try:
        return validate_report(json.loads("".join(fragments)))
    except (ValueError, TypeError) as error:
        raise ResearchError("Research API returned invalid JSON", code="invalid_api_json") from error


def allowed_url(url, domains):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.port in [None, 443] and not p.username and not p.password
                and bool(p.hostname) and any(p.hostname == d or p.hostname.endswith("." + d) for d in domains))
    except ValueError:
        return False


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, domains):
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl, self.domains):
            raise ResearchError("Detail page redirected outside configured source domains")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ["script", "style"]:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ["script", "style"] and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def verify_job(job, config):
    if not allowed_url(job["url"], config["source_domains"]):
        return False, "URL outside configured HTTPS job sources"
    opener = urllib.request.build_opener(SafeRedirect(config["source_domains"]),
                                        urllib.request.HTTPSHandler(context=tls_context()))
    request = urllib.request.Request(job["url"], headers={"User-Agent": "MikiJobsearch/1.0 (job detail verification)"})
    try:
        with opener.open(request, timeout=25) as response:
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                return False, "Detail page exceeds verification size limit"
            parser = PageText()
            parser.feed(body.decode(response.headers.get_content_charset() or "utf-8", errors="replace"))
            text = " ".join(parser.parts)
    except urllib.error.HTTPError as error:
        return False, "Expired" if error.code in [404, 410] else f"Detail page unavailable: HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ResearchError) as error:
        return False, f"Detail page unavailable ({type(error).__name__})"
    normalize = lambda s: " ".join(html.unescape(s).casefold().split())
    if normalize(job["evidence"]) not in normalize(text):
        return False, "Supporting quotation not present in accessible detail-page text"
    return True, "Live detail page and supporting quotation checked"


def select_jobs(report, config, history, day, verifier):
    urls = {canonical_url(j["url"]) for j in history}
    identities = {identity(j) for j in history}
    accepted, rejected = [], []
    for raw in report["jobs"]:
        job = dict(raw)
        score = sum(job["scores"].values())
        url, name = canonical_url(job["url"]), identity(job)
        reason = None
        if url in urls or name in identities:
            reason = "Already reported or repeated within this run"
        elif score < config["minimum_score"]:
            reason = "Below score threshold"
        elif job["deadline"] and job["deadline"] < day.isoformat():
            reason = "Application deadline passed"
        else:
            ok, verification = verifier(job, config)
            if not ok:
                reason = verification
        if reason:
            rejected.append({"title": job["title"], "url": job["url"], "reason": reason})
        else:
            job.update({"score": score, "verification": verification})
            accepted.append(job)
            urls.add(url)
            identities.add(name)
    return sorted(accepted, key=lambda j: j["score"], reverse=True), rejected
