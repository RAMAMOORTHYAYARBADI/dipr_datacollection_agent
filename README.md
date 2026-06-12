# DIPR TN — Agentic Pipeline

Single script that downloads, validates, and auto-fixes everything.

## Setup
```bash
pip install -r requirements.txt
```

## Run

### Full run (download + validate + fix)
```bash
python agent.py
```

### Validate + fix only (skip download, use existing PDFs)
```bash
python agent.py --validate-only
```

### Report only
```bash
python agent.py --report-only
```

## What it does automatically

| Phase | Action |
|-------|--------|
| 1. Download  | Scrapes website date-by-date, downloads all PDFs |
| 2. Validate  | Compares website vs disk vs manifest per date |
| 3. Auto-Fix  | Resolves every issue found |
| 4. Rebuild   | Rebuilds manifest from disk ground truth |
| 5. Final Check | Runs all 8 validation checks |
| 6. Report    | Saves full audit to reports/audit_TIMESTAMP.txt |

## Issues auto-fixed

- Missing PDFs → re-downloaded from website
- Wrong-date files in folder → moved or deleted
- Manifest entry, no file → re-downloaded
- File on disk, not in manifest → added to manifest
- Corrupt PDF → re-downloaded
- PR-??? → re-extracted with full regex
- Duplicate manifest entries → deduplicated
- Path date mismatch → corrected in manifest
