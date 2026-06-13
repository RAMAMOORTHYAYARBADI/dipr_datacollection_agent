"""
DIPR TN - PDF Downloader
========================
Downloads all press release PDFs from dipr.tn.gov.in
Date range: May 10, 2026 → today

How the site works (confirmed from browser inspection):
  - URL: https://dipr.tn.gov.in/press-release1.html
  - Has a date input field (type="date")
  - Browser sends date as MM/DD/YYYY via the input
  - Page renders PDF links via Oracle APEX JavaScript
  - Each PDF is a direct downloadable link

Usage:
    pip install selenium webdriver-manager requests
    python download_pdfs.py
"""

import os
import re
import json
import time
import hashlib
import logging
import sys
from datetime import date, timedelta, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit here if needed
# ─────────────────────────────────────────────────────────────────────────────

START_DATE = date(2026, 5, 10)   # May 10, 2026
END_DATE   = date.today()

BASE_URL   = "https://dipr.tn.gov.in/press-release1.html"
PDF_DIR    = Path(__file__).parent / "pdfs"
MANIFEST   = PDF_DIR / "manifest.json"
LOG_FILE   = PDF_DIR / "download.log"

DELAY_BETWEEN_DATES = 2.0   # seconds to wait after changing date
DELAY_BETWEEN_PDFS  = 0.5   # seconds between PDF downloads
PDF_TIMEOUT         = 40    # seconds to wait for PDF download
MAX_PDF_RETRIES     = 3

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

PDF_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("dipr")

# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST — tracks what's done so re-runs skip completed dates
# ─────────────────────────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST.exists():
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {
        "start_date":    START_DATE.isoformat(),
        "end_date":      END_DATE.isoformat(),
        "dates_done":    [],
        "dates_no_pdfs": [],
        "downloaded":    [],
        "failed":        [],
    }

def save_manifest(m: dict):
    m["last_saved"] = datetime.now().isoformat(timespec="seconds")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
# PARSE PDF LINKS FROM PAGE HTML
# ─────────────────────────────────────────────────────────────────────────────

