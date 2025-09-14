# monitor_sizes.py
from datetime import datetime
import os
import re
import time
import json

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ======= Konfiguracja przez zmienne środowiskowe =======
# SPREADSHEET_ID            -> ID arkusza (z URL: między /d/ a /edit)
# SERVICE_ACCOUNT_JSON_PATH -> ścieżka do pliku klucza JSON (np. "service_account.json")
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDS = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=SCOPES)
GS = gspread.authorize(CREDS)

DATE_FMT = "%Y-%m-%d"

# Teksty spotykane na polskich sklepach / fallback EN
ADD_TO_CART_TEXTS = ["Dodaj do koszyka", "Do koszyka", "Add to cart"]
NOTIFY_TEXTS     = ["Powiadom", "Powiadom o dostępności", "Powiadom mnie o dostępności", "Notify", "Availability"]
COOKIE_TEXTS     = ["Akceptuj", "Zgadzam się", "Accept", "Rozumiem"]

# =======================================================
# Pomocnicze: tworzenie zakładek, jeżeli ich nie ma
# =======================================================
def get_or_create_worksheet(sh, title, headers):
    """
    Zwraca worksheet o nazwie 'title'.
    Jeśli nie istnieje – tworzy go i wpisuje nagłówki.
    Jeśli istnieje, ale pusty – dopisze nagłówki.
    """
    try:
        ws = sh.worksheet(title)
        # Jeżeli worksheet jest pusty – dołóż nagłówki
        if not ws.get_all_values():
            ws.append_row(headers, value_input_option="RAW")
        return ws
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=100, cols=max(10, len(headers)))
        ws.append_row(headers, value_input_option="RAW")
        return ws

# =======================================================
# UI helpers
# =======================================================
def _any_visible_enabled(page, texts):
    for t in texts:
        loc = page.locator(f"button:has-text('{t}')")
        try:
            if loc.first.is_visible():
                if loc.first.get_attribute("disabled") is None and loc.first.get_attribute("aria-disabled") not in ("true", "1"):
                    return True
        except Exception:
            pass
    return False

def _any_visible(page, texts):
    for t in texts:
        try:
            if page.locator(f"button:has-text('{t}')").first.is_visible():
                return True
        except Exception:
            pass
    return False

def accept_cookies(page):
    for t in COOKIE_TEXTS:
        try:
            page.locator(f"button:has-text('{t}')").first.click(timeout=1500)
            time.sleep(0.2)
            return
        except Exception:
            pass

# =======================================================
# Ekstrakcja product_id (kilka heurystyk)
# =======================================================
def extract_product_id(page, url):
    # 1) z adresu /pl/p/<slug>/<ID>
    m = re.search(r"/pl/p/[^/]+/(\d+)", url)
    if m:
        return m.group(1)

    # 2) product-codes[product-id]
    try:
        pid = page.eval_on_selector("product-codes[product-id]", "el => el.getAttribute('product-id')")
        if pid and pid.strip().isdigit():
            return pid.strip()
    except Exception:
        pass

    # 3) dowolny element z atrybutem product-id
    try:
        pid = page.eval_on_selector("[product-id]", "el => el.getAttribute('product-id')")
        if pid and pid.strip().isdigit():
            return pid.strip()
    except Exception:
        pass

    # 4) hidden input w formularzu koszyka
    for sel in ["form[action*='cart'] input[name='id']",
                "form[action*='basket'] input[name='id']",
                "input[name='product_id']"]:
        try:
            pid = page.eval_on_selector(sel, "el => el.value")
            if pid and pid.strip().isdigit():
                return pid.strip()
        except Exception:
            pass

    # 5) data-product-id na przyciskach
    try:
        pid = page.eval_on_selector("[data-product-id]", "el => el.getAttribute('data-product-id')")
        if pid and pid.strip().isdigit():
            return pid.strip()
    except Exception:
        pass

    # 6) JSON-LD – pola productID/@id/sku/mpn jeżeli czysto numeryczne
    try:
        scripts = page.locator("script[type='application/ld+json']")
        count = min(scripts.count(), 6)
        for i in range(count):
            try:
                jtxt = scripts.nth(i).inner_text()
                data = json.loads(jtxt)
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for obj in items:
                if isinstance(obj, dict):
                    for key in ("productID", "@id", "sku", "mpn"):
                        val = obj.get(key)
                        if isinstance(val, str) and val.isdigit():
                            return val
    except Exception:
        pass

    return ""  # nie znaleziono

