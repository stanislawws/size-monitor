# monitor_sizes.py
from datetime import datetime
import os, re, time, json, itertools

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

# ================= Konfiguracja =================
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON_PATH", "service_account.json")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDS = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=SCOPES)
GS = gspread.authorize(CREDS)

DATE_FMT = "%Y-%m-%d"

# Tryb zliczania:
# True  -> rozmiar uznajemy za dostępny, jeśli w JAKIEJKOLWIEK kombinacji innych atrybutów (np. kolorów) da się go kupić (fallback)
# False -> liczymy dla bieżących ustawień atrybutów
UNION_MODE = True

# Limity dla grup INNYCH niż „Rozmiar” (dla wydajności w trybie fallback)
MAX_OPTIONS_PER_GROUP = 6
MAX_PAIRWISE_CHECKS   = 24

# Teksty przycisków/komunikatów (PL + EN)
ADD_TO_CART_TEXTS = ["Dodaj do koszyka", "Do koszyka", "Add to cart"]
NOTIFY_TEXTS      = ["Powiadom", "Powiadom o dostępności", "Powiadom mnie o dostępności", "Notify", "Availability"]
OOS_TEXTS         = ["Brak towaru", "Niedostępny", "Out of stock"]
COOKIE_TEXTS      = ["Akceptuj", "Zgadzam się", "Accept", "Rozumiem"]

# Timeouts/waity (ms)
GOTO_TIMEOUT    = 15000
WAIT_SELECTOR   = 4000
POST_CLICK_WAIT = 500

# ================= Arkusze =================
def get_or_create_worksheet(sh, title, headers):
    try:
        ws = sh.worksheet(title)
        if not ws.get_all_values():
            ws.append_row(headers, value_input_option="RAW")
        return ws
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=200, cols=max(10, len(headers)))
        ws.append_row(headers, value_input_option="RAW")
        return ws

# ================= Helpery UI =================
def _any_visible_enabled(page: Page, texts):
    for t in texts:
        for sel in [f"button:has-text('{t}')", f"a:has-text('{t}')", f"[role='button']:has-text('{t}')"]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    disabled = loc.get_attribute("disabled")
                    aria = (loc.get_attribute("aria-disabled") or "").lower()
                    if disabled is None and aria not in ("true", "1"):
                        return True
            except Exception:
                pass
    return False

def _any_visible(page: Page, texts):
    for t in texts:
        try:
            if page.locator(f":text('{t}')").first.is_visible():
                return True
        except Exception:
            pass
    return False

def accept_cookies(page: Page):
    for t in COOKIE_TEXTS:
        try:
            page.locator(f"button:has-text('{t}')").first.click(timeout=1200)
            page.wait_for_timeout(150)
            return
        except Exception:
            pass

def scroll_into_view_of_variants(page: Page):
    try:
        page.locator("text=Wybierz wariant produktu").first.scroll_into_view_if_needed(timeout=800)
    except Exception:
        try:
            page.locator("text=Rozmiar").first.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass

# ================= Warianty =================
class VariantGroup:
    def __init__(self, kind, root, label):
        self.kind = kind      # "radio" | "select" | "fallback"
        self.root = root      # Playwright Locator
        self.label = (label or "").strip()

    def __repr__(self):
        return f"<VariantGroup {self.kind} label='{self.label}'>"

def get_variant_groups(page: Page):
    groups = []

    # radio-variant-option (kafelki)
    radio_groups = page.locator("radio-variant-option")
    try:
        for i in range(radio_groups.count()):
            el = radio_groups.nth(i)
            label = (el.get_attribute("validation-name-label") or el.text_content() or "").strip()
            groups.append(VariantGroup("radio", el, label))
    except Exception:
        pass

    # select-variant-option (rozwijane)
    select_groups = page.locator("select-variant-option")
    try:
        for i in range(select_groups.count()):
            el = select_groups.nth(i)
            label = (el.get_attribute("validation-name-label") or el.text_content() or "").strip()
            groups.append(VariantGroup("select", el, label))
    except Exception:
        pass

    # fallback: nagłówek "Rozmiar" i okolice
    try:
        header = page.locator("text=Rozmiar").first
        if header and header.is_visible():
            container = header.locator("xpath=..")
            groups.append(VariantGroup("fallback", container, "Rozmiar"))
    except Exception:
        pass

    return groups