def parse_pdf_links(html: str, for_date: date) -> list[dict]:
    """Extract all PDF links from the rendered page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = a.get_text(" ", strip=True)

        # Must be a PDF link
        if not (".pdf" in href.lower() or ".PDF" in href):
            continue
        # Skip navigation/unrelated links
        if len(text) < 5 and not href.lower().endswith(".pdf"):
            continue

        # Build full URL
        if href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            full_url = "https://dipr.tn.gov.in" + href
        else:
            full_url = "https://dipr.tn.gov.in/" + href

        # Get clean filename
        raw_name = href.split("/")[-1].split("?")[0]
        filename = make_safe_filename(raw_name or text)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        # Deduplicate by filename
        if filename.lower() in seen:
            continue
        seen.add(filename.lower())

        results.append({
            "date":         for_date.isoformat(),               # 2026-05-12
            "date_display": for_date.strftime("%d/%m/%Y"),      # 12/05/2026
            "filename":     filename,
            "title":        text,
            "url":          full_url,
            "pr_number":    extract_pr_number(filename + " " + text),
            "language":     detect_language(filename + " " + text),
            "department":   extract_department(filename + " " + text),
        })

    return results


def make_safe_filename(name: str) -> str:
    """Remove characters that are invalid in filenames."""
    name = name.replace("%20", " ").replace("+", " ")
    # Windows-safe: remove  \ / : * ? " < > |
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Truncate if too long (Windows MAX_PATH)
    if len(name) > 180:
        ext  = name[-4:] if name[-4] == "." else ".pdf"
        name = name[:176] + ext
    return name


def extract_pr_number(text: str) -> str:
    # Match patterns like P.R.No.013 or PR No.005
    m = re.search(r"P\.?R\.?\s*No\.?\s*(\d+)", text, re.I)
    if m:
        return "PR-" + m.group(1).zfill(3)
    m = re.search(r"No\.?\s*(\d+)", text, re.I)
    if m:
        return "PR-" + m.group(1).zfill(3)
    return "PR-???"


def detect_language(text: str) -> str:
    t = text.lower()
    if "tamil" in t:
        return "Tamil"
    if "english" in t:
        return "English"
    if len(re.findall(r"[\u0B80-\u0BFF]", text)) > 3:
        return "Tamil"
    return "English"


def extract_department(text: str) -> str:
    # Remove PR number prefix and date suffix
    clean = re.sub(r"(?:DIPR|DIR|TNLA|SDAT|TNPSC)[-\s.]*P\.?R\.?\s*No\.?\d+", "", text, flags=re.I)
    clean = re.sub(r"[-\s]*Date[-\s.]*\d+.*", "", clean, flags=re.I)
    clean = re.sub(r"\.(pdf|PDF)$", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" -–_.")
    # Take the first meaningful segment
    parts = [p.strip() for p in re.split(r"\s*[-–]\s*", clean) if len(p.strip()) > 5]
    return parts[0][:80] if parts else "Government of Tamil Nadu"

# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD ONE PDF FILE
# ─────────────────────────────────────────────────────────────────────────────

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer":    "https://dipr.tn.gov.in/",
    "Accept":     "application/pdf,*/*",
}

def download_one_pdf(entry: dict, dest_dir: Path) -> dict:
    """Download a single PDF. Returns entry with download_status added."""
    dest = dest_dir / entry["filename"]

    # Skip if already downloaded and valid
    if dest.exists() and dest.stat().st_size > 1000:
        log.info(f"    ⏭️  Skip (exists): {entry['filename'][:65]}")
        return {**entry, "local_path": str(dest), "download_status": "exists",
                "file_size_kb": round(dest.stat().st_size / 1024, 1)}

    last_error = ""
    for attempt in range(1, MAX_PDF_RETRIES + 1):
        try:
            resp = requests.get(
                entry["url"],
                headers=DOWNLOAD_HEADERS,
                timeout=PDF_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()

            content = b"".join(resp.iter_content(chunk_size=65536))

            if len(content) < 1000:
                raise ValueError(f"File too small: {len(content)} bytes")

            # Verify PDF magic bytes
            if not content[:4] == b"%PDF":
                raise ValueError(f"Not a PDF (starts with {content[:8]})")

            dest.write_bytes(content)
            size_kb = len(content) / 1024
            log.info(f"    ✅ {entry['filename'][:65]:65s}  {size_kb:6.1f} KB")
            return {**entry, "local_path": str(dest), "download_status": "ok",
                    "file_size_kb": round(size_kb, 1)}

        except Exception as e:
            last_error = str(e)
            if attempt < MAX_PDF_RETRIES:
                log.warning(f"    ⚠️  Attempt {attempt}/{MAX_PDF_RETRIES} failed: {e} — retrying...")
                time.sleep(2 ** attempt)

    log.error(f"    ❌ FAILED after {MAX_PDF_RETRIES} attempts: {entry['filename'][:65]}")
    log.error(f"       Reason: {last_error}")
    return {**entry, "local_path": "", "download_status": "failed", "error": last_error}

# ─────────────────────────────────────────────────────────────────────────────
# SELENIUM — fetch PDF links for one date
# ─────────────────────────────────────────────────────────────────────────────

def make_driver():
    """Create headless Chrome. Called once, reused for all dates."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")          # new headless mode (Chrome 112+)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=opts)
    driver.implicitly_wait(5)
    return driver


