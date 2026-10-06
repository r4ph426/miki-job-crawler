# Salary estimates

When a listing does not disclose pay, the service adds **Geschätzte Gehaltsspanne**
to the email. `salary` remains the employer's original disclosure (or unknown).
The derived `salary_estimate` is saved separately in the report and delivery history,
with the reference role, Berlin region, source URL, retrieval date, original range,
hours calculation and method version. It does not change job scores or exclusions.

`config/salary-benchmarks.json` contains twelve occupational ranges read from
StepStone's Berlin salary pages on 6 October 2026. These statistical comparisons
do not describe the individual employer's offer. The email links to the exact
reference page and shows the retrieval date. Industry, seniority, personal experience
and variable pay are not individually modelled. Sources give annual gross figures;
40 weekly hours is the explicit full-time comparison assumption.

Match the job title to one reference role; never infer pay from its A/B/C search
family or score. Unmatched, ambiguous, leadership, senior, internship and
hospitality front-office titles retain unknown pay. Employer-stated amounts and
tariff grades take precedence, including when there is no numeric range.

Explicit weekly hours scale the reference range proportionally before rounding
the lower bound down and upper bound up to whole thousands of euros. For example,
the Officemanager/in comparison is €33,000–45,200; at 30 hours, the email shows
approximately €24,000–34,000 gross/year and the 30/40 hours basis. A weekly-hours
range uses its lower and upper endpoints. Unspecified or conflicting hours show a
clearly labelled full-time equivalent. No actual part-time income is invented.

References expire after 180 days; future-dated references are also ineligible.
Refresh the values and `checked_on` only after opening and checking the linked
salary pages. The retrieval date is not the underlying survey period. If a source
cannot be checked, retain its old date; once expired, no estimate is produced.
Extend title matching only when the occupational comparison is appropriate, and
keep any proxy role visible in the email.

Enrichment happens after live verification and selection, before rendering.
An unsent report can gain an estimate on retry. A sent, sending, uncertain or partial
record remains protected by the existing delivery guard; this feature never
resends or rewrites a previously delivered email. Today's stored report can be
rendered as a separate local preview without editing its delivery record.