def group_is_size(g: VariantGroup):
    return bool(re.search(r"\brozmiar\b", g.label, re.I))

# ================== NOWE: statyczny odczyt rozmiarów (bez klikania) ==================
def read_sizes_static_from_radio(size_group: VariantGroup):
    """
    Dla <radio-variant-option> Shoper zwraca inputy:
      <input class="radio-box__input" id="option-...-NNN" data-user-value="S|M|..." [data-option-value-unavailable="1"]>
    Logika:
      - sizes_all: zawartość data-user-value dla wszystkich inputów
      - sizes_avail: te, które NIE mają data-option-value-unavailable="1" i nie są disabled
    """
    sizes_all = []
    sizes_avail = []
    try:
        inputs = size_group.root.locator("input.radio-box__input")
        cnt = inputs.count()
        for i in range(cnt):
            inp = inputs.nth(i)
            label = (inp.get_attribute("data-user-value") or "").strip()
            if not label:
                # awaryjnie: label z <label for="...">
                _id = inp.get_attribute("id")
                if _id:
                    lab = size_group.root.locator(f"label[for='{_id}']").first
                    try:
                        label = (lab.inner_text() or "").strip()
                    except Exception:
                        label = ""
            if not label:
                continue
            sizes_all.append(label)

            unavailable = (inp.get_attribute("data-option-value-unavailable") == "1")
            disabled    = (inp.get_attribute("disabled") is not None) or ((inp.get_attribute("aria-disabled") or "").lower() in ("true","1"))
            klass       = (inp.get_attribute("class") or "")
            looks_oos   = bool(re.search(r"unavailable|out[-_ ]?of[-_ ]?stock|sold|disabled", klass, re.I))

            if not (unavailable or disabled or looks_oos):
                sizes_avail.append(label)
    except Exception:
        pass
    return sizes_all, sizes_avail

def read_sizes_static_from_select(size_group: VariantGroup):
    """
    Dla <select-variant-option>:
      <select> <option value="...">S</option> lub <option disabled>XL</option>
    """
    sizes_all, sizes_avail = [], []
    try:
        sel = size_group.root.locator("select").first
        options = sel.locator("option")
        cnt = options.count()
        for i in range(cnt):
            opt = options.nth(i)
            txt = (opt.text_content() or "").strip()
            if not txt or re.search(r"wybierz", txt, re.I):
                continue
            sizes_all.append(txt)
            disabled = opt.get_attribute("disabled") is not None
            unavailable = (opt.get_attribute("data-option-value-unavailable") == "1")
            if not (disabled or unavailable):
                sizes_avail.append(txt)
    except Exception:
        pass
    return sizes_all, sizes_avail

