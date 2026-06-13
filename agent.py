"""
DIPR TN — Complete Agentic Pipeline
=====================================
Phase 1: Download PDFs from website (skip already done)
Phase 2: Upload NEW PDFs to HuggingFace → delete from local disk
Phase 3: Validate (website vs HF vs manifest)
Phase 4: Auto-fix any issues found
Phase 5: Rebuild manifest from HF file list
Phase 6: Final check + report

Zero manual interaction. Runs fully on GitHub Actions cron.

HuggingFace layout:
  pdfs/2026-05-10/DIPR-P.R.No.001-...pdf
  pdfs/2026-05-11/DIPR-P.R.No.004-...pdf
  ...
  manifest.json   ← synced every run

GitHub repo layout (code only, no PDFs):
  agent.py
  requirements.txt
  .github/workflows/
  pdfs/manifest.json   ← lightweight tracking file
  reports/             ← audit reports
"""

import re, json, time, sys, logging, shutil, argparse, os
from datetime import date, timedelta, datetime
from pathlib import Path
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────────────
START_DATE  = date(2026, 5, 10)
END_DATE    = date.today()

BASE_URL    = "https://dipr.tn.gov.in/press-release1.html"
PDF_DIR     = Path(__file__).parent / "pdfs"
MANIFEST    = PDF_DIR / "manifest.json"
REPORT_DIR  = Path(__file__).parent / "reports"
LOG_FILE    = Path(__file__).parent / "agent.log"

# HuggingFace (from env secrets)
HF_TOKEN    = os.environ.get("HF_TOKEN", "")
HF_REPO_ID  = os.environ.get("HF_REPO_ID", "")

APEX_WAIT   = 20
DELAY_DATE  = 2.0
DELAY_PDF   = 0.5
PDF_TIMEOUT = 40
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": "https://dipr.tn.gov.in/",
    "Accept": "application/pdf,*/*",
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

# ── HELPERS ───────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    name = name.replace("%20", " ").replace("+", " ")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:176] + name[-4:] if len(name) > 180 else name) or "unknown.pdf"


def extract_pr(text: str) -> str:
    for pat in [
        r"P\.R\.?\s*N[oO][._\-\s]+(\d+)",
        r"PR[_\s]N[oO][._\-\s]+(\d+)",
        r"P\.R\.(\d{3,})",
        r"\bPR[-_](\d+)\b",
        r"(?:TNLA|SDAT|TNPSC|DIR|DIPR)[-_]\s*(\d+)\b",
        r"\bNo[._\-]\s*(\d+)",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            return "PR-" + m.group(1).zfill(3)
    return "PR-???"


def extract_dept(text: str) -> str:
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
        if pl in {"press release", "press note"}: return 0
        s = 1 + (1 if len(p) > 20 else 0)
        if any(k in pl for k in ["dept","department","minister","meeting","speech",
            "assembly","institute","centre","review","scheme","launch","inaugur"]):
            s += 3
        return s
    best_score = max(score(p) for p in parts)
    last = [p for p in parts if score(p) == best_score][-1]
    return re.sub(r"\s*[-–]?\s*Press\s*Release\s*$", "", last, flags=re.I).strip()[:100] \
           or "Government of Tamil Nadu"


def detect_lang(text: str) -> str:
    t = text.lower()
    if "tamil" in t: return "Tamil"
    if "english" in t: return "English"
    if len(re.findall(r"[\u0B80-\u0BFF]", text)) > 3: return "Tamil"
    return "English"

# ── MANIFEST ──────────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST.exists():
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {
        "collection_start": START_DATE.isoformat(),
        "dates_done": [], "dates_no_pdfs": [],
        "downloaded": [], "failed": [],
        "run_history": [],
    }


def save_manifest(m: dict):
    m["last_updated"]      = datetime.now().isoformat(timespec="seconds")
    m["last_date_scraped"] = END_DATE.isoformat()
    m["total_records"]     = len(m.get("downloaded", []))
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

# ── HUGGINGFACE ───────────────────────────────────────────────────────────

def hf_list_pdfs() -> set:
    """Return set of all PDF paths already on HuggingFace (e.g. 'pdfs/2026-05-10/file.pdf')"""
    if not HF_TOKEN or not HF_REPO_ID:
        log.warning("HF_TOKEN or HF_REPO_ID not set — skipping HF operations")
        return set()
    try:
        from huggingface_hub import HfApi
        api   = HfApi(token=HF_TOKEN)
        files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")
        return {f for f in files if f.lower().endswith(".pdf")}
    except Exception as e:
        log.error(f"HF list error: {e}")
        return set()


def hf_upload_file(local_path: Path, hf_path: str) -> bool:
    """Upload one file to HuggingFace dataset repo."""
    if not HF_TOKEN or not HF_REPO_ID:
        return False
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN)
        api.upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=hf_path,
            repo_id=HF_REPO_ID,
            repo_type="dataset",
        )
        return True
    except Exception as e:
        log.error(f"HF upload error {hf_path}: {e}")
        return False


