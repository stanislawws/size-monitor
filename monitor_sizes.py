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

# Tryb zliczania dostępności:
# True  -> rozmiar jest "dostępny", jeśli w JAKIEJKOLWIEK kombinacji innych atrybutów (np. kolorach) da się go kupić (zalecane)
# False -> rozmiar liczony tylko dla domyślnych wyborów pozostałych atrybutów
UNION_MODE = True

# Limity przeszukiwania kombinacji innych atrybutów (dla wydajności)
MAX_OPTIONS_PER_GROUP = 4        # rozważamy do 4 pierwszych opcji z każdej grupy
MAX_PAIRWISE_CHECKS    = 16       # maksymalnie 4x4 przy testach par atrybutów

# Teksty przycisków/komunikatów (PL + fallback EN)
ADD_TO_CART_TEXTS = ["Dodaj do koszyka", "Do koszyka", "Add to cart"]
NOTIFY_TEXTS      = ["Powiadom", "Powiadom o dostępności", "Powiadom mnie o dostępności", "Notify", "Availability"]
OOS_TEXTS         = ["Brak towaru", "Niedostępny", "Out of stock"]

COOKIE_TEXTS      = ["Akceptuj", "Zgadzam się", "Accept", "Rozumiem"]

# Timeouts/waity (ms)
GOTO_TIMEOUT   = 15000
WAIT_SELECTOR  = 4000
POST_CLICK_WAIT= 350

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
    # sprawdzamy button/a/[role=button] z podanym tekstem
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
        # przewiń w okolice wariantów/cta
        page.locator("text=Wybierz wariant produktu").first.scroll_into_view_if_needed(timeout=800)
    except Exception:
        try:
            page.locator("text=Rozmiar").first.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass

# ================= Warianty (grupy i opcje) =================
class VariantGroup:
    def __init__(self, kind, root, label):
        self.kind = kind      # "radio" | "select" | "fallback"
        self.root = root
        self.label = (label or "").strip()

    def __repr__(self):
        return f"<VariantGroup {self.kind} label='{self.label}'>"

def get_variant_groups(page: Page):
    groups = []

    # radio-variant-option
    radio_groups = page.locator("radio-variant-option")
    try:
        for i in range(radio_groups.count()):
            el = radio_groups.nth(i)
            label = (el.get_attribute("validation-name-label") or el.text_content() or "").strip()
            groups.append(VariantGroup("radio", el, label))
    except Exception:
        pass

    # select-variant-option
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

def list_options_for_group(g: VariantGroup):
    """
    Zwraca listę "opcji" do wyboru w danej grupie.
    Dla radio/fallback: zwracamy listę lokatorów etykiet (label/button/..).
    Dla select: zwracamy listę krotek (value, text, locator_option).
    Filtrujemy śmieci ("Wybierz..."), ograniczamy do MAX_OPTIONS_PER_GROUP.
    """
    items = []
    try:
        if g.kind == "radio":
            labels = g.root.locator("label, .radio, .radio_box, .control, button")
            cnt = labels.count()
            for i in range(cnt):
                lab = labels.nth(i)
                txt = (lab.text_content() or "").strip()
                if not txt: 
                    continue
                # krótkie etykiety rozmiarów/kolorów; odrzucamy sekcje typu "Rozmiar"
                if re.search(r"wybierz", txt, re.I): 
                    continue
                if len(txt) <= 18:  # kolor bywa dłuższy niż rozmiar; dajemy margines
                    items.append(("radio", txt, lab))
                if len(items) >= MAX_OPTIONS_PER_GROUP:
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
                if len(items) >= MAX_OPTIONS_PER_GROUP:
                    break

        else:  # fallback
            labels = g.root.locator("xpath=following::*[self::label or self::button or contains(@class,'radio') or contains(@class,'tile')][position()<=24]")
            cnt = labels.count()
            for i in range(cnt):
                lab = labels.nth(i)
                txt = (lab.text_content() or "").strip()
                if not txt or re.search(r"wybierz", txt, re.I):
                    continue
                if len(txt) <= 18:
                    items.append(("radio", txt, lab))
                if len(items) >= MAX_OPTIONS_PER_GROUP:
                    break
    except Exception:
        pass
    return items

def select_option(item, page: Page):
    """
    item: krotka zwrócona przez list_options_for_group
    """
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
    # Negatywne sygnały (brak stanu)
    if _any_visible(page, NOTIFY_TEXTS) or _any_visible(page, OOS_TEXTS):
        return False
    # Pozytywne sygnały (można kupić)
    if _any_visible_enabled(page, ADD_TO_CART_TEXTS):
        return True
    return False

