# pip install selenium beautifulsoup4 requests webdriver-manager

import os
import re
import time
import threading
import queue
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

# ---------- CONFIG ----------
BASE_URL = "https://www.kawada-toys.com/en/brand/nanoblock/catalog/"
DOWNLOAD_DIR = "nanoblock_manuals"
STATE_FILE = os.path.join(DOWNLOAD_DIR, "processed_products.txt")
WAIT_SECONDS = 1
MAX_NO_CHANGE = 3

# ---------- SETUP ----------
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', '', name).strip()

def create_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # Choose ONE driver setup (uncomment/comment as needed)
    # Option A: auto-download (recommended)
    from webdriver_manager.chrome import ChromeDriverManager
    service = Service(ChromeDriverManager().install())

    # Option B: your specific chromedriver path
    # service = Service(r"C:\Users\108266\Desktop\chromedriver-win64\chromedriver.exe")

    return webdriver.Chrome(service=service, options=options)

# ---------- PERSISTENT STATE ----------
processed_urls = set()
processed_lock = threading.Lock()

def load_processed():
    global processed_urls
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            for line in f:
                url = line.strip()
                if url:
                    processed_urls.add(url)
        print(f"[State] Loaded {len(processed_urls)} previously processed products from {STATE_FILE}")
    else:
        print("[State] No existing state file. Starting fresh.")

def save_processed(url):
    with processed_lock:
        if url not in processed_urls:
            processed_urls.add(url)
            with open(STATE_FILE, 'a') as f:
                f.write(url + '\n')

# ---------- SCROLLER THREAD ----------
def scroller_worker(link_queue, scanner_done_event):
    driver = create_driver()
    driver.implicitly_wait(10)
    print("[Scroller] Loading catalog...")
    driver.get(BASE_URL)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))

    def get_current_links():
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/brand/nanoblock/catalog/']")
        unique = set()
        for a in links:
            href = a.get_attribute("href")
            if href and href != BASE_URL and "/brand/nanoblock/catalog/" in href:
                unique.add(href)
        return unique

    all_links_seen = set()
    no_change_count = 0

    while no_change_count < MAX_NO_CHANGE:
        # Scroll to bottom
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(WAIT_SECONDS)

        # Scroll last product link into view (triggers lazy‑load)
        all_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/brand/nanoblock/catalog/']")
        if all_links:
            last_link = all_links[-1]
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", last_link)
            time.sleep(WAIT_SECONDS)

        # Try "Load More" button
        try:
            load_more = driver.find_element(By.CSS_SELECTOR,
                                            "button.load-more, a.load-more, .load-more-button, #load-more")
            if load_more.is_displayed() and load_more.is_enabled():
                print("[Scroller] Clicking 'Load More'...")
                load_more.click()
                time.sleep(WAIT_SECONDS)
        except:
            pass

        current_links = get_current_links()
        newly_appeared = current_links - all_links_seen

        if newly_appeared:
            print(f"[Scroller] Found {len(newly_appeared)} new link(s) in total (some may already be processed).")
            all_links_seen.update(newly_appeared)
            no_change_count = 0   

            unprocessed_new = newly_appeared - processed_urls
            if unprocessed_new:
                print(f"[Scroller]   Adding {len(unprocessed_new)} unprocessed product(s) to queue.")
                for url in unprocessed_new:
                    link_queue.put(url)
            else:
                print("[Scroller]   All new links are already processed. Continuing to scroll.")
        else:
            no_change_count += 1
            print(f"[Scroller] No new links appeared, attempt {no_change_count}/{MAX_NO_CHANGE}")

        driver.execute_script("window.scrollBy(0, 500);")
        time.sleep(WAIT_SECONDS)

    # Scroller finished – signal that no more items will be added
    scanner_done_event.set()
    driver.quit()
    print("[Scroller] Finished.")

