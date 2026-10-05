"""Import historical recommendations only; never extract a CV, secrets or instructions."""
import argparse
import json
import re
from pathlib import Path
from zipfile import ZipFile


def convert_history(text):
    rows = []
    for line in text.splitlines():
        if not re.match(r"^\d{4}-\d{2}-\d{2}\s*\|", line):
            continue
        parts = [p.strip() for p in line.split("|", 4)]
        if len(parts) != 5:
            raise ValueError("Malformed dated history row")
        date, score, title, employer, url = parts
        match = re.search(r"https://[^\s<>]+", url)
        if not match:
            raise ValueError("Historical recommendation has no HTTPS source URL")
        number = re.search(r"\d+", score)
        rows.append({"date": date, "score": int(number.group()) if number else None,
                     "title": title, "employer": employer, "url": match.group().rstrip(")."),
                     "origin": "archive; recorded as reported, delivery not independently verified"})
    if not rows:
        raise ValueError("Archive contains no historical recommendations")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; preserve it before importing a newer archive")
    with ZipFile(args.archive) as archive:
        candidates = [name for name in archive.namelist() if name.endswith("/ledger.md")]
        if len(candidates) != 1:
            parser.error("Expected exactly one ledger.md in the archive")
        history = convert_history(archive.read(candidates[0]).decode("utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Imported {len(history)} recommendations; unknown scores: {sum(j['score'] is None for j in history)}")


if __name__ == "__main__":
    main()
