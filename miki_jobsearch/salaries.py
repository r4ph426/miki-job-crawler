"""Sourced market estimates kept separate from employer-advertised pay."""

import math
import re
from datetime import date

METHOD_VERSION = "berlin-market-v1"


def normalized(value):
    value = value.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return " ".join(re.findall(r"\w+", value))


def salary_missing(value):
    # Preserve amounts and tariff grades even when the accompanying text says unknown.
    if re.search(r"\d|€|\b(?:eur|tv[oö]d|tv[l-]|tarif)\b", value, re.I):
        return False
    text = normalized(value)
    if text in {"", "k a", "n a", "tbd", "competitive", "verhandelbar", "nach vereinbarung"}:
        return True
    return bool(re.fullmatch(
        r"(?:(?:gehalt|salary|verguetung) )?(?:unbekannt|unknown|nicht (?:genannt|angegeben|bekannt|veroeffentlicht)|"
        r"keine (?:angabe|angaben|gehaltsangabe)|not (?:listed|disclosed|specified|provided))"
        r"(?: (?:in der anzeige|in the listing))?", text))


HOURS = re.compile(
    r"(?<![\d.,])(\d{1,2}(?:[.,]\d+)?)\s*"
    r"(?:(?:[-–—]|bis|to)\s*(\d{1,2}(?:[.,]\d+)?)\s*)?"
    r"(?:stunden|std\.?|hours?|h)\b", re.I)


def hours_basis(job, reference_hours):
    """Only scale explicit weekly hours; unspecified part-time stays a labelled FTE."""
    hours = job.get("hours", "")
    title = job.get("title", "")
    values = []
    for text in [hours, title]:
        # Do not turn an open-ended or alternative offer into fixed weekly hours.
        if re.search(r"\b(?:mindestens|hoechstens|höchstens|ab|oder|or|minimum|maximum|at least|bis zu)\b", text, re.I):
            continue
        for match in HOURS.finditer(text):
            # These are weekly estimates. Explicit daily/monthly hours are unsuitable.
            following = text[match.end():match.end() + 24]
            if re.match(r"\s*\.?(?:\s*(?:/|pro\s+|per\s+))?\s*(?:tag|day|monat|month|taeglich|täglich|daily|monatlich|monthly)\b", following, re.I):
                continue
            lower = float(match[1].replace(",", "."))
            upper = float((match[2] or match[1]).replace(",", "."))
            if 1 <= lower <= upper <= 48:
                values.append((lower, upper))
    unique = set(values)
    if len(unique) == 1:
        lower, upper = unique.pop()
        span = (f"{lower:g}" if lower == upper else f"{lower:g}–{upper:g}").replace(".", ",")
        note = f"{span} Std./Woche; anteilig auf Basis von {reference_hours:g} Std."
        return lower / reference_hours, upper / reference_hours, note
    if "vollzeit" in normalized(hours) and "teilzeit" not in normalized(hours):
        note = f"Vollzeit; Vergleichsbasis {reference_hours:g} Std./Woche"
    else:
        note = f"Vollzeitäquivalent ({reference_hours:g} Std./Woche); tatsächliche Wochenstunden unklar"
    return 1, 1, note


def estimate_salary(job, day, benchmarks):
    if not salary_missing(job.get("salary", "")):
        return None
    title = normalized(job["title"])
    # The reference table covers individual commercial roles, not leadership,
    # hospitality front-office work, training or internships.
    if re.search(r"\b(?:senior|head|lead|director|chief|\w*leitung\w*|\w*leiter\w*|praktik\w*|werkstudent\w*|"
                 r"ausbildung|intern|front office|back office)\b", title):
        return None
    matches = [entry for entry in benchmarks.get("roles", [])
               if any(re.search(pattern, title) for pattern in entry["title_patterns"])]
    if len(matches) != 1:
        return None
    reference = matches[0]
    checked_on = date.fromisoformat(reference["checked_on"])
    if not 0 <= (day - checked_on).days <= benchmarks["max_age_days"]:
        return None
    reference_hours = benchmarks["reference_weekly_hours"]
    lower_factor, upper_factor, basis = hours_basis(job, reference_hours)
    # Outward rounding avoids implying precision from a broad occupational benchmark.
    lower = math.floor(reference["annual_min_eur"] * lower_factor / 1000) * 1000
    upper = math.ceil(reference["annual_max_eur"] * upper_factor / 1000) * 1000
    return {
        "annual_min_eur": lower, "annual_max_eur": upper, "hours_basis": basis,
        "benchmark_role": reference["role"], "benchmark_region": benchmarks["region"],
        "source_name": reference["source_name"], "source_url": reference["source_url"],
        "source_checked_on": reference["checked_on"],
        "reference_annual_min_eur": reference["annual_min_eur"],
        "reference_annual_max_eur": reference["annual_max_eur"],
        "reference_weekly_hours": reference_hours, "method_version": METHOD_VERSION,
    }


def with_salary_estimates(report, day, benchmarks):
    """Enrich only the selected report; never replace advertised pay or past records."""
    jobs = []
    for original in report["jobs"]:
        job = dict(original)
        job.pop("salary_estimate", None)
        estimate = estimate_salary(job, day, benchmarks)
        if estimate:
            job["salary_estimate"] = estimate
        jobs.append(job)
    return dict(report, jobs=jobs)
