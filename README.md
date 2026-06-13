# DIPR TN PDF Downloader

Downloads all press release PDFs from dipr.tn.gov.in
**Date range: 10 May 2026 → today**

## Setup

```bash
pip install -r requirements.txt
```

Chrome browser must be installed on your computer.

## Run

```bash
python download_pdfs.py
```

## What happens

1. Opens Chrome (headless — no window)
2. Loads dipr.tn.gov.in/press-release1.html
3. For each date (Mon–Sat), sets the date and waits for PDF links to load
4. Downloads every PDF into `pdfs/YYYY-MM-DD/filename.pdf`
5. Saves progress to `pdfs/manifest.json` after every date

## Re-run safely

If it stops midway, just run again.
Already-downloaded dates and files are skipped automatically.

## Output structure

```
pdfs/
├── manifest.json          ← tracks everything
├── download.log           ← full log
├── 2026-05-10/
│   ├── DIPR-P.R.No.001-....pdf
│   └── DIPR-P.R.No.002-....pdf
├── 2026-05-11/
│   └── ...
└── ...
```
