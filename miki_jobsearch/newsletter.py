"""Approved compact newsletter, rendered without scripts or webfont dependencies."""

import html
from datetime import date, timedelta

from .salaries import salary_missing

DESIGN_VERSION = "electric-blue-v1"
FONT = "'Helvetica Neue',Arial,sans-serif"
BLUE = "#0055FF"
INK = "#151515"
GREY = "#62625D"
ICON_BASE = "https://raw.githubusercontent.com/r4ph426/miki-job-crawler/main/assets/email-icons/v1"


def esc(value):
    return html.escape(str(value), quote=True)


def icon(name, blue=False):
    color = "blue" if blue else "grey"
    return (f'<img src="{ICON_BASE}/{name}-{color}.png" width="16" height="16" '
            'alt="" role="presentation" style="display:inline-block;vertical-align:-3px;border:0;margin-right:6px">')


def paragraph(text, small=False, gap=8):
    size, color = (12, GREY) if small else (14, INK)
    return (f'<p class="meta" style="margin:{gap}px 0 0;font-size:{size}px;line-height:1.42;'
            f'color:{color}">{text}</p>') if small else (
        f'<p style="margin:{gap}px 0 0;font-size:14px;line-height:1.42;color:{INK}">{text}</p>')


def label(text):
    return f'<em class="signal" style="font-style:italic;font-weight:400;color:{BLUE}">{text}</em>'


def notice(title, text):
    return ('<div class="notice" style="margin:16px 0;padding-left:12px;border-left:2px solid '
            f'{BLUE}"><p class="signal" style="margin:0 0 4px;font-size:12px;color:{BLUE}">'
            f'{icon("calendar-days", True)}{esc(title)}</p>{text}</div>')


def heading(text):
    return f'<h2 style="margin:0 0 8px;font-size:20px;line-height:1.2;font-weight:400">{esc(text)}</h2>'


def salary_paragraphs(job):
    estimate = job.get("salary_estimate") if salary_missing(job["salary"]) else None
    if not estimate:
        return paragraph(esc("Gehalt: " + job["salary"]), True, gap=2)
    lower = f'{estimate["annual_min_eur"]:,}'.replace(",", ".")
    upper = f'{estimate["annual_max_eur"]:,}'.replace(",", ".")
    checked = date.fromisoformat(estimate["source_checked_on"])
    source = (f'{estimate["benchmark_role"]}, {estimate["benchmark_region"]} · '
              f'Stand {checked:%d.%m.%Y}')
    return (paragraph(esc(f"Geschätzte Gehaltsspanne: ca. {lower}–{upper} € brutto/Jahr"), True, gap=2)
            + paragraph(esc(estimate["hours_basis"]), True, gap=2)
            + paragraph(f'Vergleich: {esc(source)} · <a class="signal" href="{esc(estimate["source_url"])}" '
                        f'style="color:{BLUE};text-decoration:underline">{esc(estimate["source_name"])}</a>', True, gap=2))


