"""
Link visitor worker.
Reads links directly from the formatted_urls_partN.txt file (comma-separated).
Visits each link with a 1-minute page-load timeout. On success, moves on
immediately. Loops forever.
Spawned as a subprocess by the Streamlit wrapper (visiterkeepalive.py).
"""

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time
import subprocess
import sys
import importlib
import datetime
import os
import platform
import shutil
import signal

# ============ CONFIGURATION ============
FORMATTED_URLS_FILE = "formatted_urls_part9.txt"     # comma-separated URLs
PAGE_LOAD_TIMEOUT_SECONDS = 60             # per-link timeout
PAUSE_BETWEEN_LINKS_SECONDS = 300          # small breather between links
RESTART_PAUSE_SECONDS = 300                # pause between full passes
MAX_CONSECUTIVE_ERRORS = 5                 # restart browser after N errors
# =======================================

running = True


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


def signal_handler(sig, frame):
    global running
    log("🛑 Shutdown signal received. Will exit after current step.")
    running = False


try:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
except (ValueError, OSError):
    pass


# ---------- Package installation ----------
def pip_install(package):
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package, "--quiet"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"⚠️ pip install {package} failed: {e}")
        return False


def ensure_python_deps():
    for pkg in ["selenium"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            log(f"📦 Installing {pkg}...")
            pip_install(pkg)


ensure_python_deps()


# ---------- Links loading ----------
def load_links():
    """
    Read URLs directly from FORMATTED_URLS_FILE.
    Format: comma-separated, but also tolerates newlines / whitespace.
    Returns a deduplicated list preserving order.
    """
    if not os.path.exists(FORMATTED_URLS_FILE):
        log(f"⚠️ {FORMATTED_URLS_FILE} not found")
        return []

    try:
        with open(FORMATTED_URLS_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        log(f"⚠️ Could not read {FORMATTED_URLS_FILE}: {e}")
        return []

    # Split on commas, newlines, or whitespace
    parts = []
    for line in raw.replace(",", "\n").splitlines():
        url = line.strip()
        if url:
            parts.append(url)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for u in parts:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    log(f"📄 Loaded {len(unique)} links from {FORMATTED_URLS_FILE}")
    return unique


# ---------- Chrome detection ----------
def find_chrome_binary():
    system = platform.system()
    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expanduser(
                r"~\AppData\Local\Google\Chrome\Application\chrome.exe"
            ),
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/snap/bin/chromium",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def ensure_chrome_binary():
    path = find_chrome_binary()
    if path:
        log(f"✅ Chrome found: {path}")
        return path
    log("⚠️ Chrome not found. Install it (see notes).")
    return None


# ---------- Driver ----------
def build_chrome_options(chrome_path):
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-logging")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-sync")
    opts.add_argument("--no-first-run")
    opts.add_argument("--blink-settings=imagesEnabled=false")  # faster loads
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if chrome_path:
        opts.binary_location = chrome_path
    return opts


def create_driver(chrome_path):
    try:
        opts = build_chrome_options(chrome_path)
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
            "headers": {
                "ngrok-skip-browser-warning": "1",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36 MyApp/1.0"
                ),
            }
        })
        return driver
    except Exception as e:
        log(f"❌ Error initializing browser: {e}")
        return None


# ---------- Visit one link ----------
def visit_link(driver, url):
    """
    Visit a single URL. Returns (ok, info).
    Uses 'eager' pageLoadStrategy so we don't wait for every asset.
    """
    try:
        t0 = time.time()

        # Eager strategy: DOM ready, don't wait for full load
        try:
            driver.execute_cdp_cmd("Page.enable", {})
            driver.execute_cdp_cmd(
                "Page.setWebLifecycleState", {"state": "active"}
            )
        except Exception:
            pass

        driver.get(url)

        elapsed = time.time() - t0
        title = ""
        try:
            title = driver.title or ""
        except Exception:
            pass

        return True, {
            "url": url,
            "title": title[:120],
            "seconds": round(elapsed, 2),
        }
    except Exception as e:
        return False, {"url": url, "error": str(e)[:200]}


# ---------- Main loop ----------
def main():
    log("=" * 60)
    log("🚀 Link Visitor worker started")
    log(f"   Links source        : {FORMATTED_URLS_FILE}")
    log(f"   Page load timeout   : {PAGE_LOAD_TIMEOUT_SECONDS}s per link")
    log(f"   PID                 : {os.getpid()}")
    log("=" * 60)

    chrome_path = ensure_chrome_binary()

    links = load_links()
    if not links:
        log(f"❌ No links found in {FORMATTED_URLS_FILE}. "
            "Worker cannot proceed.")
        return
    log(f"🎯 Total links to visit per pass: {len(links)}")

    driver = create_driver(chrome_path)
    if driver is None:
        log("❌ Could not create browser. Exiting.")
        return

    consecutive_errors = 0
    total_visits = 0
    pass_num = 0

    try:
        while running:
            pass_num += 1
            log(f"\n===== PASS #{pass_num} ({len(links)} links) =====")

            for i, url in enumerate(links, 1):
                if not running:
                    break

                # Restart browser if it's been erroring
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log(f"♻️ Too many errors ({consecutive_errors}), "
                        "restarting browser...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    time.sleep(3)
                    driver = create_driver(chrome_path)
                    if driver is None:
                        log("❌ Browser restart failed. Sleeping 60s...")
                        time.sleep(60)
                        driver = create_driver(chrome_path)
                        if driver is None:
                            break
                    consecutive_errors = 0

                log(f"[{i}/{len(links)}] → {url}")
                ok, info = visit_link(driver, url)

                if ok:
                    total_visits += 1
                    log(f"   ✅ {info['seconds']}s | {info['title']}")
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    log(f"   ❌ {info.get('error', 'unknown')}")

                # Small pause so we don't hammer the CPU
                if PAUSE_BETWEEN_LINKS_SECONDS and running:
                    time.sleep(PAUSE_BETWEEN_LINKS_SECONDS)

            if not running:
                break

            log(f"😴 Pass #{pass_num} complete ({total_visits} successful "
                f"visits so far). Sleeping {RESTART_PAUSE_SECONDS}s "
                "before next pass...")

            end_wait = time.time() + RESTART_PAUSE_SECONDS
            while time.time() < end_wait and running:
                time.sleep(2)

            # Reload the txt file each pass in case it changed
            new_links = load_links()
            if new_links:
                links = new_links

    finally:
        log("🔄 Closing browser...")
        try:
            driver.quit()
        except Exception:
            pass

    log(f"👋 Worker stopped. Total successful visits: {total_visits}")


if __name__ == "__main__":
    main()