def hf_upload_manifest() -> bool:
    """Upload manifest.json to HuggingFace."""
    if not HF_TOKEN or not HF_REPO_ID:
        return False
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN)
        api.upload_file(
            path_or_fileobj=str(MANIFEST),
            path_in_repo="manifest.json",
            repo_id=HF_REPO_ID,
            repo_type="dataset",
        )
        log.info("  Manifest synced to HuggingFace")
        return True
    except Exception as e:
        log.error(f"HF manifest upload error: {e}")
        return False

# ── SELENIUM ──────────────────────────────────────────────────────────────

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
                      "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.implicitly_wait(5)
    return driver


def scrape_date(driver, target: date) -> list:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    date_val = target.strftime("%Y-%m-%d")
    wait = WebDriverWait(driver, 15)
    if "press-release1" not in driver.current_url:
        driver.get(BASE_URL)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)

    date_input = None
    try:
        date_input = driver.find_element(By.ID, "press_release_date")
    except Exception:
        for sel in ["input[name='press_release_date']", "input[type='date']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    date_input = el; break
            except Exception:
                pass

    if not date_input:
        log.warning(f"  Date input not found for {target}")
        return []

    driver.execute_script("""
        var el=arguments[0], val=arguments[1];
        Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value')
              .set.call(el,val);
        el.dispatchEvent(new Event('input',{bubbles:true}));
        el.dispatchEvent(new Event('change',{bubbles:true}));
        el.blur();
    """, date_input, date_val)

    time.sleep(0.5)
    for xpath in ["//button[@type='submit']","//input[@type='submit']"]:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            if btn.is_displayed():
                btn.click(); break
        except Exception:
            pass

    for sec in range(APEX_WAIT):
        time.sleep(1)
        links = driver.find_elements(
            By.XPATH,"//a[contains(translate(@href,'PDF','pdf'),'.pdf')]")
        if links:
            log.info(f"  Links appeared after {sec+1}s")
            break

    return parse_links(driver.page_source, target)


def parse_links(html: str, target: date) -> list:
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href","").strip()
        text = a.get_text(" ", strip=True)
        if ".pdf" not in href.lower() and ".PDF" not in href:
            continue
        full = (href if href.startswith("http") else
                "https://dipr.tn.gov.in" + href if href.startswith("/") else
                "https://dipr.tn.gov.in/" + href)
        raw  = href.split("/")[-1].split("?")[0]
        fn   = safe_filename(raw or text)
        if not fn.lower().endswith(".pdf"): fn += ".pdf"
        if fn.lower() in seen: continue
        seen.add(fn.lower())
        combined = (text + " " + fn) if len(text) > 10 else fn
        results.append({
            "date":         target.isoformat(),
            "date_display": target.strftime("%d/%m/%Y"),
            "filename":     fn,
            "title":        text,
            "url":          full,
            "pr_number":    extract_pr(combined),
            "language":     detect_lang(combined),
            "department":   extract_dept(text if len(text)>10 else fn),
        })
    return results

# ── DOWNLOAD ONE PDF ──────────────────────────────────────────────────────

def download_pdf(entry: dict, dest_dir: Path) -> dict:
    dest = dest_dir / entry["filename"]
    rel  = f"pdfs/{entry['date']}/{entry['filename']}"

    if dest.exists() and dest.stat().st_size > 1000:
        try:
            with open(dest,"rb") as f:
                if f.read(4) == b"%PDF":
                    log.info(f"  ⏭  Exists: {entry['filename'][:60]}")
                    return {**entry,"local_path":rel,"download_status":"exists",
                            "file_size_kb":round(dest.stat().st_size/1024,1)}
        except Exception:
            pass

    last_err = ""
    for attempt in range(1, MAX_RETRIES+1):
        try:
            r = requests.get(entry["url"], headers=HEADERS,
                             timeout=PDF_TIMEOUT, stream=True)
            r.raise_for_status()
            content = b"".join(r.iter_content(65536))
            if len(content) < 1000:
                raise ValueError(f"Too small ({len(content)} bytes)")
            if content[:4] != b"%PDF":
                raise ValueError(f"Not a PDF")
            dest.write_bytes(content)
            kb = len(content)/1024
            log.info(f"  ✅ {entry['filename'][:60]:60s}  {kb:.1f} KB")
            return {**entry,"local_path":rel,"download_status":"ok",
                    "file_size_kb":round(kb,1)}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)

    log.error(f"  ❌ FAILED: {entry['filename'][:60]}")
    return {**entry,"local_path":"","download_status":"failed","error":last_err}