def render_report(report, day, history, fixture=False, revision=None):
    jobs = report["jobs"]
    weekly = day.weekday() == 4
    subject = (f"Wochenüberblick Stellensuche · KW {day.isocalendar().week}" if weekly
               else f"Stellen für Miki · {day:%d.%m.%Y} · {len(jobs)} neue Treffer")
    if revision:
        subject = f"Aktualisierte Stellenliste für Miki · {day:%d.%m.%Y} · {len(jobs)} geprüfte Treffer"
    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    body = ["<!doctype html><html lang='de'><head><meta charset='utf-8'>",
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">',
            f'<meta name="miki-design" content="{DESIGN_VERSION}">',
            '<style>@media(prefers-color-scheme:dark){body,.email{background:#191917!important;color:#F0F0EA!important}'
            'p,h1,h2,h3,td{color:#F0F0EA!important}.meta{color:#B5B5AC!important}'
            '.signal{color:#79A5FF!important}.notice{border-left-color:#79A5FF!important}'
            '.rule{border-top-color:#484841!important}}</style></head>',
            f'<body style="margin:0;padding:0;background:#FFFFFF;color:{INK};font-family:{FONT};font-weight:400">',
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">',
            f'<table class="email" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="max-width:620px;background:#FFFFFF;font-family:{FONT};text-align:left"><tr><td style="padding:24px 16px 16px">',
            f'<p class="meta" style="margin:0 0 8px;font-size:12px;line-height:1.35;color:{GREY}">Berlin &amp; Umgebung</p>',
            '<h1 style="margin:0;font-size:32px;line-height:1.05;letter-spacing:-.015em;font-weight:300">Stellen für Miki</h1>',
            paragraph(f"{weekdays[day.weekday()]}, {day:%d.%m.%Y} · {len(jobs)} geprüfte Treffer", True)]
    if fixture:
        body.append(paragraph("Testdaten: keine Live-Recherche, keine E-Mail versendet."))
    if revision:
        body.append(paragraph("Aktualisierung nach erneuter Recherche und Prüfung der Originalanzeigen. Diese Liste ersetzt den früheren heutigen Bericht."))
    summary, action = report["summary"], report["next_action"]
    if not jobs or report["rejected"] or report.get("assessment_pages_checked"):
        summary = f"{len(jobs)} neue, unabhängig verifizierte Treffer ab 55 Punkten."
        action = (f"Heute die Anzeige {jobs[0]['title']} bei {jobs[0]['employer']} prüfen und die Bewerbung vorbereiten "
                  f"(geschätzter Aufwand: {jobs[0]['effort_minutes']} Minuten)." if jobs else
                  "Heute 30 Minuten für den Bewerbungsüberblick einplanen: Rückmeldungen und offene Bewerbungen im eigenen Postfach prüfen.")
    body.append(paragraph("Hallo Miki, " + esc(summary), gap=16))
    outages = [s for s in report["source_checks"] if s["status"] != "ok"]
    notice_text = ""
    if not any(j.get("deadline") for j in jobs):
        notice_text += paragraph("Keine Bewerbungsfristen genannt. Bitte in den Originalanzeigen prüfen.", gap=0)
    if outages:
        notice_text += paragraph("Quellenausfälle: eingeschränkte Abdeckung", True, gap=4)
        notice_text += "".join(paragraph(f"{esc(s['group'])}: {esc(s['details'])}", True, gap=4) for s in outages)
    if notice_text:
        body.append(notice("Fristen & Recherche", notice_text))
    failures = [r for r in report["rejected"] if r["reason"] not in
                ["Already reported or repeated within this run", "Below score threshold", "Application deadline passed", "Expired"]]
    if failures:
        body.append(paragraph(f"{len(failures)} Kandidaten konnten nicht unabhängig geprüft werden und wurden nicht aufgenommen.", True))
    known = history + [dict(j, date=str(day)) for j in jobs]
    deadlines = [j for j in known if j.get("deadline") and str(day) <= j["deadline"] <= str(day + timedelta(days=5))]
    if deadlines:
        body.append(notice("Fristen in den nächsten fünf Tagen", "".join(
            paragraph(f"{esc(j['deadline'])}: {esc(j['title'])}, {esc(j['employer'])}; Bewerbungsstatus unbekannt", gap=4)
            for j in deadlines)))
    for number, job in enumerate(jobs, 1):
        body.extend([
            '<div class="rule" style="border-top:1px solid #D8D8D0;margin-top:24px;padding-top:16px">',
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="table-layout:fixed"><tr>',
            f'<td style="vertical-align:top;padding:0 8px 0 0;overflow-wrap:break-word;hyphens:auto">'
            f'<h2 style="margin:0;font-size:20px;line-height:1.2;font-weight:400;letter-spacing:-.005em">{number}. {esc(job["title"])}</h2></td>',
            f'<td class="signal" width="60" style="vertical-align:top;text-align:right;font-size:24px;line-height:1.15;'
            f'font-weight:400;color:{BLUE};white-space:nowrap">{job["score"]}<span style="font-size:12px;letter-spacing:0">/100</span></td></tr></table>',
            paragraph(esc(job["employer"]), gap=4),
            paragraph(icon("map-pin") + esc(job["district"]), True, gap=2),
            paragraph(esc(job["hours"] + " · " + job["contract"]), True),
            salary_paragraphs(job),
            paragraph(icon("train-front") + esc(job["commute"]), True, gap=2),
            paragraph(label("Dafür:") + "<br>" + esc(job["pro"])),
            paragraph(label("Dagegen:") + "<br>" + esc(job["con"]), gap=4),
            paragraph(f"Passung {job['scores']['skill']}/40 · Zugang {job['scores']['entry']}/30 · Pendeln {job['scores']['commute']}/20 · Bedingungen {job['scores']['conditions']}/10", True),
            paragraph(icon("clock-3", True) + f"<em>Aufwand:</em> {job['effort_minutes']} Min. ({esc(job['effort_details'])})", True),
            paragraph("<em>Beleg:</em> " + esc(job["evidence"]), True, gap=4),
            f'<p style="margin:4px 0 0"><a class="signal" href="{esc(job["url"])}" '
            f'style="display:inline-block;padding:12px 0 8px;font-size:14px;line-height:1.35;color:{BLUE};text-decoration:underline">'
            f'Anzeige öffnen {icon("arrow-up-right", True)}</a></p></div>',
        ])
    if not jobs:
        body.append(paragraph("Keine neuen, unabhängig verifizierten Treffer ab 55 Punkten. Das ist keine Aussage über den gesamten Stellenmarkt."))
    body.append('<div class="rule" style="border-top:1px solid #D8D8D0;margin-top:16px;padding-top:16px">')
    body.extend([heading("Vorschlag für heute"), paragraph(esc(action), gap=0), "</div>"])
    if weekly:
        monday = day - timedelta(days=4)
        week_jobs = [j for j in known if str(monday) <= j["date"] <= str(day)]
        bins = {"55–69": 0, "70–84": 0, "85–100": 0, "unter 55 (Althistorie)": 0, "unbekannt (Althistorie)": 0}
        for job in week_jobs:
            score = job.get("score")
            key = ("unbekannt (Althistorie)" if score is None else "85–100" if score >= 85
                   else "70–84" if score >= 70 else "55–69" if score >= 55 else "unter 55 (Althistorie)")
            bins[key] += 1
        body.append('<div class="rule" style="border-top:1px solid #D8D8D0;margin-top:24px;padding-top:16px">')
        body.extend([heading("Wochenüberblick"), paragraph(f"{len(week_jobs)} gemeldete Treffer einschließlich dieses Berichts.", gap=0)])
        body.extend(paragraph(f"{esc(key)} Punkte: {count}", True, gap=4) for key, count in bins.items())
        body.append(paragraph("Bewerbungsstatus: nicht erfasst.", True))
        body.append(paragraph("Fristen der nächsten 14 Tage"))
        future = [j for j in known if j.get("deadline") and str(day) <= j["deadline"] <= str(day + timedelta(days=14))]
        body.extend(paragraph(f"{esc(j['deadline'])}: {esc(j['title'])}, {esc(j['employer'])}", True, gap=4) for j in future)
        if not future:
            body.append(paragraph("Keine bestätigten Fristen in der gespeicherten Historie.", True))
        body.append(paragraph("Marktbeobachtung"))
        body.extend(paragraph(f"Familie {esc(key)}: {esc(value)}", True, gap=4) for key, value in report["family_observations"].items())
        body.extend([paragraph("Wochenende: " + esc(report["weekend_action"])), "</div>"])
    body.append('<div class="rule" style="border-top:1px solid #D8D8D0;margin-top:24px;padding-top:16px">')
    body.append(paragraph("Geprüfte Suchbegriffe", True, gap=0))
    body.extend(paragraph(f"{esc(family)} · {esc(', '.join(terms))}", True, gap=4) for family, terms in report["searches"].items())
    body.append(paragraph(f"Prüfstand: {day:%d.%m.%Y}. Die Anzeigen sind aktuell abrufbar; ihr Veröffentlichungsdatum ist nicht überall bekannt. Angaben vor einer Bewerbung in der Originalanzeige prüfen. Pendelzeiten und Bewerbungsaufwand sind Schätzungen.", True, gap=16))
    if any(j.get("salary_estimate") and salary_missing(j["salary"]) for j in jobs):
        body.append(paragraph("Geschätzte Gehaltsspannen sind gerundete Marktwerte ähnlicher Rollen in Berlin. "
                              "Der Arbeitgeber nennt kein Gehalt; sein Angebot kann abweichen. "
                              "Erfahrung, Branche und variable Vergütung sind nicht individuell berücksichtigt.", True, gap=8))
    body.append("</div></td></tr></table></td></tr></table></body></html>")
    return subject, "\n".join(body)
