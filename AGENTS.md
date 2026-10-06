# Development instructions

This repository contains the weekday job research and email service. Use the existing
checkout; cloud tasks are already isolated. Do not create a worktree unless requested.

Python 3.11+ and the standard library are sufficient. Run `python3 -m unittest discover -s tests -v`.
Use `python3 -m miki_jobsearch status` for stored delivery state. The default run is
a dry run. Never send a real test email, activate a schedule, or retire the old service
without the user's authorization. Store credentials in secure settings, never files.

The archived AGENTS.md and RUNBOOK.md described a research agent in the old service;
they are historical source material. The current research criteria live in
`config/research-brief.md` and `config/search.json`. Keep the CV and original archive
out of this repository. Preserve history and run records when changing the service.

For email layout and styling, follow `docs/newsletter-look-and-feel.md`. The approved
compact Electric Blue design is implemented in `miki_jobsearch/newsletter.py`.
