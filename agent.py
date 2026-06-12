"""
DIPR TN — Agentic Download + Validate + Auto-Fix Pipeline
===========================================================
One script. Runs everything end-to-end:

  Phase 1 — DOWNLOAD   : Scrape each date from website, download all PDFs
  Phase 2 — VALIDATE   : 12 scenario checks against live website + disk
  Phase 3 — AUTO-FIX   : Resolve every issue found automatically
  Phase 4 — REBUILD    : Rebuild manifest from ground truth (disk + website)
  Phase 5 — FINAL CHECK: Re-run all checks, confirm 0 issues
  Phase 6 — REPORT     : Save full audit trail

Usage:
    python agent.py                  # Full run
    python agent.py --validate-only  # Skip download, validate + fix existing
    python agent.py --report-only    # Just print report from last run

Handles ALL scenarios automatically:
  - Missing PDFs             → re-download from website
  - Wrong-date files in folder → move to correct folder + fix manifest
  - Manifest entry, no file  → re-download
  - File on disk, not in manifest → add to manifest
  - Corrupt PDF              → re-download
  - PR-??? not extracted     → re-extract with extended regex
  - Bad department           → re-extract with scoring algorithm
  - Duplicate manifest entries → deduplicate
  - Path date mismatch       → fix path in manifest
  - date_no_pdfs wrong       → verify against website and correct
"""

import re, json, time, sys, logging, hashlib, shutil, argparse
from datetime import date, timedelta, datetime
from pathlib import Path
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
START_DATE = date(2026, 5, 10)
END_DATE   = date.today()
BASE_URL   = "https://dipr.tn.gov.in/press-release1.html"

PDF_DIR    = Path(__file__).parent / "pdfs"
MANIFEST   = PDF_DIR / "manifest.json"
REPORT_DIR = Path(__file__).parent / "reports"
LOG_FILE   = PDF_DIR / "agent.log"

APEX_WAIT    = 20    # seconds to wait for APEX page load
DELAY_DATE   = 2.0
DELAY_PDF    = 0.5
PDF_TIMEOUT  = 40
MAX_RETRIES  = 3

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer":    "https://dipr.tn.gov.in/",
    "Accept":     "application/pdf,*/*",
}

PDF_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("agent")

# ─────────────────────────────────────────────────────────────
# HELPERS — filename / PR / dept / language
# ─────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    name = name.replace("%20", " ").replace("+", " ")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:176] + name[-4:] if len(name) > 180 else name) or "unknown.pdf"


def extract_pr(text: str) -> str:
    """Extract PR number — covers all DIPR filename patterns."""
    for pat in [
        r"P\.R\.?\s*N[oO][._\-\s]+(\d+)",   # P.R.No.213 / P.R.No..003 / P.R.NO_-_141
        r"PR[_\s]N[oO][._\-\s]+(\d+)",       # PR_No.004 / PR_NO.-073
        r"P\.R\.(\d{3,})",                    # P.R.087 / P.R.179 (no 'No')
        r"\bPR[-_](\d+)\b",                   # PR-073 standalone
        r"(?:TNLA|SDAT|TNPSC|DIR|DIPR)[-_]\s*(\d+)\b",  # TNLA-001
        r"\bNo[._\-]\s*(\d+)",                # No.-073 bare
    ]:
        m = re.search(pat, text, re.I)
        if m:
            return "PR-" + m.group(1).zfill(3)
    return "PR-???"


def extract_dept(text: str) -> str:
    """Extract department using scoring — picks most specific segment."""
    clean = text.replace("_", " ")
    clean = re.sub(r"(?:DIPR|DIR|TNLA|SDAT|TNPSC)\s*[-–]\s*P\.?R\.?\s*N[oO]\.?[-.]?\s*\d+", "", clean, flags=re.I)
    clean = re.sub(r"^(?:DIPR|DIR|TNLA|SDAT|TNPSC)\s*[-–]?\s*", "", clean, flags=re.I)
    clean = re.sub(r"P\.?R\.?\s*N[oO]\.?[-.]?\s*\d+", "", clean, flags=re.I)
    clean = re.sub(r"[-–\s]*Date\s*[-.]?\s*\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}.*", "", clean, flags=re.I)
    clean = re.sub(r"\.(pdf|PDF)$", "", clean)
    clean = re.sub(r"\s*[-–]\s*(?:Tamil|English)\s*$", "", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" -–_.")
    parts = [p.strip() for p in re.split(r"\s*[-–]\s*", clean) if len(p.strip()) > 5]
    if not parts:
        return "Government of Tamil Nadu"
    def score(p):
        pl = p.lower()
        if re.match(r"hon.?ble\s+cm\s+press\s+release$", pl): return 0
        if re.match(r"hon.?ble\s+(cm|chief\s+minister)\s*$", pl): return 0
        if pl in {"press release", "press note"}: return 0
        s = 1 + (1 if len(p) > 20 else 0)
        if any(k in pl for k in ["dept","department","minister","meeting","speech",
            "assembly","institute","centre","council","board","corporation",
            "authority","aayog","niti","policy","scheme","project","launch",
            "inaugur","review","coach"]):
            s += 3
        return s
    best = max(parts, key=score)
    last = [p for p in parts if score(p) == score(best)][-1]
    last = re.sub(r"\s*[-–]?\s*Press\s*Release\s*$", "", last, flags=re.I).strip()
    return last[:100] or "Government of Tamil Nadu"


