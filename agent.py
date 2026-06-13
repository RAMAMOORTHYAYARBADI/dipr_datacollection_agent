"""
DIPR TN — Complete Agentic Pipeline
GitHub Actions compatible Chrome setup
"""

import re, json, time, sys, logging, argparse, os
from datetime import date, timedelta, datetime
from pathlib import Path
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────────────
START_DATE = date(2026, 5, 10)
END_DATE   = date.today()

BASE_URL   = "https://dipr.tn.gov.in/press-release1.html"
PDF_DIR    = Path(__file__).parent / "pdfs"
MANIFEST   = PDF_DIR / "manifest.json"
REPORT_DIR = Path(__file__).parent / "reports"
LOG_FILE   = Path(__file__).parent / "agent.log"

HF_TOKEN   = os.environ.get("HF_TOKEN", "")
HF_REPO_ID = os.environ.get("HF_REPO_ID", "")

APEX_WAIT   = 25
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
    last = [p for p in parts if score(p) == max(score(p2) for p2 in parts)][-1]
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
        "downloaded": [], "failed": [], "run_history": [],
    }


def save_manifest(m: dict):
    m["last_updated"]      = datetime.now().isoformat(timespec="seconds")
    m["last_date_scraped"] = END_DATE.isoformat()
    m["total_records"]     = len(m.get("downloaded", []))
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

# ── HUGGINGFACE ───────────────────────────────────────────────────────────

def hf_list_pdfs() -> set:
    if not HF_TOKEN or not HF_REPO_ID:
        log.warning("HF secrets not set — skipping HF operations")
        return set()
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN)
        files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type="dataset")
        return {f for f in files if f.lower().endswith(".pdf")}
    except Exception as e:
        log.error(f"HF list error: {e}")
        return set()


def hf_upload_file(local_path: Path, hf_path: str) -> bool:
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


def hf_upload_manifest():
    if not HF_TOKEN or not HF_REPO_ID:
        return
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
    except Exception as e:
        log.error(f"HF manifest upload: {e}")

# ── CHROME DRIVER (GitHub Actions compatible) ─────────────────────────────