# =======================================================
# Wyszukanie grupy wariantów "Rozmiar"
# =======================================================
def find_size_group(page):
    # web-component z "radio" wariantami
    radio_groups = page.locator("radio-variant-option")
    try:
        for i in range(radio_groups.count()):
            el = radio_groups.nth(i)
            label = (el.get_attribute("validation-name-label") or el.text_content() or "").strip()
            if re.search(r"rozmiar", label, re.I):
                return "radio", el
    except Exception:
        pass

    # web-component z selectem
    select_groups = page.locator("select-variant-option")
    try:
        for i in range(select_groups.count()):
            el = select_groups.nth(i)
            label = (el.get_attribute("validation-name-label") or el.text_content() or "").strip()
            if re.search(r"rozmiar", label, re.I):
                return "select", el
    except Exception:
        pass

    # fallback – nagłówek "Rozmiar", a potem kafelki/przyciski w dół DOM
    try:
        header = page.locator("text=Rozmiar").first
        if header and header.is_visible():
            container = header.locator("xpath=..")
            return "fallback", container
    except Exception:
        pass

    return None, None

def extract_product_name(page):
    for sel in ["h1", "h1.product__title", "header h1", "title"]:
        try:
            t = page.locator(sel).first.inner_text().strip()
            if t:
                return t
        except Exception:
            continue
    return ""

# =======================================================
# Enumeracja rozmiarów + sprawdzenie dostępności
# =======================================================
def enumerate_sizes_and_availability(page, group_type, root):
    sizes_all, sizes_avail = [], []

    def mark_availability(size_label):
        add_enabled = _any_visible_enabled(page, ADD_TO_CART_TEXTS)
        notify_visible = _any_visible(page, NOTIFY_TEXTS)
        if add_enabled and not notify_visible:
            sizes_avail.append(size_label)

    if group_type == "radio":
        labels = root.locator("label, .radio, .radio_box, .control")
        count = labels.count()
        for i in range(count):
            lab = labels.nth(i)
            txt = (lab.text_content() or "").strip()
            if not txt:
                continue
            if len(txt) <= 6 and re.search(r"[XSML\d]", txt, re.I):
                sizes_all.append(txt)
                try:
                    lab.click(timeout=2000)
                    page.wait_for_timeout(300)
                    mark_availability(txt)
                except Exception:
                    pass

    elif group_type == "select":
        select = root.locator("select").first
        options = select.locator("option")
        for i in range(options.count()):
            opt = options.nth(i)
            txt = (opt.text_content() or "").strip()
            if not txt or re.search(r"wybierz", txt, re.I):
                continue
            sizes_all.append(txt)
            try:
                select.select_option(value=opt.get_attribute("value"))
                page.wait_for_timeout(300)
                mark_availability(txt)
            except Exception:
                pass

    else:  # fallback
        labels = root.locator("xpath=following::*[self::label or self::button or contains(@class,'radio') or contains(@class,'tile')][position()<=20]")
        count = labels.count()
        for i in range(count):
            lab = labels.nth(i)
            txt = (lab.text_content() or "").strip()
            if not txt:
                continue
            if len(txt) <= 6 and re.search(r"[XSML\d]", txt, re.I):
                sizes_all.append(txt)
                try:
                    lab.click(timeout=2000)
                    page.wait_for_timeout(300)
                    mark_availability(txt)
                except Exception:
                    pass

    # deduplikacja z zachowaniem kolejności
    seen = set()
    sizes_all = [x for x in sizes_all if not (x in seen or seen.add(x))]
    seen.clear()
    sizes_avail = [x for x in sizes_avail if not (x in seen or seen.add(x))]

    return sizes_all, sizes_avail