# ================= Logika liczenia dostępności rozmiarów =================
def check_size_availability_union(page: Page, size_group: VariantGroup, other_groups):
    """
    Sprawdza, czy DANY rozmiar jest dostępny w JAKIEJKOLWIEK kombinacji innych atrybutów.
    Strategia: domyślne -> pojedyncze zmiany -> pary.
    """
    size_options = list_options_for_group(size_group)
    other_opts = [list_options_for_group(g) for g in other_groups]

    available_sizes = []
    all_size_labels = [o[1] for o in size_options]

    # Przygotuj "domyślne" pierwsze opcje dla innych grup (o ile istnieją)
    def select_defaults_for_others():
        for opts in other_opts:
            if opts:
                select_option(opts[0], page)

    for s_item in size_options:
        size_label = s_item[1]
        # 1) domyślne wybory innych grup
        select_defaults_for_others()
        select_option(s_item, page)
        if is_current_variant_available(page):
            available_sizes.append(size_label)
            continue

        # 2) pojedyncze zmiany w innych grupach
        success = False
        for gi, opts in enumerate(other_opts):
            for oi, item in enumerate(opts[:MAX_OPTIONS_PER_GROUP]):
                select_defaults_for_others()
                # ustaw wybraną grupę gi na alternatywę item
                select_option(item, page)
                select_option(s_item, page)
                if is_current_variant_available(page):
                    available_sizes.append(size_label)
                    success = True
                    break
            if success: 
                break
        if success:
            continue

        # 3) pary grup (prosty, ale skuteczny produkt kart.)
        # ograniczamy liczbę prób do MAX_PAIRWISE_CHECKS
        tried = 0
        if len(other_opts) >= 2:
            for (opts_a, opts_b) in itertools.combinations(other_opts, 2):
                for item_a in opts_a[:MAX_OPTIONS_PER_GROUP]:
                    for item_b in opts_b[:MAX_OPTIONS_PER_GROUP]:
                        tried += 1
                        if tried > MAX_PAIRWISE_CHECKS:
                            break
                        select_defaults_for_others()
                        select_option(item_a, page)
                        select_option(item_b, page)
                        select_option(s_item, page)
                        if is_current_variant_available(page):
                            available_sizes.append(size_label)
                            success = True
                            break
                    if success or tried > MAX_PAIRWISE_CHECKS:
                        break
                if success or tried > MAX_PAIRWISE_CHECKS:
                    break

    return all_size_labels, available_sizes

def check_size_availability_simple(page: Page, size_group: VariantGroup, other_groups):
    """
    Prosty tryb: ustaw inne grupy na pierwsze opcje i licz rozmiary tylko dla tej jednej kombinacji.
    """
    size_options = list_options_for_group(size_group)
    all_size_labels = [o[1] for o in size_options]

    # ustaw domyślne (pierwsze) wartości innych grup
    for g in other_groups:
        opts = list_options_for_group(g)
        if opts:
            select_option(opts[0], page)

    available_sizes = []
    for s_item in size_options:
        select_option(s_item, page)
        if is_current_variant_available(page):
            available_sizes.append(s_item[1])

    return all_size_labels, available_sizes

# ================= Skan pojedynczego produktu =================
def extract_product_id(page: Page, url: str):
    m = re.search(r"/pl/p/[^/]+/(\d+)", url)
    if m:
        return m.group(1)

    try:
        pid = page.eval_on_selector("product-codes[product-id]", "el => el.getAttribute('product-id')")
        if pid and pid.strip().isdigit():
            return pid.strip()
    except Exception:
        pass
    try:
        pid = page.eval_on_selector("[product-id]", "el => el.getAttribute('product-id')")
        if pid and pid.strip().isdigit():
            return pid.strip()
    except Exception:
        pass
    for sel in ["form[action*='cart'] input[name='id']",
                "form[action*='basket'] input[name='id']",
                "input[name='product_id']",
                "[data-product-id]"]:
        try:
            if "data-" in sel:
                pid = page.eval_on_selector(sel, "el => el.getAttribute('data-product-id')")
            else:
                pid = page.eval_on_selector(sel, "el => el.value")
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

        size_group = size_groups[0]  # zwykle jedna
        if UNION_MODE:
            sizes_all, sizes_avail = check_size_availability_union(page, size_group, other_groups)
        else:
            sizes_all, sizes_avail = check_size_availability_simple(page, size_group, other_groups)

        print(f"   Rozmiary: all={sizes_all} avail={sizes_avail}")
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

# ================= Arkusze: odczyt/zapis =================
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