def make_driver():
    """
    GitHub Actions compatible Chrome setup.

    Problem: webdriver-manager downloads chromedriver but Chrome binary
    path on GitHub Actions runners is non-standard, causing timeout.

    Fix: explicitly find the Chrome binary installed by
    browser-actions/setup-chrome and pass it to ChromeOptions.
    """
    import subprocess
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--remote-debugging-port=0")  # avoids port conflict
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    )

    # ── Find Chrome binary ─────────────────────────────────────────────
    # browser-actions/setup-chrome installs to known paths on GitHub runners
    chrome_candidates = [
        # browser-actions/setup-chrome path (GitHub Actions)
        "/opt/hostedtoolcache/setup-chrome/google-chrome/stable/x64/chrome",
        "/opt/hostedtoolcache/setup-chrome/chromium/stable/x64/chrome",
        # Standard Linux paths
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        # snap
        "/snap/bin/chromium",
    ]

    chrome_binary = None
    for path in chrome_candidates:
        if Path(path).exists():
            chrome_binary = path
            log.info(f"  Found Chrome binary: {path}")
            break

    if not chrome_binary:
        # Last resort: ask the OS
        try:
            result = subprocess.run(
                ["which", "google-chrome"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                chrome_binary = result.stdout.strip()
                log.info(f"  Found Chrome via which: {chrome_binary}")
        except Exception:
            pass

    if chrome_binary:
        opts.binary_location = chrome_binary
    else:
        log.warning("  Chrome binary not found via path scan — using system default")

    # ── Find ChromeDriver ──────────────────────────────────────────────
    # Try to find chromedriver that matches the installed Chrome
    chromedriver_candidates = [
        # browser-actions/setup-chrome also installs chromedriver
        "/opt/hostedtoolcache/setup-chrome/google-chrome/stable/x64/chromedriver",
        "/opt/hostedtoolcache/setup-chrome/chromium/stable/x64/chromedriver",
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ]

    chromedriver_path = None
    for path in chromedriver_candidates:
        if Path(path).exists():
            chromedriver_path = path
            log.info(f"  Found chromedriver: {path}")
            break

    if chromedriver_path:
        svc = Service(chromedriver_path)
    else:
        # Fall back to webdriver-manager
        log.info("  Using webdriver-manager for chromedriver")
        from webdriver_manager.chrome import ChromeDriverManager
        svc = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(30)
    driver.implicitly_wait(5)
    return driver

# ── SCRAPE DATE ───────────────────────────────────────────────────────────

def scrape_date(driver, target: date) -> list:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    date_val = target.strftime("%Y-%m-%d")
    wait     = WebDriverWait(driver, 15)

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
            if btn.is_displayed(): btn.click(); break
        except Exception:
            pass

    for sec in range(APEX_WAIT):
        time.sleep(1)
        links = driver.find_elements(
            By.XPATH, "//a[contains(translate(@href,'PDF','pdf'),'.pdf')]")
        if links:
            log.info(f"  Links appeared after {sec+1}s ({len(links)} links)")
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

# ── DOWNLOAD ──────────────────────────────────────────────────────────────

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
                raise ValueError("Not a PDF")
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

    dates_done = set(manifest.get("dates_done",[]))
    downloaded = list(manifest.get("downloaded",[]))
    failed     = list(manifest.get("failed",[]))

    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        all_dates.append(d); d += timedelta(days=1)

    pending  = [d for d in all_dates if d.isoformat() not in dates_done]
    new_pdfs = 0
    log.info(f"  Total:{len(all_dates)}  Done:{len(dates_done)}  Pending:{len(pending)}")

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
            no_pdfs = set(manifest.get("dates_no_pdfs",[])); no_pdfs.add(ds)
            manifest.update({"dates_no_pdfs": sorted(no_pdfs),
                             "dates_done": sorted(dates_done)})
            save_manifest(manifest)
            time.sleep(DELAY_DATE); continue

        date_dir = PDF_DIR / ds
        date_dir.mkdir(exist_ok=True)
        existing = {e["filename"] for e in downloaded}

        for entry in entries:
            result = download_pdf(entry, date_dir)
            time.sleep(DELAY_PDF)
            if result["download_status"] in ("ok","exists"):
                if result["filename"] not in existing:
                    downloaded.append(result); existing.add(result["filename"])
                if result["download_status"] == "ok":
                    new_pdfs += 1
            else:
                failed.append(result)

        dates_done.add(ds)
        manifest.update({"dates_done": sorted(dates_done),
                         "downloaded": downloaded, "failed": failed})
        save_manifest(manifest)
        time.sleep(DELAY_DATE)

    manifest["_new_pdfs"] = new_pdfs
    log.info(f"\n  Download complete. New PDFs: {new_pdfs}")
    return manifest

# ── PHASE 2: UPLOAD TO HF + DELETE LOCAL ─────────────────────────────────

def phase_upload_to_hf(manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 2 — UPLOAD TO HUGGINGFACE + CLEAN LOCAL")
    log.info("═"*60)

    if not HF_TOKEN or not HF_REPO_ID:
        log.warning("  HF secrets not set — skipping")
        return manifest

    hf_files = hf_list_pdfs()
    log.info(f"  Already on HF: {len(hf_files)} PDFs")

    uploaded = deleted = failed = 0

    d = START_DATE
    while d <= date.today():
        ds       = d.isoformat()
        date_dir = PDF_DIR / ds
        d += timedelta(days=1)
        if not date_dir.exists(): continue

        pdfs = list(date_dir.glob("*.pdf")) + list(date_dir.glob("*.PDF"))
        for pdf in pdfs:
            hf_path = f"pdfs/{ds}/{pdf.name}"
            if hf_path in hf_files:
                pdf.unlink(); deleted += 1
                log.info(f"  🗑  Already on HF, deleted: {pdf.name[:50]}")
            else:
                log.info(f"  ⬆  Uploading: {pdf.name[:50]}")
                if hf_upload_file(pdf, hf_path):
                    pdf.unlink(); uploaded += 1
                    log.info(f"  ✅ Uploaded + deleted: {pdf.name[:50]}")
                else:
                    failed += 1

        try:
            if date_dir.exists() and not any(date_dir.iterdir()):
                date_dir.rmdir()
        except Exception:
            pass

    hf_upload_manifest()
    log.info(f"  Uploaded:{uploaded}  Already-deleted:{deleted}  Failed:{failed}")
    manifest.update({"_hf_uploaded": uploaded, "_hf_deleted": deleted})
    return manifest

# ── PHASE 3: VALIDATE ─────────────────────────────────────────────────────

def phase_validate(manifest: dict) -> tuple:
    log.info("\n" + "═"*60)
    log.info("PHASE 3 — VALIDATE")
    log.info("═"*60)

    downloaded    = manifest.get("downloaded", [])
    dates_done    = set(manifest.get("dates_done", []))
    hf_files      = hf_list_pdfs()
    issues        = []

    log.info(f"  HF PDFs:{len(hf_files)}  Manifest entries:{len(downloaded)}")

    for e in downloaded:
        hf_path = f"pdfs/{e['date']}/{e['filename']}"
        if hf_path not in hf_files:
            issues.append({"type":"MISSING_FROM_HF","date":e["date"],
                           "filename":e["filename"],"url":e.get("url",""),"entry":e})
            log.warning(f"  ❌ MISSING_FROM_HF: {e['date']} {e['filename'][:45]}")

    for e in downloaded:
        if e.get("pr_number") == "PR-???":
            new_pr = extract_pr(e.get("title","") + " " + e.get("filename",""))
            issues.append({"type":"BAD_PR","entry":e,"suggested":new_pr})

    all_dates = []
    dd = START_DATE
    while dd <= END_DATE:
        all_dates.append(dd); dd += timedelta(days=1)

    for dd in all_dates:
        ds = dd.isoformat()
        if ds not in dates_done and dd < date.today():
            issues.append({"type":"MISSING_DATE","date":ds})

    log.info(f"  Issues found: {len(issues)}")
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
            entry    = issue["entry"]
            ds       = issue["date"]
            url      = issue.get("url","")
            date_dir = PDF_DIR / ds
            date_dir.mkdir(exist_ok=True)
            if url:
                result = download_pdf(entry, date_dir)
                if result["download_status"] in ("ok","exists"):
                    hf_path  = f"pdfs/{ds}/{entry['filename']}"
                    pdf_path = date_dir / entry["filename"]
                    if hf_upload_file(pdf_path, hf_path):
                        pdf_path.unlink(missing_ok=True)
                        try: date_dir.rmdir()
                        except Exception: pass
                        fixed += 1
        elif itype == "BAD_PR":
            entry = issue["entry"]
            sug   = issue.get("suggested","PR-???")
            if sug != "PR-???":
                for e in downloaded:
                    if e["filename"]==entry["filename"] and e["date"]==entry["date"]:
                        e["pr_number"] = sug; break
                fixed += 1

    manifest["downloaded"] = downloaded
    save_manifest(manifest)
    log.info(f"  Fixed: {fixed}/{len(issues)}")
    return manifest

# ── PHASE 5: REBUILD ──────────────────────────────────────────────────────

def phase_rebuild(manifest: dict) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 5 — REBUILD MANIFEST from HuggingFace")
    log.info("═"*60)

    hf_files = hf_list_pdfs()
    existing = {(e["date"], e["filename"].lower()): e
                for e in manifest.get("downloaded",[])}

    rebuilt     = []
    dates_done  = set(manifest.get("dates_done",[]))

    for hf_path in sorted(hf_files):
        parts = hf_path.split("/")
        if len(parts) != 3: continue
        _, ds, fname = parts
        key = (ds, fname.lower())
        if key in existing:
            e = dict(existing[key])
            e["hf_path"] = hf_path
            rebuilt.append(e)
        else:
            rebuilt.append({
                "date": ds,
                "date_display": datetime.strptime(ds,"%Y-%m-%d").strftime("%d/%m/%Y"),
                "filename": fname, "title": fname, "url": "",
                "pr_number": extract_pr(fname), "language": detect_lang(fname),
                "department": extract_dept(fname), "hf_path": hf_path,
                "download_status": "on_hf", "file_size_kb": 0,
            })
        dates_done.add(ds)

    for e in rebuilt:
        if e.get("pr_number") == "PR-???":
            fixed = extract_pr(e.get("title","") + " " + e.get("filename",""))
            if fixed != "PR-???": e["pr_number"] = fixed

    seen, deduped = set(), []
    for e in rebuilt:
        k = (e["date"], e["filename"].lower())
        if k not in seen: seen.add(k); deduped.append(e)
    deduped.sort(key=lambda e: (e["date"], e["filename"]))

    for ds in manifest.get("dates_done",[]): dates_done.add(ds)

    manifest.update({"downloaded": deduped, "dates_done": sorted(dates_done),
                     "dates_no_pdfs": manifest.get("dates_no_pdfs",[]),
                     "failed": []})
    save_manifest(manifest)
    hf_upload_manifest()
    log.info(f"  Rebuilt: {len(deduped)} entries from {len(hf_files)} HF files")
    return manifest

# ── PHASE 6: FINAL + REPORT ───────────────────────────────────────────────

def phase_final(manifest: dict, issues: list, run_start: datetime) -> dict:
    log.info("\n" + "═"*60)
    log.info("PHASE 6 — FINAL CHECK + REPORT")
    log.info("═"*60)

    downloaded = manifest.get("downloaded",[])
    dates_done = set(manifest.get("dates_done",[]))
    all_dates  = []
    dd = START_DATE
    while dd <= END_DATE:
        all_dates.append(dd); dd += timedelta(days=1)

    bad_pr   = [e for e in downloaded if e.get("pr_number")=="PR-???"]
    missing  = [dd.isoformat() for dd in all_dates
                if dd.isoformat() not in dates_done and dd < date.today()]

    total_issues = len(bad_pr) + len(missing)
    log.info(f"  ✅ missing_dates : {len(missing)}" if not missing
             else f"  ❌ missing_dates : {len(missing)}")
    log.info(f"  ✅ bad_pr_numbers: {len(bad_pr)}" if not bad_pr
             else f"  ❌ bad_pr_numbers: {len(bad_pr)}")
    log.info(f"  📄 Total entries : {len(downloaded)}")
    log.info(f"  🔍 Total issues  : {total_issues}")

    history = manifest.get("run_history",[])
    history.append({
        "run_at":       run_start.isoformat(timespec="seconds"),
        "new_pdfs":     manifest.get("_new_pdfs",0),
        "hf_uploaded":  manifest.get("_hf_uploaded",0),
        "issues_found": len(issues),
        "status":       "ok" if total_issues==0 else "warn",
    })
    manifest["run_history"] = history[-10:]
    for k in ["_new_pdfs","_hf_uploaded","_hf_deleted"]:
        manifest.pop(k, None)

    save_manifest(manifest)
    hf_upload_manifest()

    # Save report
    ts  = run_start.strftime("%Y%m%d_%H%M%S")
    rpt = REPORT_DIR / f"audit_{ts}.txt"
    rpt.write_text(
        f"DIPR TN Audit — {run_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*50}\n"
        f"Total PDFs   : {len(downloaded)}\n"
        f"Dates done   : {len(dates_done)}/{len(all_dates)}\n"
        f"Issues found : {len(issues)}\n"
        f"Final issues : {total_issues}\n\n"
        f"Run history:\n" +
        "\n".join(f"  {r['run_at']}  new={r['new_pdfs']}  status={r['status']}"
                  for r in manifest["run_history"][-10:]),
        encoding="utf-8"
    )
    log.info(f"  Report: {rpt.name}")
    return manifest

# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    run_start = datetime.now()
    manifest  = load_manifest()

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║   DIPR TN — Agentic Pipeline (HuggingFace storage)      ║")
    log.info(f"║   {START_DATE}  →  {END_DATE}                              ║")
    log.info("╚══════════════════════════════════════════════════════════╝\n")

    issues = []

    if not args.validate_only:
        log.info("🌐 Starting Chrome...")
        try:
            driver = make_driver()
            driver.get(BASE_URL)
            time.sleep(3)
            title = driver.title
            log.info(f"✅ Browser ready — page title: '{title}'")
        except Exception as e:
            log.error(f"❌ Chrome failed: {e}")
            log.error("Switching to validate-only mode")
            args.validate_only = True
            driver = None

        if driver:
            try:
                manifest = phase_download(driver, manifest)
            except KeyboardInterrupt:
                log.info("Interrupted")
            finally:
                try: driver.quit()
                except Exception: pass
                log.info("🌐 Browser closed")

    manifest = phase_upload_to_hf(manifest)
    manifest, issues = phase_validate(manifest)
    if issues:
        manifest = phase_autofix(manifest, issues)
    manifest = phase_rebuild(manifest)
    manifest = phase_final(manifest, issues, run_start)

    fc = manifest.get("final_check",{})
    n  = sum(v for v in {"missing_dates":0,"bad_pr_numbers":0}.values())
    log.info("\n╔══════════════════════════════════════════════════════════╗")
    log.info("║  ✅  Pipeline complete — check report for details        ║")
    log.info(f"║  📄  {len(manifest.get('downloaded',[]))} PDFs tracked in manifest                    ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    sys.exit(0)


if __name__ == "__main__":
    main()