# ================== Fallback (klikany) ==================
def list_options_for_group(g: VariantGroup, limit=None):
    cap = limit if (isinstance(limit, int) and limit > 0) else 10**6
    items = []
    try:
        if g.kind == "radio":
            labels = g.root.locator("label, .radio, .radio_box, .control, button")
            cnt = labels.count()
            for i in range(cnt):
                lab = labels.nth(i)
                txt = (lab.text_content() or "").strip()
                if not txt or re.search(r"wybierz", txt, re.I):
                    continue
                if len(txt) <= 18:
                    items.append(("radio", txt, lab))
                if len(items) >= cap:
                    break

        elif g.kind == "select":
            sel = g.root.locator("select").first
            options = sel.locator("option")
            cnt = options.count()
            for i in range(cnt):
                opt = options.nth(i)
                txt = (opt.text_content() or "").strip()
                val = opt.get_attribute("value") or ""
                if not txt or re.search(r"wybierz", txt, re.I):
                    continue
                items.append(("select", txt, (sel, val)))
                if len(items) >= cap:
                    break

        else:  # fallback
            labels = g.root.locator("xpath=following::*[self::label or self::button or contains(@class,'radio') or contains(@class,'tile')][position()<=48]")
            cnt = labels.count()
            for i in range(cnt):
                lab = labels.nth(i)
                txt = (lab.text_content() or "").strip()
                if not txt or re.search(r"wybierz", txt, re.I):
                    continue
                if len(txt) <= 18:
                    items.append(("radio", txt, lab))
                if len(items) >= cap:
                    break
    except Exception:
        pass
    return items

def select_option(item, page: Page):
    typ, txt, payload = item
    try:
        if typ == "radio":
            payload.click(timeout=1200)
        elif typ == "select":
            sel, val = payload
            sel.select_option(value=val)
        page.wait_for_timeout(POST_CLICK_WAIT)
        return True
    except Exception:
        return False

def is_current_variant_available(page: Page):
    if _any_visible(page, NOTIFY_TEXTS) or _any_visible(page, OOS_TEXTS):
        return False
    if _any_visible_enabled(page, ADD_TO_CART_TEXTS):
        return True
    return False

def check_size_availability_union(page: Page, size_group: VariantGroup, other_groups):
    size_options = list_options_for_group(size_group, limit=None)  # pełna lista rozmiarów
    all_size_labels = [o[1] for o in size_options]
    other_opts = [list_options_for_group(g, limit=MAX_OPTIONS_PER_GROUP) for g in other_groups]

    def select_defaults_for_others():
        for opts in other_opts:
            if opts:
                select_option(opts[0], page)

    available_sizes = []

    for s_item in size_options:
        size_label = s_item[1]

        # Domyślne wybory
        select_defaults_for_others()
        select_option(s_item, page)
        if is_current_variant_available(page):
            available_sizes.append(size_label)
            continue

        # Pojedyncze zmiany
        found = False
        for opts in other_opts:
            for item in opts:
                select_defaults_for_others()
                select_option(item, page)
                select_option(s_item, page)
                if is_current_variant_available(page):
                    available_sizes.append(size_label)
                    found = True
                    break
            if found:
                break
        if found:
            continue

        # Pary
        tried = 0
        if len(other_opts) >= 2:
            for (opts_a, opts_b) in itertools.combinations(other_opts, 2):
                for item_a in opts_a:
                    for item_b in opts_b:
                        tried += 1
                        if tried > MAX_PAIRWISE_CHECKS:
                            break
                        select_defaults_for_others()
                        select_option(item_a, page)
                        select_option(item_b, page)
                        select_option(s_item, page)
                        if is_current_variant_available(page):
                            available_sizes.append(size_label)
                            found = True
                            break
                    if found or tried > MAX_PAIRWISE_CHECKS:
                        break
                if found or tried > MAX_PAIRWISE_CHECKS:
                    break

    return all_size_labels, available_sizes

# ================= Skan pojedynczego produktu =================
def extract_product_id(page: Page, url: str):
    m = re.search(r"/pl/p/[^/]+/(\d+)", url)
    if m:
        return m.group(1)

    for sel, getter in [
        ("product-codes[product-id]", "el => el.getAttribute('product-id')"),
        ("[product-id]", "el => el.getAttribute('product-id')"),
        ("form[action*='cart'] input[name='id']", "el => el.value"),
        ("form[action*='basket'] input[name='id']", "el => el.value"),
        ("input[name='product_id']", "el => el.value"),
        ("[data-product-id]", "el => el.getAttribute('data-product-id')"),
    ]:
        try:
            pid = page.eval_on_selector(sel, getter)
            if pid and str(pid).strip().isdigit():
                return str(pid).strip()
        except Exception:
            pass

    # JSON-LD
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
    return ""