# =======================================================
# Skan pojedynczego produktu
# =======================================================
def probe_product(url, browser):
    page = browser.new_page()
    try:
        page.goto(url, timeout=60000)
        accept_cookies(page)
        try:
            page.wait_for_timeout(500)  # inicjalizacja komponentów
            page.wait_for_selector("text=Wybierz wariant produktu", timeout=5000)
        except PWTimeout:
            pass

        product_name = extract_product_name(page)
        product_id = extract_product_id(page, url)

        gtype, root = find_size_group(page)
        if not gtype:
            return dict(product_id=product_id, url=url, name=product_name, sizes_all=[], sizes_avail=[], size_count=0, status="no_size_group")

        sizes_all, sizes_avail = enumerate_sizes_and_availability(page, gtype, root)
        return dict(
            product_id=product_id,
            url=url,
            name=product_name,
            sizes_all=sizes_all,
            sizes_avail=sizes_avail,
            size_count=len(sizes_avail),
            status="ok"
        )
    except Exception as e:
        return dict(product_id="", url=url, name="", sizes_all=[], sizes_avail=[], size_count="", status=f"error: {e.__class__.__name__}")
    finally:
        try:
            page.close()
        except Exception:
            pass

# =======================================================
# Operacje na Arkuszu
# =======================================================
def read_product_urls():
    sh = GS.open_by_key(SPREADSHEET_ID)
    ws = get_or_create_worksheet(sh, "Products", ["product_id", "url"])
    vals = ws.get_all_values()
    urls = []
    if not vals:
        return urls
    header = [c.strip().lower() for c in vals[0]]
    # Obsługa dwóch układów: [product_id, url] lub [url]
    if len(header) >= 2 and header[0] in ("product_id", "id") and header[1].startswith("url"):
        for r in vals[1:]:
            if len(r) > 1 and r[1].startswith("http"):
                urls.append(r[1])
    else:
        urls = [r[0] for r in vals[1:] if r and len(r) > 0 and r[0].startswith("http")]
    return urls

def maybe_update_products_id(url, pid):
    """Jeśli 'Products' ma układ [product_id, url] i A jest puste — uzupełnij ID."""
    if not pid:
        return
    try:
        sh = GS.open_by_key(SPREADSHEET_ID)
        ws = get_or_create_worksheet(sh, "Products", ["product_id", "url"])
        vals = ws.get_all_values()
        if not vals:
            return
        header = [c.strip().lower() for c in vals[0]]
        if len(header) >= 2 and header[0] in ("product_id", "id") and header[1].startswith("url"):
            for i, row in enumerate(vals[1:], start=2):
                if len(row) > 1 and row[1] == url:
                    if not row[0]:
                        ws.update_acell(f"A{i}", pid)
                    return
    except Exception:
        pass

def append_daily_row(result):
    sh = GS.open_by_key(SPREADSHEET_ID)
    ws = get_or_create_worksheet(
        sh, "Daily",
        ["product_id", "date", "url", "product_name", "size_count", "sizes_avail", "sizes_all", "status"]
    )
    today = datetime.utcnow().strftime(DATE_FMT)
    ws.append_row([
        result.get("product_id", ""),
        today,
        result["url"],
        result["name"],
        result["size_count"],
        ", ".join(result["sizes_avail"]),
        ", ".join(result["sizes_all"]),
        result["status"]
    ], value_input_option="RAW")

# =======================================================
# Główna pętla
# =======================================================
def main():
    urls = read_product_urls()
    if not urls:
        print("Brak URL-i w zakładce 'Products'. Dodaj co najmniej jeden adres produktu i uruchom ponownie.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        for url in urls:
            res = probe_product(url, browser)
            append_daily_row(res)
            maybe_update_products_id(url, res.get("product_id", ""))
            time.sleep(0.7)  # łagodne tempo, szacunek dla sklepu
        browser.close()

if __name__ == "__main__":
    main()