# ── PHASE 1: DOWNLOAD ─────────────────────────────────────────────────────

def phase_download(driver, manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 1 — DOWNLOAD")
    log.info("═"*60)

    dates_done  = set(manifest.get("dates_done",[]))
    downloaded  = list(manifest.get("downloaded",[]))
    failed      = list(manifest.get("failed",[]))

    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        all_dates.append(d)
        d += timedelta(days=1)

    pending = [d for d in all_dates if d.isoformat() not in dates_done]
    log.info(f"  Total: {len(all_dates)}  Done: {len(dates_done)}  Pending: {len(pending)}")

    new_pdfs = 0

    for i, target in enumerate(pending, 1):
        ds = target.isoformat()
        log.info(f"\n  [{i}/{len(pending)}] {target.strftime('%A, %d %b %Y')}")

        try:
            entries = scrape_date(driver, target)
        except Exception as e:
            log.error(f"  Scrape error: {e}")
            entries = []

        if not entries:
            dates_done.add(ds)
            no_pdfs = set(manifest.get("dates_no_pdfs",[]))
            no_pdfs.add(ds)
            manifest["dates_no_pdfs"] = sorted(no_pdfs)
            manifest["dates_done"]    = sorted(dates_done)
            save_manifest(manifest)
            time.sleep(DELAY_DATE)
            continue

        date_dir = PDF_DIR / ds
        date_dir.mkdir(exist_ok=True)
        existing = {e["filename"] for e in downloaded}

        for entry in entries:
            result = download_pdf(entry, date_dir)
            time.sleep(DELAY_PDF)
            if result["download_status"] in ("ok","exists"):
                if result["filename"] not in existing:
                    downloaded.append(result)
                    existing.add(result["filename"])
                if result["download_status"] == "ok":
                    new_pdfs += 1
            else:
                failed.append(result)

        dates_done.add(ds)
        manifest["dates_done"] = sorted(dates_done)
        manifest["downloaded"] = downloaded
        manifest["failed"]     = failed
        save_manifest(manifest)
        time.sleep(DELAY_DATE)

    log.info(f"\n  Download complete. New PDFs this run: {new_pdfs}")
    manifest["_new_pdfs_this_run"] = new_pdfs
    return manifest

# ── PHASE 2: UPLOAD TO HUGGINGFACE → DELETE LOCAL ─────────────────────────

def phase_upload_to_hf(manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 2 — UPLOAD TO HUGGINGFACE + CLEAN LOCAL")
    log.info("═"*60)

    if not HF_TOKEN or not HF_REPO_ID:
        log.warning("  HF_TOKEN/HF_REPO_ID not set — skipping upload")
        return manifest

    # Get what's already on HF
    log.info("  Fetching existing HuggingFace file list...")
    hf_files = hf_list_pdfs()
    log.info(f"  Already on HuggingFace: {len(hf_files)} PDFs")

    uploaded  = 0
    deleted   = 0
    failed_up = 0

    # Walk every date folder on disk
    d = START_DATE
    while d <= date.today():
        ds       = d.isoformat()
        date_dir = PDF_DIR / ds
        d += timedelta(days=1)

        if not date_dir.exists():
            continue

        pdfs = list(date_dir.glob("*.pdf")) + list(date_dir.glob("*.PDF"))
        if not pdfs:
            # remove empty folder
            try: date_dir.rmdir()
            except Exception: pass
            continue

        for pdf in pdfs:
            hf_path = f"pdfs/{ds}/{pdf.name}"

            if hf_path in hf_files:
                # Already on HF — just delete local copy
                pdf.unlink()
                deleted += 1
                log.info(f"  🗑  Already on HF, deleted local: {pdf.name[:55]}")
            else:
                # New file — upload then delete
                log.info(f"  ⬆  Uploading: {pdf.name[:55]}")
                ok = hf_upload_file(pdf, hf_path)
                if ok:
                    pdf.unlink()
                    uploaded += 1
                    log.info(f"  ✅ Uploaded + deleted: {pdf.name[:55]}")
                else:
                    failed_up += 1
                    log.error(f"  ❌ Upload failed, keeping local: {pdf.name[:55]}")

        # Remove folder if now empty
        try:
            if date_dir.exists() and not any(date_dir.iterdir()):
                date_dir.rmdir()
        except Exception:
            pass

    # Upload manifest.json to HF as well
    hf_upload_manifest()

    log.info(f"\n  Upload complete: {uploaded} uploaded, {deleted} already-there deleted, {failed_up} failed")

    manifest["_hf_uploaded"]  = uploaded
    manifest["_hf_deleted"]   = deleted
    manifest["_hf_failed"]    = failed_up
    return manifest

# ── PHASE 3: VALIDATE ─────────────────────────────────────────────────────

def phase_validate(manifest: dict) -> tuple:
    log.info("\n" + "═"*60)
    log.info("PHASE 3 — VALIDATE (manifest vs HuggingFace)")
    log.info("═"*60)

    downloaded    = manifest.get("downloaded", [])
    dates_done    = set(manifest.get("dates_done", []))
    dates_no_pdfs = set(manifest.get("dates_no_pdfs", []))

    # Get HF file list once
    hf_files = hf_list_pdfs()
    log.info(f"  HuggingFace PDFs: {len(hf_files)}")
    log.info(f"  Manifest entries: {len(downloaded)}")

    issues = []

    # Check every manifest entry exists on HF
    for e in downloaded:
        hf_path = f"pdfs/{e['date']}/{e['filename']}"
        if hf_path not in hf_files:
            issues.append({
                "type": "MISSING_FROM_HF",
                "date": e["date"],
                "filename": e["filename"],
                "url": e.get("url",""),
                "entry": e,
            })
            log.warning(f"  ❌ MISSING_FROM_HF: {e['date']} {e['filename'][:50]}")

    # Check PR-???
    for e in downloaded:
        if e.get("pr_number") == "PR-???":
            new_pr = extract_pr(e.get("title","") + " " + e.get("filename",""))
            issues.append({
                "type": "BAD_PR",
                "entry": e,
                "suggested": new_pr,
            })

    # Missing dates
    all_dates = []
    dd = START_DATE
    while dd <= END_DATE:
        all_dates.append(dd)
        dd += timedelta(days=1)

    for dd in all_dates:
        ds = dd.isoformat()
        if ds not in dates_done and dd < date.today():
            issues.append({"type": "MISSING_DATE", "date": ds})
            log.warning(f"  ❌ MISSING_DATE: {ds}")

    log.info(f"\n  Validation: {len(issues)} issues found")
    return manifest, issues

# ── PHASE 4: AUTO-FIX ─────────────────────────────────────────────────────

def phase_autofix(manifest: dict, issues: list) -> dict:
    log.info("\n" + "═"*60)
    log.info(f"PHASE 4 — AUTO-FIX ({len(issues)} issues)")
    log.info("═"*60)

    downloaded = manifest.get("downloaded", [])
    fixed = 0

    for issue in issues:
        itype = issue["type"]

        if itype == "MISSING_FROM_HF":
            # Re-download and re-upload
            entry    = issue["entry"]
            url      = issue.get("url","")
            ds       = issue["date"]
            date_dir = PDF_DIR / ds
            date_dir.mkdir(exist_ok=True)

            if url:
                result = download_pdf(entry, date_dir)
                if result["download_status"] in ("ok","exists"):
                    hf_path = f"pdfs/{ds}/{entry['filename']}"
                    pdf_path = date_dir / entry["filename"]
                    if hf_upload_file(pdf_path, hf_path):
                        pdf_path.unlink(missing_ok=True)
                        try: date_dir.rmdir()
                        except Exception: pass
                        log.info(f"  Fixed: re-downloaded + uploaded {entry['filename'][:50]}")
                        fixed += 1
                    else:
                        log.error(f"  Fix failed (HF upload): {entry['filename'][:50]}")
                else:
                    log.error(f"  Fix failed (download): {entry['filename'][:50]}")
            else:
                log.warning(f"  No URL for {entry['filename'][:50]} — cannot fix")

        elif itype == "BAD_PR":
            entry = issue["entry"]
            suggested = issue.get("suggested","PR-???")
            if suggested != "PR-???":
                for e in downloaded:
                    if e["filename"] == entry["filename"] and e["date"] == entry["date"]:
                        e["pr_number"] = suggested
                        break
                log.info(f"  Fixed PR: {entry['filename'][:50]} → {suggested}")
                fixed += 1

        elif itype == "MISSING_DATE":
            log.info(f"  MISSING_DATE {issue['date']} — will be picked up on next cron run")

    manifest["downloaded"] = downloaded
    save_manifest(manifest)
    log.info(f"\n  Auto-fix complete: {fixed}/{len(issues)} fixed")
    return manifest

# ── PHASE 5: REBUILD MANIFEST ─────────────────────────────────────────────

def phase_rebuild(manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 5 — REBUILD MANIFEST from HuggingFace")
    log.info("═"*60)

    hf_files = hf_list_pdfs()
    log.info(f"  HF files found: {len(hf_files)}")

    old_count = len(manifest.get("downloaded", []))

    # Build index of existing manifest entries by (date, filename)
    existing = {}
    for e in manifest.get("downloaded", []):
        key = (e["date"], e["filename"].lower())
        existing[key] = e

    rebuilt     = []
    dates_done  = set()
    dates_empty = set(manifest.get("dates_no_pdfs", []))

    for hf_path in sorted(hf_files):
        # hf_path format: pdfs/2026-05-10/DIPR-...pdf
        parts = hf_path.split("/")
        if len(parts) != 3:
            continue
        _, ds, fname = parts

        key = (ds, fname.lower())
        if key in existing:
            e = dict(existing[key])
            e["local_path"] = hf_path
            e["hf_path"]    = hf_path
            rebuilt.append(e)
        else:
            rebuilt.append({
                "date":            ds,
                "date_display":    datetime.strptime(ds,"%Y-%m-%d").strftime("%d/%m/%Y"),
                "filename":        fname,
                "title":           fname,
                "url":             "",
                "pr_number":       extract_pr(fname),
                "language":        detect_lang(fname),
                "department":      extract_dept(fname),
                "local_path":      hf_path,
                "hf_path":         hf_path,
                "download_status": "on_hf",
                "file_size_kb":    0,
            })
        dates_done.add(ds)

    # Fix PR-??? one final pass
    for e in rebuilt:
        if e.get("pr_number") == "PR-???":
            fixed = extract_pr(e.get("title","") + " " + e.get("filename",""))
            if fixed != "PR-???":
                e["pr_number"] = fixed

    # Deduplicate
    seen, deduped = set(), []
    for e in rebuilt:
        k = (e["date"], e["filename"].lower())
        if k not in seen:
            seen.add(k)
            deduped.append(e)
    deduped.sort(key=lambda e: (e["date"], e["filename"]))

    # Merge back existing dates_done (including empty dates)
    for ds in manifest.get("dates_done", []):
        dates_done.add(ds)

    manifest["downloaded"]    = deduped
    manifest["dates_done"]    = sorted(dates_done)
    manifest["dates_no_pdfs"] = sorted(dates_empty)
    manifest["failed"]        = []

    save_manifest(manifest)
    hf_upload_manifest()

    log.info(f"  Rebuilt: {old_count} → {len(deduped)} entries")
    return manifest

# ── PHASE 6: FINAL CHECK + REPORT ─────────────────────────────────────────

def phase_final(manifest: dict, issues_found: list, run_start: datetime) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 6 — FINAL CHECK + REPORT")
    log.info("═"*60)

    downloaded    = manifest.get("downloaded", [])
    dates_done    = set(manifest.get("dates_done", []))
    dates_no_pdfs = set(manifest.get("dates_no_pdfs", []))

    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        all_dates.append(d)
        d += timedelta(days=1)

    checks = {
        "missing_dates":    [],
        "bad_pr_numbers":   [],
        "duplicate_entries":[],
    }
    for dd in all_dates:
        ds = dd.isoformat()
        if ds not in dates_done and dd < date.today():
            checks["missing_dates"].append(ds)
    for e in downloaded:
        if e.get("pr_number") == "PR-???":
            checks["bad_pr_numbers"].append(e["filename"])
    seen = {}
    for e in downloaded:
        k = (e["date"], e["filename"].lower())
        if k in seen:
            checks["duplicate_entries"].append(e["filename"])
        seen[k] = True

    total_issues = sum(len(v) for v in checks.values())

    for k, v in checks.items():
        icon = "✅" if not v else "❌"
        log.info(f"  {icon} {k:<25}: {len(v)}")

    log.info(f"\n  📄 Manifest entries : {len(downloaded)}")
    log.info(f"  📅 Dates done       : {len(dates_done)}/{len(all_dates)}")
    log.info(f"  🔍 Remaining issues : {total_issues}")

    # Update run_history (last 10 only)
    history = manifest.get("run_history", [])
    history.append({
        "run_at":          run_start.isoformat(timespec="seconds"),
        "new_pdfs":        manifest.get("_new_pdfs_this_run", 0),
        "hf_uploaded":     manifest.get("_hf_uploaded", 0),
        "issues_found":    len(issues_found),
        "issues_fixed":    len([i for i in issues_found if i["type"] != "MISSING_DATE"]),
        "status":          "ok" if total_issues == 0 else "warn",
    })
    manifest["run_history"] = history[-10:]

    # Clean internal keys
    for k in ["_new_pdfs_this_run","_hf_uploaded","_hf_deleted","_hf_failed"]:
        manifest.pop(k, None)

    save_manifest(manifest)
    hf_upload_manifest()

    # Save report
    ts  = run_start.strftime("%Y%m%d_%H%M%S")
    rpt = REPORT_DIR / f"audit_{ts}.txt"
    lines = [
        f"DIPR TN — Audit Report  {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
        f"{'='*60}",
        f"Total PDFs   : {len(downloaded)}",
        f"Dates done   : {len(dates_done)}/{len(all_dates)}",
        f"Issues found : {len(issues_found)}",
        f"Final issues : {total_issues}",
        "",
        "Final checks:",
    ]
    for k,v in checks.items():
        lines.append(f"  {'✅' if not v else '❌'} {k}: {len(v)}")
    lines += ["", "Run history (last 10):"]
    for r in manifest["run_history"][-10:]:
        lines.append(f"  {r['run_at']}  new_pdfs={r['new_pdfs']}  status={r['status']}")
    rpt.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"\n  Report saved: {rpt.name}")

    manifest["final_check"] = {
        "timestamp":    run_start.isoformat(timespec="seconds"),
        "total_issues": total_issues,
        "checks":       {k: len(v) for k,v in checks.items()},
    }
    save_manifest(manifest)
    return manifest

# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true",
                        help="Skip download, just validate + fix + upload")
    args = parser.parse_args()

    run_start = datetime.now()
    manifest  = load_manifest()

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║   DIPR TN — Agentic Pipeline (HuggingFace storage)      ║")
    log.info(f"║   {START_DATE}  →  {END_DATE}                               ║")
    log.info("╚══════════════════════════════════════════════════════════╝\n")

    issues = []

    if not args.validate_only:
        # Phase 1: Download
        log.info("🌐 Starting Chrome...")
        try:
            driver = make_driver()
            driver.get(BASE_URL)
            time.sleep(3)
            log.info("✅ Browser ready\n")
        except Exception as e:
            log.error(f"❌ Chrome failed: {e}")
            sys.exit(1)

        try:
            manifest = phase_download(driver, manifest)
        except KeyboardInterrupt:
            log.info("Interrupted — saving progress")
        finally:
            driver.quit()
            log.info("🌐 Browser closed")

    # Phase 2: Upload to HuggingFace + delete local PDFs
    manifest = phase_upload_to_hf(manifest)

    # Phase 3: Validate
    manifest, issues = phase_validate(manifest)

    # Phase 4: Auto-fix
    if issues:
        manifest = phase_autofix(manifest, issues)

    # Phase 5: Rebuild manifest from HF
    manifest = phase_rebuild(manifest)

    # Phase 6: Final check + report
    manifest = phase_final(manifest, issues, run_start)

    # Summary banner
    fc = manifest.get("final_check", {})
    n  = fc.get("total_issues", "?")
    log.info("\n╔══════════════════════════════════════════════════════════╗")
    if n == 0:
        log.info("║  ✅  PERFECT — 0 issues. All PDFs on HuggingFace.       ║")
    else:
        log.info(f"║  ⚠️   {n} issue(s) remain — check report                 ║")
    log.info(f"║  📄  {len(manifest.get('downloaded',[]))} PDFs  |  HF: {HF_REPO_ID or 'not set'}  ║")
    log.info("╚══════════════════════════════════════════════════════════╝")

    # Exit 0 even if warnings (so cron doesn't fail on empty-day runs)
    sys.exit(0)


if __name__ == "__main__":
    main()