def extract_product_name(page: Page):
    for sel in ["h1", "h1.product__title", "header h1", "title"]:
        try:
            t = page.locator(sel).first.inner_text().strip()
            if t:
                return t
        except Exception:
            continue
    return ""

def probe_product(url, browser):
    page = browser.new_page()
    try:
        print(f"==> URL: {url}")
        page.goto(url, timeout=GOTO_TIMEOUT)
        accept_cookies(page)
        scroll_into_view_of_variants(page)
        try:
            page.wait_for_selector("text=Wybierz wariant produktu", timeout=WAIT_SELECTOR)
        except PWTimeout:
            pass

        product_name = extract_product_name(page)
        product_id   = extract_product_id(page, url)

        groups = get_variant_groups(page)
        size_groups = [g for g in groups if group_is_size(g)]
        other_groups = [g for g in groups if not group_is_size(g)]

        if not size_groups:
            print("   (brak grupy 'Rozmiar')")
            return dict(product_id=product_id, url=url, name=product_name, sizes_all=[], sizes_avail=[], size_count=0, status="no_size_group")

        size_group = size_groups[0]

        # --- PRIORYTET: statyczny odczyt bez klikania ---
        sizes_all, sizes_avail = [], []
        if size_group.kind == "radio":
            sizes_all, sizes_avail = read_sizes_static_from_radio(size_group)
        elif size_group.kind == "select":
            sizes_all, sizes_avail = read_sizes_static_from_select(size_group)

        # Jeśli statycznie udało się coś wykryć – używamy (najdokładniejsze)
        if sizes_all:
            print(f"   [STATIC] Rozmiary: all={sizes_all} avail={sizes_avail} count={len(sizes_avail)}")
            return dict(
                product_id=product_id,
                url=url,
                name=product_name,
                sizes_all=sizes_all,
                sizes_avail=sizes_avail,
                size_count=len(sizes_avail),
                status="ok"
            )

        # --- Fallback: klikanie (np. bardzo stare motywy) ---
        if UNION_MODE:
            sizes_all, sizes_avail = check_size_availability_union(page, size_group, other_groups)
        else:
            # prosty tryb (nie używany, ale zostawiam na przyszłość)
            opts = list_options_for_group(size_group, limit=None)
            sizes_all = [o[1] for o in opts]
            sizes_avail = []
            for o in opts:
                select_option(o, page)
                if is_current_variant_available(page):
                    sizes_avail.append(o[1])

        print(f"   [FALLBACK] Rozmiary: all={sizes_all} avail={sizes_avail} count={len(sizes_avail)}")
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
        print("   ERROR:", e.__class__.__name__, str(e))
        return dict(product_id="", url=url, name="", sizes_all=[], sizes_avail=[], size_count="", status=f"error: {e.__class__.__name__}")
    finally:
        try:
            page.close()
        except Exception:
            pass

# ================= Arkusze =================
def read_product_urls():
    sh = GS.open_by_key(SPREADSHEET_ID)
    ws = get_or_create_worksheet(sh, "Products", ["product_id", "url"])
    vals = ws.get_all_values()
    urls = []
    if not vals:
        return urls
    header = [c.strip().lower() for c in vals[0]]
    if len(header) >= 2 and header[0] in ("product_id", "id") and header[1].startswith("url"):
        for r in vals[1:]:
            if len(r) > 1 and r[1].startswith("http"):
                urls.append(r[1])
    else:
        urls = [r[0] for r in vals[1:] if r and len(r) > 0 and r[0].startswith("http")]
    return urls

def maybe_update_products_id(url, pid):
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
                if len(row) > 1 and row[1] == url and not row[0]:
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

# ================= Główna pętla =================
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
            time.sleep(0.5)  # łagodnie dla sklepu
        browser.close()

if __name__ == "__main__":
    main()