def get_pdf_links_for_date(driver, target: date) -> list[dict]:
    """
    Navigate to the DIPR page, set the date, wait for PDF links to load.

    The site's date input (type="date") stores value as YYYY-MM-DD internally
    but the Oracle APEX page renders content based on whatever date is selected.

    Confirmed from screenshots:
      - Input value shown in browser:  05/12/2026  (MM/DD/YYYY display)
      - Internal HTML value:           2026-05-12  (YYYY-MM-DD)
      - Page heading shows:            Pr Date: 12/05/2026

    So: set input.value = "YYYY-MM-DD", then fire change events.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys

    # Date in YYYY-MM-DD (what HTML date inputs store internally)
    date_value  = target.strftime("%Y-%m-%d")     # 2026-05-12
    date_ddmmyy = target.strftime("%d/%m/%Y")     # 12/05/2026  (for verification)

    log.info(f"  📅 Setting date → {date_ddmmyy}")

    wait = WebDriverWait(driver, 15)

    # ── Load the page ──────────────────────────────────────────────────────
    current_url = driver.current_url
    if "press-release1" not in current_url:
        driver.get(BASE_URL)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            pass
        time.sleep(2)

    # ── Find the date input ────────────────────────────────────────────────
    date_input = None
    selectors = [
        "input[type='date']",
        "input[name*='date' i]",
        "input[id*='date' i]",
        "input[placeholder*='date' i]",
    ]
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el.is_displayed():
                date_input = el
                log.info(f"    Found date input: <input id='{el.get_attribute('id')}' name='{el.get_attribute('name')}'/>")
                break
        except Exception:
            pass

    if date_input is None:
        log.warning("    ⚠️  Date input not found on page!")
        return []

    # ── Set the date value and trigger page reload ─────────────────────────
    #
    # Strategy: use JavaScript to set the value then dispatch events.
    # Oracle APEX typically listens for 'change' event to trigger AJAX refresh.
    #
    driver.execute_script("""
        var el = arguments[0];
        var newVal = arguments[1];

        // Focus the element
        el.focus();

        // Set value using native input value setter (bypasses React/framework guards)
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeInputValueSetter.call(el, newVal);

        // Fire events in order: input → change (APEX listens to change)
        el.dispatchEvent(new Event('input',  { bubbles: true, cancelable: true }));
        el.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));

        // Blur to confirm
        el.blur();
    """, date_input, date_value)

    # Small pause then check if APEX fires an AJAX call
    time.sleep(0.5)

    # Also try clicking any submit/go button that might be present
    for xpath in [
        "//button[@type='submit']",
        "//input[@type='submit']",
        "//button[contains(translate(.,'go','GO'),'GO')]",
        "//a[contains(@class,'go')]",
    ]:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            if btn.is_displayed():
                btn.click()
                log.info(f"    Clicked submit button")
                break
        except Exception:
            pass

    # ── Wait for PDF links to appear ───────────────────────────────────────
    # We wait up to 8 seconds for at least one PDF link to appear
    pdf_loaded = False
    for _ in range(8):
        time.sleep(1)
        links = driver.find_elements(By.XPATH, "//a[contains(translate(@href,'PDF','pdf'),'.pdf')]")
        if links:
            pdf_loaded = True
            break

    if not pdf_loaded:
        # Check if page shows "no records" or similar
        page_text = driver.find_element(By.TAG_NAME, "body").text
        if any(w in page_text.lower() for w in ["no record", "no data", "no press"]):
            log.info(f"    Page says: no press releases for this date")
        else:
            log.info(f"    No PDF links found after 8s wait")
        return []

    # ── Verify page is showing the correct date ────────────────────────────
    page_source = driver.page_source
    # Look for the date heading: "Pr Date: 12/05/2026" or similar
    date_patterns = [
        target.strftime("%d/%m/%Y"),    # 12/05/2026
        target.strftime("%d.%m.%Y"),    # 12.05.2026
        target.strftime("%-d/%m/%Y") if sys.platform != "win32" else "",  # 12/05/2026
        f"{target.day}/{target.month}/{target.year}",
    ]
    date_confirmed = any(p and p in page_source for p in date_patterns)
    if not date_confirmed:
        log.warning(f"    ⚠️  Could not confirm date {date_ddmmyy} in page — parsing anyway")

    # ── Extract PDF links ──────────────────────────────────────────────────
    entries = parse_pdf_links(page_source, target)
    log.info(f"    Found {len(entries)} PDF link(s)")
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    manifest    = load_manifest()
    dates_done  = set(manifest.get("dates_done", []))
    dates_empty = set(manifest.get("dates_no_pdfs", []))
    downloaded  = list(manifest.get("downloaded", []))
    failed      = list(manifest.get("failed", []))

    # Count stats
    ok_total   = sum(1 for e in downloaded if e.get("download_status") == "ok")
    skip_total = sum(1 for e in downloaded if e.get("download_status") == "exists")

    # Build list of dates to process
    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        if d.weekday() != 6:   # skip Sundays — no govt releases
            all_dates.append(d)
        d += timedelta(days=1)

    pending = [d for d in all_dates if d.isoformat() not in dates_done]

    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║            DIPR TN — PDF Downloader                             ║")
    print(f"║  From: {START_DATE.strftime('%d %b %Y')}  →  To: {END_DATE.strftime('%d %b %Y')}                    ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  Working days total  : {len(all_dates):>4}                                   ║")
    print(f"║  Already completed   : {len(dates_done):>4}                                   ║")
    print(f"║  To process now      : {len(pending):>4}                                   ║")
    print(f"║  PDFs downloaded     : {ok_total:>4}                                   ║")
    print(f"║  PDFs skipped        : {skip_total:>4}  (already on disk)              ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print()

    if not pending:
        print("✅ All dates already processed! Nothing to do.")
        print(f"   Total PDFs on disk: {ok_total + skip_total}")
        print(f"   Folder: {PDF_DIR}")
        return

    # ── Start Selenium ─────────────────────────────────────────────────────
    print("🌐 Starting Chrome browser...")
    try:
        driver = make_driver()
    except Exception as e:
        print(f"❌ Could not start Chrome: {e}")
        print("   Make sure Chrome is installed and run:")
        print("   pip install selenium webdriver-manager")
        sys.exit(1)

    print("✅ Browser started.\n")

    # Load the page once
    driver.get(BASE_URL)
    time.sleep(3)

    try:
        for i, target in enumerate(pending, 1):
            date_str = target.isoformat()
            print(f"\n[{i:>3}/{len(pending)}]  {target.strftime('%A, %d %b %Y')}  ({date_str})")

            # ── Get PDF links for this date ──────────────────────────────
            try:
                entries = get_pdf_links_for_date(driver, target)
            except Exception as e:
                log.error(f"  Error getting links: {e}")
                entries = []

            if not entries:
                dates_done.add(date_str)
                dates_empty.add(date_str)
                manifest["dates_done"]    = sorted(dates_done)
                manifest["dates_no_pdfs"] = sorted(dates_empty)
                save_manifest(manifest)
                time.sleep(DELAY_BETWEEN_DATES)
                continue

            # ── Create folder for this date ──────────────────────────────
            date_dir = PDF_DIR / date_str
            date_dir.mkdir(exist_ok=True)

            # ── Download each PDF ────────────────────────────────────────
            for entry in entries:
                result = download_one_pdf(entry, date_dir)
                time.sleep(DELAY_BETWEEN_PDFS)

                if result["download_status"] in ("ok", "exists"):
                    # Avoid duplicate entries in manifest
                    existing_filenames = {e["filename"] for e in downloaded}
                    if result["filename"] not in existing_filenames:
                        downloaded.append(result)
                    if result["download_status"] == "ok":
                        ok_total += 1
                    else:
                        skip_total += 1
                else:
                    failed.append(result)

            # ── Mark date done and save ──────────────────────────────────
            dates_done.add(date_str)
            manifest["dates_done"]    = sorted(dates_done)
            manifest["dates_no_pdfs"] = sorted(dates_empty)
            manifest["downloaded"]    = downloaded
            manifest["failed"]        = failed
            save_manifest(manifest)

            print(f"  📊  Running total → ✅ {ok_total} downloaded  "
                  f"⏭️  {skip_total} skipped  ❌ {len(failed)} failed")

            time.sleep(DELAY_BETWEEN_DATES)

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user. Progress saved to manifest.json")
        print("   Run again to resume from where you stopped.")

    finally:
        driver.quit()
        print("\n🌐 Browser closed.")

    # ── FINAL SUMMARY ──────────────────────────────────────────────────────
    total_pdfs = ok_total + skip_total
    total_size = sum(
        f.stat().st_size
        for f in PDF_DIR.rglob("*.pdf")
        if f.is_file()
    )

    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║                    DOWNLOAD COMPLETE                            ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  ✅  Downloaded this run : {ok_total:<6}                              ║")
    print(f"║  ⏭️   Already existed   : {skip_total:<6}                              ║")
    print(f"║  📄  Total PDFs on disk : {total_pdfs:<6}                              ║")
    print(f"║  💾  Total size         : {total_size/(1024*1024):.1f} MB                              ║")
    print(f"║  ❌  Failed             : {len(failed):<6}                              ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  📁  Saved to: {str(PDF_DIR):<50}║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    if failed:
        print(f"\n  ❌ {len(failed)} PDFs failed to download:")
        for e in failed:
            print(f"     {e.get('date','')}  {e.get('filename','?')[:60]}")
            print(f"     Reason: {e.get('error','unknown')}")

    print()

    # Final manifest save
    manifest["summary"] = {
        "total_pdfs":     total_pdfs,
        "total_size_mb":  round(total_size / (1024 * 1024), 2),
        "ok":             ok_total,
        "skipped":        skip_total,
        "failed":         len(failed),
        "completed_at":   datetime.now().isoformat(timespec="seconds"),
    }
    save_manifest(manifest)


if __name__ == "__main__":
    main()