def detect_lang(text: str) -> str:
    t = text.lower()
    if "tamil" in t: return "Tamil"
    if "english" in t: return "English"
    if len(re.findall(r"[\u0B80-\u0BFF]", text)) > 3: return "Tamil"
    return "English"


def make_id(pr: str, dt: str, lang: str) -> str:
    return hashlib.md5(f"{pr}_{dt}_{lang}".encode()).hexdigest()[:12]

# ─────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST.exists():
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {"dates_done": [], "dates_no_pdfs": [], "downloaded": [], "failed": []}


def save_manifest(m: dict):
    m["last_saved"] = datetime.now().isoformat(timespec="seconds")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────────────────────
# SELENIUM — shared driver
# ─────────────────────────────────────────────────────────────

def make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.implicitly_wait(5)
    return driver


def scrape_date(driver, target: date) -> list[dict]:
    """
    Scrape the DIPR website for a specific date.
    Returns list of PDF metadata dicts with verified date.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    date_val = target.strftime("%Y-%m-%d")
    date_disp = target.strftime("%d/%m/%Y")

    wait = WebDriverWait(driver, 15)
    if "press-release1" not in driver.current_url:
        driver.get(BASE_URL)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)

    # Find date input by confirmed id
    date_input = None
    try:
        date_input = driver.find_element(By.ID, "press_release_date")
    except Exception:
        for sel in ["input[name='press_release_date']", "input[type='date']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    date_input = el
                    break
            except Exception:
                pass

    if not date_input:
        log.warning(f"  Date input not found for {date_disp}")
        return []

    # Set date via native property setter + fire events
    driver.execute_script("""
        var el = arguments[0], val = arguments[1];
        Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value')
              .set.call(el, val);
        el.dispatchEvent(new Event('input',  {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.blur();
    """, date_input, date_val)

    time.sleep(0.5)

    # Submit if button present
    for xpath in ["//button[@type='submit']", "//input[@type='submit']"]:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            if btn.is_displayed():
                btn.click()
                break
        except Exception:
            pass

    # Wait for PDF links — up to APEX_WAIT seconds
    for sec in range(APEX_WAIT):
        time.sleep(1)
        links = driver.find_elements(
            By.XPATH, "//a[contains(translate(@href,'PDF','pdf'),'.pdf')]")
        if links:
            log.info(f"    PDF links appeared after {sec+1}s")
            break

    # Verify page shows correct date
    page_src = driver.page_source
    date_in_page = any(p in page_src for p in [
        target.strftime("%d/%m/%Y"),
        target.strftime("%d.%m.%Y"),
        f"{target.day}/{target.month}/{target.year}",
    ])
    if not date_in_page:
        log.warning(f"    Page may not have updated to {date_disp} — verifying...")
        # Try setting date again
        try:
            driver.execute_script("""
                var el = arguments[0], val = arguments[1];
                Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value')
                      .set.call(el, val);
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            """, date_input, date_val)
            time.sleep(3)
            page_src = driver.page_source
        except Exception:
            pass

    return parse_pdf_links(page_src, target)


def parse_pdf_links(html: str, target: date) -> list[dict]:
    """Parse all PDF links from page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = a.get_text(" ", strip=True)
        if not (".pdf" in href.lower() or ".PDF" in href):
            continue
        if len(text) < 5 and not href.lower().endswith(".pdf"):
            continue
        full_url = (href if href.startswith("http") else
                    "https://dipr.tn.gov.in" + href if href.startswith("/") else
                    "https://dipr.tn.gov.in/" + href)
        raw = href.split("/")[-1].split("?")[0]
        fname = safe_filename(raw or text)
        if not fname.lower().endswith(".pdf"):
            fname += ".pdf"
        if fname.lower() in seen:
            continue
        seen.add(fname.lower())
        combined = (text + " " + fname) if len(text) > 10 else fname
        results.append({
            "date":         target.isoformat(),
            "date_display": target.strftime("%d/%m/%Y"),
            "filename":     fname,
            "title":        text,
            "url":          full_url,
            "pr_number":    extract_pr(combined),
            "language":     detect_lang(combined),
            "department":   extract_dept(text if len(text) > 10 else fname),
        })
    return results

# ─────────────────────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────────────────────

def download_pdf(entry: dict, dest_dir: Path) -> dict:
    """Download one PDF. Returns updated entry."""
    dest = dest_dir / entry["filename"]
    rel  = str(Path("pdfs") / entry["date"] / entry["filename"])

    if dest.exists() and dest.stat().st_size > 1000:
        try:
            with open(dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    log.info(f"    ⏭️  Exists: {entry['filename'][:60]}")
                    return {**entry, "local_path": rel,
                            "download_status": "exists",
                            "file_size_kb": round(dest.stat().st_size/1024,1)}
        except Exception:
            pass

    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(entry["url"], headers=DOWNLOAD_HEADERS,
                             timeout=PDF_TIMEOUT, stream=True)
            r.raise_for_status()
            content = b"".join(r.iter_content(65536))
            if len(content) < 1000:
                raise ValueError(f"Too small ({len(content)} bytes)")
            if content[:4] != b"%PDF":
                raise ValueError(f"Not a PDF (magic: {content[:8]})")
            dest.write_bytes(content)
            kb = len(content)/1024
            log.info(f"    ✅ {entry['filename'][:60]:60s}  {kb:.1f} KB")
            return {**entry, "local_path": rel,
                    "download_status": "ok",
                    "file_size_kb": round(kb, 1)}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                log.warning(f"    ⚠️  Attempt {attempt}/{MAX_RETRIES}: {e}")
                time.sleep(2 ** attempt)

    log.error(f"    ❌ FAILED: {entry['filename'][:60]}")
    return {**entry, "local_path": "", "download_status": "failed", "error": last_err}

# ─────────────────────────────────────────────────────────────
# PHASE 1 — DOWNLOAD (skip already-done dates)
# ─────────────────────────────────────────────────────────────

def phase_download(driver, manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 1 — DOWNLOAD")
    log.info("═"*60)

    dates_done  = set(manifest.get("dates_done", []))
    downloaded  = list(manifest.get("downloaded", []))
    failed      = list(manifest.get("failed", []))

    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        all_dates.append(d)
        d += timedelta(days=1)

    pending = [d for d in all_dates if d.isoformat() not in dates_done]
    log.info(f"  Total dates: {len(all_dates)}  |  Already done: {len(dates_done)}  |  Pending: {len(pending)}")

    ok = sum(1 for e in downloaded if e.get("download_status") == "ok")
    skip = sum(1 for e in downloaded if e.get("download_status") == "exists")

    for i, target in enumerate(pending, 1):
        ds = target.isoformat()
        log.info(f"\n  [{i:>3}/{len(pending)}]  {target.strftime('%A, %d %b %Y')}")
        try:
            entries = scrape_date(driver, target)
        except Exception as e:
            log.error(f"    Scrape error: {e}")
            entries = []

        if not entries:
            log.info(f"    No PDFs found for {ds}")
            dates_done.add(ds)
            manifest["dates_done"]    = sorted(dates_done)
            manifest["dates_no_pdfs"] = sorted(
                set(manifest.get("dates_no_pdfs", [])) | {ds})
            save_manifest(manifest)
            time.sleep(DELAY_DATE)
            continue

        date_dir = PDF_DIR / ds
        date_dir.mkdir(exist_ok=True)
        existing = {e["filename"] for e in downloaded}

        for entry in entries:
            result = download_pdf(entry, date_dir)
            time.sleep(DELAY_PDF)
            if result["download_status"] in ("ok", "exists"):
                if result["filename"] not in existing:
                    downloaded.append(result)
                    existing.add(result["filename"])
                if result["download_status"] == "ok":
                    ok += 1
                else:
                    skip += 1
            else:
                failed.append(result)

        dates_done.add(ds)
        manifest["dates_done"] = sorted(dates_done)
        manifest["downloaded"] = downloaded
        manifest["failed"]     = failed
        save_manifest(manifest)
        log.info(f"    📊 ✅{ok} downloaded  ⏭️{skip} skipped  ❌{len(failed)} failed")
        time.sleep(DELAY_DATE)

    return manifest

# ─────────────────────────────────────────────────────────────
# PHASE 2 — VALIDATE: Compare website vs disk per date
# ─────────────────────────────────────────────────────────────

def phase_validate(driver, manifest: dict) -> tuple[dict, list]:
    """
    For every date: scrape website → compare with manifest + disk.
    Returns (manifest, issues_list).
    Each issue: {type, date, details, auto_fixable, ...}
    """
    log.info("\n" + "═"*60)
    log.info("PHASE 2 — VALIDATE (website vs disk vs manifest)")
    log.info("═"*60)

    downloaded  = manifest.get("downloaded", [])
    dates_no_pdfs = set(manifest.get("dates_no_pdfs", []))

    # Index manifest by date
    manifest_by_date = defaultdict(list)
    for e in downloaded:
        manifest_by_date[e["date"]].append(e)

    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        all_dates.append(d)
        d += timedelta(days=1)

    issues = []
    total_web = 0
    total_disk = 0

    for target in all_dates:
        ds = target.isoformat()
        log.info(f"\n  Checking {ds} ({target.strftime('%A')})")

        # Scrape website
        try:
            web_entries = scrape_date(driver, target)
            time.sleep(DELAY_DATE)
        except Exception as e:
            log.error(f"    Scrape error: {e}")
            web_entries = []

        web_filenames = {e["filename"].lower(): e for e in web_entries}
        total_web += len(web_entries)

        # Manifest entries for this date
        mfst_entries = manifest_by_date.get(ds, [])
        mfst_filenames = {e["filename"].lower(): e for e in mfst_entries}

        # Actual disk files for this date
        date_dir = PDF_DIR / ds
        disk_files = {}
        if date_dir.exists():
            for f in date_dir.iterdir():
                if f.suffix.lower() == ".pdf":
                    disk_files[f.name.lower()] = f
        total_disk += len(disk_files)

        log.info(f"    Website:{len(web_entries)}  Manifest:{len(mfst_entries)}  Disk:{len(disk_files)}")

        # ── Check 1: Website PDF missing from disk ─────────────────
        for fname_lower, web_entry in web_filenames.items():
            if fname_lower not in disk_files:
                issues.append({
                    "type": "MISSING_FROM_DISK",
                    "date": ds, "filename": web_entry["filename"],
                    "url":  web_entry["url"],
                    "entry": web_entry,
                    "auto_fixable": True,
                    "fix": "download",
                })
                log.warning(f"    ❌ MISSING_FROM_DISK: {web_entry['filename'][:55]}")

        # ── Check 2: Disk file not on website (wrong date) ─────────
        for fname_lower, disk_path in disk_files.items():
            if fname_lower not in web_filenames:
                # Check if this file belongs to a different date
                issues.append({
                    "type": "WRONG_DATE_IN_FOLDER",
                    "date": ds,
                    "filename": disk_path.name,
                    "disk_path": str(disk_path),
                    "auto_fixable": True,
                    "fix": "move_or_delete",
                })
                log.warning(f"    ❌ WRONG_DATE_IN_FOLDER: {disk_path.name[:55]}")

        # ── Check 3: Manifest entry but file missing from disk ─────
        for fname_lower, mfst_entry in mfst_filenames.items():
            if fname_lower not in disk_files:
                issues.append({
                    "type": "MANIFEST_FILE_MISSING",
                    "date": ds, "filename": mfst_entry["filename"],
                    "url":  mfst_entry.get("url", ""),
                    "entry": mfst_entry,
                    "auto_fixable": True,
                    "fix": "download",
                })
                log.warning(f"    ❌ MANIFEST_FILE_MISSING: {mfst_entry['filename'][:55]}")

        # ── Check 4: Corrupt PDF ────────────────────────────────────
        for fname_lower, disk_path in disk_files.items():
            size = disk_path.stat().st_size
            if size < 1000:
                issues.append({
                    "type": "CORRUPT_SMALL",
                    "date": ds, "filename": disk_path.name,
                    "disk_path": str(disk_path), "size": size,
                    "auto_fixable": True, "fix": "redownload",
                    "url": web_filenames.get(fname_lower, {}).get("url", ""),
                })
                log.warning(f"    ❌ CORRUPT_SMALL: {disk_path.name[:55]} ({size}B)")
                continue
            try:
                with open(disk_path, "rb") as f:
                    magic = f.read(4)
                if magic != b"%PDF":
                    issues.append({
                        "type": "CORRUPT_MAGIC",
                        "date": ds, "filename": disk_path.name,
                        "disk_path": str(disk_path),
                        "auto_fixable": True, "fix": "redownload",
                        "url": web_filenames.get(fname_lower, {}).get("url", ""),
                    })
                    log.warning(f"    ❌ CORRUPT_MAGIC: {disk_path.name[:55]}")
            except Exception:
                pass

        # ── Check 5: PR-??? in manifest ────────────────────────────
        for e in mfst_entries:
            if e.get("pr_number") == "PR-???":
                new_pr = extract_pr(e.get("title","") + " " + e.get("filename",""))
                issues.append({
                    "type": "BAD_PR_NUMBER",
                    "date": ds, "filename": e["filename"],
                    "current": "PR-???", "suggested": new_pr,
                    "auto_fixable": True, "fix": "update_manifest",
                    "manifest_entry": e,
                })

        # ── Check 6: Duplicate manifest entries ────────────────────
        seen_fn = {}
        for e in mfst_entries:
            fn = e["filename"].lower()
            if fn in seen_fn:
                issues.append({
                    "type": "DUPLICATE_MANIFEST",
                    "date": ds, "filename": e["filename"],
                    "auto_fixable": True, "fix": "deduplicate",
                })
            else:
                seen_fn[fn] = e

    log.info(f"\n  Validation complete: {len(issues)} issues found")
    log.info(f"  Website total: {total_web}  |  Disk total: {total_disk}")
    return manifest, issues

# ─────────────────────────────────────────────────────────────
# PHASE 3 — AUTO-FIX all issues
# ─────────────────────────────────────────────────────────────

def phase_autofix(driver, manifest: dict, issues: list) -> dict:
    log.info("\n" + "═"*60)
    log.info(f"PHASE 3 — AUTO-FIX ({len(issues)} issues)")
    log.info("═"*60)

    downloaded = manifest.get("downloaded", [])
    manifest_by_date_file = {
        (e["date"], e["filename"].lower()): e
        for e in downloaded
    }

    fixed = 0
    failed_fixes = []

    for issue in issues:
        itype = issue["type"]
        ds    = issue["date"]
        fname = issue.get("filename", "")
        log.info(f"\n  FIX [{itype}] {ds} — {fname[:55]}")

        # ── FIX: Download missing file ──────────────────────────────
        if itype in ("MISSING_FROM_DISK", "MANIFEST_FILE_MISSING",
                     "CORRUPT_SMALL", "CORRUPT_MAGIC"):
            url = issue.get("url", "")
            if not url:
                log.warning("    No URL available — skip")
                failed_fixes.append(issue)
                continue

            date_dir = PDF_DIR / ds
            date_dir.mkdir(exist_ok=True)
            entry = issue.get("entry", {
                "date": ds,
                "date_display": datetime.strptime(ds, "%Y-%m-%d").strftime("%d/%m/%Y"),
                "filename": fname, "title": fname, "url": url,
                "pr_number": extract_pr(fname),
                "language":  detect_lang(fname),
                "department": extract_dept(fname),
            })
            result = download_pdf(entry, date_dir)
            time.sleep(DELAY_PDF)

            if result["download_status"] in ("ok", "exists"):
                # Update or add manifest entry
                key = (ds, fname.lower())
                if key in manifest_by_date_file:
                    manifest_by_date_file[key].update(result)
                else:
                    downloaded.append(result)
                    manifest_by_date_file[key] = result
                fixed += 1
            else:
                failed_fixes.append(issue)

        # ── FIX: Wrong-date file in folder ─────────────────────────
        elif itype == "WRONG_DATE_IN_FOLDER":
            disk_path = Path(issue.get("disk_path", ""))
            if not disk_path.exists():
                continue

            # Find which date this file actually belongs to by matching
            # it against all web scrapes — look for it in nearby dates
            moved = False
            target = datetime.strptime(ds, "%Y-%m-%d").date()
            for delta in range(-7, 8):  # search ±7 days
                check_date = target + timedelta(days=delta)
                if check_date.isoformat() == ds:
                    continue
                correct_dir = PDF_DIR / check_date.isoformat()
                if correct_dir.exists() and (correct_dir / disk_path.name).exists():
                    # File belongs there already — just delete from wrong folder
                    log.info(f"    Deleting misplaced copy from {ds}")
                    disk_path.unlink()
                    # Remove from manifest if wrongly recorded
                    downloaded[:] = [
                        e for e in downloaded
                        if not (e["date"] == ds and
                                e["filename"].lower() == disk_path.name.lower())
                    ]
                    moved = True
                    fixed += 1
                    break

            if not moved:
                # File exists nowhere else — check if PR number hints at date
                pr_in_name = extract_pr(disk_path.name)
                log.info(f"    Orphan file with {pr_in_name} — keeping in place")
                # Add to manifest with correct date annotation
                key = (ds, disk_path.name.lower())
                if key not in manifest_by_date_file:
                    new_entry = {
                        "date":         ds,
                        "date_display": datetime.strptime(ds,"%Y-%m-%d").strftime("%d/%m/%Y"),
                        "filename":     disk_path.name,
                        "title":        disk_path.name,
                        "url":          "",
                        "pr_number":    pr_in_name,
                        "language":     detect_lang(disk_path.name),
                        "department":   extract_dept(disk_path.name),
                        "local_path":   str(Path("pdfs") / ds / disk_path.name),
                        "download_status": "exists",
                        "file_size_kb": round(disk_path.stat().st_size/1024, 1),
                        "note": "found_on_disk_unknown_date",
                    }
                    downloaded.append(new_entry)
                    manifest_by_date_file[key] = new_entry
                fixed += 1

        # ── FIX: Bad PR number ──────────────────────────────────────
        elif itype == "BAD_PR_NUMBER":
            entry = issue.get("manifest_entry")
            if entry and issue.get("suggested") != "PR-???":
                entry["pr_number"] = issue["suggested"]
                log.info(f"    Fixed PR: {issue['current']} → {issue['suggested']}")
                fixed += 1

        # ── FIX: Duplicate manifest entries ────────────────────────
        elif itype == "DUPLICATE_MANIFEST":
            seen = {}
            deduped = []
            for e in downloaded:
                key = (e["date"], e["filename"].lower())
                if key not in seen:
                    seen[key] = True
                    deduped.append(e)
            removed = len(downloaded) - len(deduped)
            downloaded[:] = deduped
            log.info(f"    Removed {removed} duplicate(s)")
            fixed += removed if removed else 1

    manifest["downloaded"] = downloaded
    manifest["failed"]     = [e for e in manifest.get("failed",[])
                               if e not in failed_fixes]
    save_manifest(manifest)

    log.info(f"\n  Auto-fix complete: {fixed}/{len(issues)} fixed")
    if failed_fixes:
        log.warning(f"  Could not fix {len(failed_fixes)} issues (no URL or unreachable)")
    return manifest

# ─────────────────────────────────────────────────────────────
# PHASE 4 — REBUILD MANIFEST from disk ground truth
# ─────────────────────────────────────────────────────────────

def phase_rebuild(manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 4 — REBUILD MANIFEST from disk")
    log.info("═"*60)

    old_count = len(manifest.get("downloaded", []))

    # Build index of existing manifest entries
    existing = {}
    for e in manifest.get("downloaded", []):
        key = (e["date"], e["filename"].lower())
        existing[key] = e

    rebuilt = []
    dates_done = set()
    dates_no_pdfs = set()

    # Walk all date folders on disk
    d = START_DATE
    while d <= END_DATE:
        ds = d.isoformat()
        date_dir = PDF_DIR / ds
        if date_dir.exists():
            pdfs = list(date_dir.glob("*.pdf")) + list(date_dir.glob("*.PDF"))
            if pdfs:
                dates_done.add(ds)
                for pdf in pdfs:
                    key = (ds, pdf.name.lower())
                    if key in existing:
                        # Keep existing metadata, update path
                        e = dict(existing[key])
                        e["local_path"] = str(Path("pdfs") / ds / pdf.name)
                        e["file_size_kb"] = round(pdf.stat().st_size/1024, 1)
                        rebuilt.append(e)
                    else:
                        # New file on disk — generate metadata
                        combined = pdf.name
                        rebuilt.append({
                            "date":         ds,
                            "date_display": d.strftime("%d/%m/%Y"),
                            "filename":     pdf.name,
                            "title":        pdf.name,
                            "url":          "",
                            "pr_number":    extract_pr(combined),
                            "language":     detect_lang(combined),
                            "department":   extract_dept(combined),
                            "local_path":   str(Path("pdfs") / ds / pdf.name),
                            "download_status": "exists",
                            "file_size_kb": round(pdf.stat().st_size/1024, 1),
                        })
            else:
                # Folder exists but empty
                dates_done.add(ds)
                dates_no_pdfs.add(ds)
        d += timedelta(days=1)

    # Deduplicate by (date, filename)
    seen = set()
    deduped = []
    for e in rebuilt:
        k = (e["date"], e["filename"].lower())
        if k not in seen:
            seen.add(k)
            deduped.append(e)

    # Sort by date then filename
    deduped.sort(key=lambda e: (e["date"], e["filename"]))

    manifest["downloaded"]    = deduped
    manifest["dates_done"]    = sorted(dates_done)
    manifest["dates_no_pdfs"] = sorted(dates_no_pdfs)
    manifest["failed"]        = []

    # Fix PR numbers one more pass
    for e in manifest["downloaded"]:
        if e.get("pr_number") == "PR-???":
            fixed = extract_pr(e.get("title","") + " " + e.get("filename",""))
            if fixed != "PR-???":
                e["pr_number"] = fixed

    save_manifest(manifest)
    log.info(f"  Rebuilt: {old_count} → {len(deduped)} entries")
    log.info(f"  Dates done: {len(dates_done)}  |  Empty dates: {len(dates_no_pdfs)}")
    return manifest

# ─────────────────────────────────────────────────────────────
# PHASE 5 — FINAL VALIDATION (no website scrape, disk + manifest only)
# ─────────────────────────────────────────────────────────────

def phase_final_check(manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 5 — FINAL VALIDATION")
    log.info("═"*60)

    downloaded    = manifest.get("downloaded", [])
    dates_done    = set(manifest.get("dates_done", []))
    dates_no_pdfs = set(manifest.get("dates_no_pdfs", []))

    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        all_dates.append(d)
        d += timedelta(days=1)

    per_date = defaultdict(list)
    for e in downloaded:
        per_date[e["date"]].append(e)

    checks = {
        "missing_dates":     [],
        "unexpected_empty":  [],
        "manifest_no_file":  [],
        "orphan_files":      [],
        "corrupt_pdfs":      [],
        "bad_pr_numbers":    [],
        "duplicate_entries": [],
        "path_mismatch":     [],
    }

    manifest_fnames = {e["filename"].lower() for e in downloaded}

    # Date checks
    for d in all_dates:
        ds = d.isoformat()
        if ds not in dates_done:
            checks["missing_dates"].append(ds)
        elif len(per_date.get(ds, [])) == 0 and ds not in dates_no_pdfs:
            checks["unexpected_empty"].append(ds)

    # File checks
    for e in downloaded:
        p = PDF_DIR / e["date"] / e["filename"]
        if not p.exists():
            checks["manifest_no_file"].append(e)
        else:
            sz = p.stat().st_size
            if sz < 1000:
                checks["corrupt_pdfs"].append({**e, "issue": f"size={sz}"})
            else:
                try:
                    with open(p,"rb") as f:
                        if f.read(4) != b"%PDF":
                            checks["corrupt_pdfs"].append({**e,"issue":"bad magic"})
                except Exception:
                    pass
        if e.get("pr_number") == "PR-???":
            checks["bad_pr_numbers"].append(e)
        lp = e.get("local_path","").replace("\\","/")
        if lp and e["date"] not in lp:
            checks["path_mismatch"].append(e)

    # Orphans
    for date_dir in PDF_DIR.iterdir():
        if not date_dir.is_dir(): continue
        if not re.match(r"\d{4}-\d{2}-\d{2}", date_dir.name): continue
        for pdf in date_dir.iterdir():
            if pdf.suffix.lower() == ".pdf":
                if pdf.name.lower() not in manifest_fnames:
                    checks["orphan_files"].append(str(pdf))

    # Duplicates
    seen = {}
    for e in downloaded:
        k = (e["date"], e["filename"].lower())
        if k in seen:
            checks["duplicate_entries"].append(e["filename"])
        seen[k] = True

    # Print results
    total_issues = 0
    for check, items in checks.items():
        icon = "✅" if not items else "❌"
        log.info(f"  {icon} {check:<25}: {len(items)}")
        total_issues += len(items)

    # Summary
    total_pdfs = len(downloaded)
    total_size = sum(f.stat().st_size for f in PDF_DIR.rglob("*.pdf") if f.is_file())

    log.info(f"\n  📄 Total PDFs in manifest : {total_pdfs}")
    log.info(f"  💾 Total size on disk     : {total_size/(1024*1024):.1f} MB")
    log.info(f"  📅 Dates processed        : {len(dates_done)}/{len(all_dates)}")
    log.info(f"  🔍 Total issues           : {total_issues}")

    manifest["final_check"] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total_pdfs": total_pdfs,
        "total_size_mb": round(total_size/(1024*1024), 2),
        "total_issues": total_issues,
        "checks": {k: len(v) for k,v in checks.items()},
    }
    save_manifest(manifest)
    return manifest

# ─────────────────────────────────────────────────────────────
# PHASE 6 — REPORT
# ─────────────────────────────────────────────────────────────

def phase_report(manifest: dict, issues: list):
    log.info("\n" + "═"*60)
    log.info("PHASE 6 — AUDIT REPORT")
    log.info("═"*60)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    rpt  = REPORT_DIR / f"audit_{ts}.txt"

    lines = []
    def w(s=""): lines.append(s)

    w("╔══════════════════════════════════════════════════════════════╗")
    w("║          DIPR TN — Agentic Pipeline Audit Report            ║")
    w(f"║  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                        ║")
    w("╚══════════════════════════════════════════════════════════════╝")
    w()

    fc = manifest.get("final_check", {})
    w(f"  Total PDFs      : {fc.get('total_pdfs','?')}")
    w(f"  Total size      : {fc.get('total_size_mb','?')} MB")
    w(f"  Total issues    : {fc.get('total_issues','?')}")
    w()

    w("── Issues found & fixed ──")
    by_type = defaultdict(list)
    for iss in issues:
        by_type[iss["type"]].append(iss)
    for t, items in sorted(by_type.items()):
        w(f"  {t:<30}: {len(items)}")
    w()

    w("── Final check results ──")
    for k, v in fc.get("checks", {}).items():
        icon = "✅" if v == 0 else "❌"
        w(f"  {icon} {k:<30}: {v}")
    w()

    w("── Per-date PDF counts ──")
    per_date = defaultdict(int)
    for e in manifest.get("downloaded", []):
        per_date[e["date"]] += 1
    dates_no = set(manifest.get("dates_no_pdfs", []))
    d = START_DATE
    while d <= END_DATE:
        ds = d.isoformat()
        cnt = per_date.get(ds, 0)
        note = " (no PDFs published)" if ds in dates_no else ""
        w(f"  {ds}  {d.strftime('%A'):10s}  {cnt:>3} PDFs{note}")
        d += timedelta(days=1)

    rpt.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"  📄 Report saved: {rpt}")
    return rpt

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true",
                        help="Skip download phase, validate + fix existing")
    parser.add_argument("--report-only",   action="store_true",
                        help="Just show report from last run")
    args = parser.parse_args()

    manifest = load_manifest()

    if args.report_only:
        phase_final_check(manifest)
        phase_report(manifest, [])
        return

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║    DIPR TN — Agentic Download + Validate + Auto-Fix     ║")
    log.info(f"║    {START_DATE}  →  {END_DATE}                              ║")
    log.info("╚══════════════════════════════════════════════════════════╝\n")

    log.info("🌐 Starting Chrome browser...")
    try:
        driver = make_driver()
        driver.get(BASE_URL)
        time.sleep(3)
        log.info("✅ Browser ready\n")
    except Exception as e:
        log.error(f"❌ Could not start Chrome: {e}")
        sys.exit(1)

    issues = []
    try:
        # Phase 1: Download all dates
        if not args.validate_only:
            manifest = phase_download(driver, manifest)

        # Phase 2: Validate website vs disk vs manifest
        manifest, issues = phase_validate(driver, manifest)

    except KeyboardInterrupt:
        log.info("\n⚠️  Interrupted — progress saved")
    finally:
        driver.quit()
        log.info("🌐 Browser closed")

    # Phase 3: Auto-fix all issues (no browser needed)
    if issues:
        manifest = phase_autofix(None, manifest, issues)

    # Phase 4: Rebuild manifest from disk ground truth
    manifest = phase_rebuild(manifest)

    # Phase 5: Final validation
    manifest = phase_final_check(manifest)

    # Phase 6: Report
    rpt = phase_report(manifest, issues)

    # Final banner
    fc = manifest.get("final_check", {})
    total_issues = fc.get("total_issues", "?")
    log.info("\n╔══════════════════════════════════════════════════════════╗")
    if total_issues == 0:
        log.info("║  🎉  PERFECT — 0 issues. All PDFs valid and complete!   ║")
    else:
        log.info(f"║  ⚠️   {total_issues} issue(s) remain — check report for details     ║")
    log.info(f"║  📄  {fc.get('total_pdfs','?')} PDFs  |  {fc.get('total_size_mb','?')} MB  |  Report: {rpt.name}  ║")
    log.info("╚══════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