# ---------- DOWNLOADER THREAD ----------
def downloader_worker(link_queue, scanner_done_event, stop_event):
    driver = create_driver()
    driver.implicitly_wait(10)
    print("[Downloader] Started.")

    while not stop_event.is_set():
        try:
            product_url = link_queue.get(timeout=1)
        except queue.Empty:
            # If the scanner is done and the queue is empty, break
            if scanner_done_event.is_set() and link_queue.empty():
                break
            continue

        # No sentinel – we just process URLs
        print(f"\n[Downloader] Processing: {product_url}")
        try:
            driver.get(product_url)
            time.sleep(WAIT_SECONDS)

            page_soup = BeautifulSoup(driver.page_source, "html.parser")

            # Find all PDF URLs
            pdf_urls = []
            for tag in page_soup.find_all(["a", "iframe", "embed", "object"]):
                url = tag.get("href") or tag.get("src")
                if url and "wp-content/uploads" in url and url.lower().endswith(".pdf"):
                    if url.startswith("/"):
                        url = "https://www.kawada-toys.com" + url
                    if url not in pdf_urls:
                        pdf_urls.append(url)

            # Fallback: search text
            if not pdf_urls:
                for text in page_soup.stripped_strings:
                    if "wp-content/uploads" in text and ".pdf" in text:
                        start = text.find("http")
                        if start != -1:
                            end = text.find(".pdf", start) + 4
                            found_url = text[start:end]
                            if found_url.startswith("/"):
                                found_url = "https://www.kawada-toys.com" + found_url
                            if found_url not in pdf_urls:
                                pdf_urls.append(found_url)

            if not pdf_urls:
                print("[Downloader]   No PDF found. Marking as processed.")
                save_processed(product_url)
                continue

            # Get product name
            title_elem = page_soup.select_one(".is-hidden-touch.p-product-title")
            product_name = None
            if title_elem:
                product_name = title_elem.get_text(strip=True)
                product_name = sanitize_filename(product_name)
                print(f"[Downloader]   Product name: {product_name}")

            # Download each PDF
            for i, pdf_url in enumerate(pdf_urls, 1):
                print(f"[Downloader]   PDF {i}/{len(pdf_urls)}: {pdf_url}")

                original_filename = pdf_url.split("/")[-1]
                product_code = os.path.splitext(original_filename)[0]

                if product_name:
                    if len(pdf_urls) > 1:
                        new_filename = f"{product_code} - {product_name} - {i}.pdf"
                    else:
                        new_filename = f"{product_code} - {product_name}.pdf"
                else:
                    new_filename = original_filename

                filepath = os.path.join(DOWNLOAD_DIR, new_filename)

                if os.path.exists(filepath):
                    print(f"[Downloader]     File exists: {filepath}")
                    continue

                try:
                    response = requests.get(pdf_url, stream=True, timeout=30)
                    response.raise_for_status()
                    with open(filepath, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    print(f"[Downloader]     Downloaded: {filepath}")
                except Exception as e:
                    print(f"[Downloader]     ERROR downloading: {e}")

                time.sleep(WAIT_SECONDS)

            # All PDFs processed – mark product as done
            save_processed(product_url)
            time.sleep(WAIT_SECONDS)

        except Exception as e:
            print(f"[Downloader]   ERROR processing product page: {e}")

    driver.quit()
    print("[Downloader] Finished.")

# ---------- MAIN ----------
if __name__ == "__main__":
    load_processed()

    link_queue = queue.Queue()
    scanner_done_event = threading.Event()   
    stop_event = threading.Event()          

    scroller_thread = threading.Thread(target=scroller_worker, args=(link_queue, scanner_done_event))
    downloader_thread = threading.Thread(target=downloader_worker, args=(link_queue, scanner_done_event, stop_event))

    scroller_thread.start()
    downloader_thread.start()

    scroller_thread.join()             downloader_thread.join()

    print("\nAll done!")
    print(f"Total processed products (including previous runs): {len(processed_urls)}")
