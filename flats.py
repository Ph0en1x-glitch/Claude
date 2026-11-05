#!/usr/bin/env python3
"""Мониторинг аренды квартир в Праге 7/8/9/18: Sreality + Bezrealitky -> Telegram.

Запускается раз в час (GitHub Actions). Помнит уже обработанные объявления
в state.json, поэтому дублей не будет.
"""
import html as htmllib
import json
import os
import random
import re
import time
import unicodedata
from pathlib import Path

import requests

# ====================== НАСТРОЙКИ ======================
DISTRICTS = [7, 8, 9, 18]       # районы Праги (Praha 7, 8, 9, 18)
PRICE_FURNISHED = 35000         # лимит, если мебель есть / не указана
PRICE_UNFURNISHED = 30000       # лимит, если квартира пустая
MIN_EQUIP_MENTIONED = 0         # сколько из 4 (ванна/духовка/посудомойка/стиралка)
                                # должно быть упомянуто в тексте; 0 = не фильтровать
SKIP_IF_NO_PETS = False         # True = не присылать, если явно написано "без животных"
MAX_MESSAGES_PER_RUN = 20       # защита от потока сообщений
MAX_DETAILS_PER_RUN = 60        # сколько страниц-деталей открывать за один запуск
# Быстрый режим (FAST=1): смотрим только самые свежие объявления, запросов мало,
# можно запускать каждые несколько минут. Полный режим: все страницы.
FAST = os.environ.get("FAST") == "1"
SR_MAX_PAGES = 2 if FAST else 8   # страниц Sreality на район (по ~22 объявления)
MAX_PAGES = 1 if FAST else 5      # страниц Bezrealitky (по 60 объявлений)
STATE_FILE = Path("state.json")
SEEN_TTL_DAYS = 120

# Районы (кадастровые части), относящиеся к Praha 7/8/9/18. Нужны ТОЛЬКО для
# Bezrealitky: там в адресе часто нет номера района ("Praha - Libeň").
# Список приблизительный, правь под себя.
BZ_AREAS = {
    7: ["Holešovice", "Bubeneč", "Troja", "Letná", "Bubny"],
    8: ["Libeň", "Karlín", "Kobylisy", "Bohnice", "Čimice", "Dolní Chabry",
        "Ďáblice", "Březiněves"],
    9: ["Vysočany", "Prosek", "Hloubětín", "Střížkov", "Hrdlořezy",
        "Černý Most", "Kyje", "Hostavice"],
    18: ["Letňany", "Čakovice"],
}

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Диспозиции: 1+1, 2+kk, 2+1, 3+kk, 3+1, 4+kk, 4+1, 5+kk, 5+1
ALLOWED_DISP = {"1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "4+1", "5+kk", "5+1"}
BEZ_DISPS = ["DISP_1_1", "DISP_2_KK", "DISP_2_1", "DISP_3_KK", "DISP_3_1",
             "DISP_4_KK", "DISP_4_1", "DISP_5_KK", "DISP_5_1"]

# Ключевые слова (текст приводится к нижнему регистру и без диакритики)
EQUIP = {
    "ванна": r"\bvan(a|u|y|e|ou|ova|ove|ovou)\b|vanou|bathtub",
    "духовка": r"troub|sporak|\boven\b",
    "посудомойка": r"myck|dishwasher",
    "стиралка": r"prack|washing machine",
}
BALCONY = r"balkon|terasa|terase|terasu|terasy|lodzie|lodzii|loggia|terrace|balcony"
NOT_FURNISHED = r"nezarizen|nevybaven|prazdn|bez nabytku|bez vybaven"
FURNISHED = r"zarizen|nabyt|castecne"
NO_PETS = (r"bez zvirat|bez domacich zvirat|(zvirat|mazlic)\w* (nejsou|nepovol|nelze)|"
           r"nepovol\w* (zvirat|mazlic)|no pets|pets? not allowed")
PETS_AGREEMENT = (r"(zvirat|mazlic)\w* (po dohode|dle dohody|po domluve|dle domluvy)|"
                  r"(po dohode|po domluve).{0,25}(zvirat|mazlic)")
PETS_OK = (r"(zvirat|mazlic)\w* (povolen|vitan|mozn|ok\b)|pet.?friendly|pets? allowed|"
           r"se zviraty|s mazlicky|s domacimi zviraty")

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "cs,en;q=0.8",
})


# ====================== УТИЛИТЫ ======================
def norm(s):
    s = unicodedata.normalize("NFKD", str(s).lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def getf(d, name):
    """Поле словаря; учитывает ключи Apollo вида 'address({"locale":"CS"})'."""
    if name in d:
        return d[name]
    for k, v in d.items():
        if k.startswith(name + "("):
            return v
    return None


def next_data(page_html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
    return json.loads(m.group(1)) if m else None


def furnished_token(v):
    if isinstance(v, bool):
        return "zarizeny" if v else "nezarizeny"
    t = norm(v)
    if t in ("no", "ne", "false", "nezarizeny", "unfurnished"):
        return "nezarizeny"
    if t in ("yes", "ano", "true", "zarizeny", "furnished"):
        return "zarizeny"
    if "part" in t or "castecne" in t:
        return "castecne zarizeny"
    return t


SKIP_KEYS = ("similar", "recommend", "related", "nearby", "otheradvert")


def strings_of(d, depth=0):
    """Все строки словаря (включая вложенные) + имена булевых полей со значением True."""
    out = []
    for k, v in d.items():
        kl = str(k).lower()
        if any(s in kl for s in SKIP_KEYS):
            continue
        if kl == "furnished":
            out.append(furnished_token(v))
        elif isinstance(v, str):
            out.append(v)
        elif v is True:
            out.append(str(k))
        elif isinstance(v, dict) and depth < 4:
            out += strings_of(v, depth + 1)
        elif isinstance(v, list) and depth < 4:
            for x in v:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    out += strings_of(x, depth + 1)
    return out


def money(v):
    return f"{int(v):,}".replace(",", " ")


def fetch(url, **kw):
    r = S.get(url, timeout=30, **kw)
    print(f"GET {r.status_code} {r.url[:170]}")
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]!r}")
    return r


def page_text(page_html):
    t = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", page_html)
    return re.sub(r"<[^>]+>", " ", t)


def first_working(name, strategies):
    errs = []
    for fn in strategies:
        try:
            items = fn()
            if items:
                print(f"{name}: стратегия {fn.__name__} дала {len(items)} объявлений")
                return items
            errs.append(f"{fn.__name__}: 0 объявлений")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{fn.__name__}: {e}")
    raise RuntimeError(" | ".join(errs)[:900])


# ====================== SREALITY ======================
# Публичный JSON API Sreality закрыт (404/401), но страница поиска содержит
# все данные в <script id="__NEXT_DATA__">. Читаем их оттуда.
SR_DISPS = ["1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "4+1", "5+kk", "5+1"]


def find_detail_url(d):
    for v in walk(d):
        for x in v.values():
            if isinstance(x, str) and ("/detail/" in x) and (x.startswith("/") or "sreality.cz" in x):
                return "https://www.sreality.cz" + x if x.startswith("/") else x
    return None


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")


def sr_locality(d):
    loc = d.get("locality")
    src_d = loc if isinstance(loc, dict) else {}
    parts = []
    for key in ("street", "cityPart", "district", "city"):
        v = src_d.get(key) or d.get(key)
        if isinstance(v, str) and v and v not in parts:
            parts.append(v)
    if parts:
        return ", ".join(parts)
    if isinstance(loc, str):
        return loc
    if isinstance(loc, dict):
        return ", ".join(str(v) for v in loc.values() if isinstance(v, str))
    return ""


def sr_candidate(d):
    hid = d.get("hash_id", d.get("hashId", d.get("id")))
    if hid is None or not any(k in d for k in ("priceCzk", "price_czk", "price")):
        return None
    name = d.get("name") or d.get("title")
    if not isinstance(name, str):
        return None
    if "pronajem" not in norm(name):
        return None
    m = re.search(r"\d\+(?:kk|kt|\d)", name, re.I)
    if not m or m.group(0).lower().replace("kt", "kk") not in ALLOWED_DISP:
        return None
    price = d.get("priceCzk") or d.get("price_czk") or d.get("price")
    if isinstance(price, dict):
        price = price.get("value_raw") or price.get("value")
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    loc = sr_locality(d)
    disp = m.group(0).lower().replace("kt", "kk")
    cp = (d.get("locality") or {}).get("cityPart") if isinstance(d.get("locality"), dict) else None
    cp = cp or d.get("cityPart")
    area = "praha-" + slug(cp) if isinstance(cp, str) and cp else "praha"
    url = find_detail_url(d) or f"https://www.sreality.cz/detail/pronajem/byt/{disp}/{area}/{hid}"
    return {
        "id": f"sr:{hid}", "source": "Sreality", "ref": str(hid),
        "title": name, "locality": loc, "price": int(price), "charges": None,
        "text": f"{name} {loc}", "url": url,
    }


def sreality():
    items, seen_ids = [], set()
    for district in DISTRICTS:
        for page in range(1, SR_MAX_PAGES + 1):
            r = fetch(f"https://www.sreality.cz/hledani/pronajem/byty/praha-{district}",
                      params={"velikost": ",".join(SR_DISPS), "cena-do": PRICE_FURNISHED,
                              "strana": page, "razeni": "nejnovejsi"})
            data = next_data(r.text)
            if not data:
                t = re.search(r"<title>(.*?)</title>", r.text, re.S)
                title = t.group(1).strip()[:60] if t else "-"
                raise RuntimeError(f"Sreality: нет __NEXT_DATA__ (url={r.url[:90]}, "
                                   f"title={title!r}, {len(r.text)} байт) — похоже на блокировку/страницу согласия")
            new = 0
            for d in walk(data):
                it = sr_candidate(d)
                if not it or it["id"] in seen_ids:
                    continue
                seen_ids.add(it["id"])
                new += 1
                it["locality"] = it["locality"] or f"Praha {district}"
                items.append(it)
            if new == 0:
                if page == 1:
                    pp = ((data.get("props") or {}).get("pageProps") or {})
                    n_price = sum(1 for d in walk(data) if any(k.lower().startswith("price") for k in d))
                    print(f"Sreality praha-{district}: объявлений не найдено; "
                          f"ключи pageProps: {list(pp)[:8]}; словарей с price*: {n_price}")
                break
            time.sleep(1)
    if not items:
        raise RuntimeError("Sreality: ни одного объявления не распознано (структура страницы изменилась?)")
    return items


def sreality_detail(item):
    r = fetch(item["url"])
    data = next_data(r.text)
    if data:
        parts = []
        for d in walk(data):
            if str(d.get("id", d.get("hash_id", d.get("hashId")))) == item["ref"] and len(d) > 3:
                parts += strings_of(d)
        if parts:
            return " ".join(parts)
    return page_text(r.text)


# ====================== BEZREALITKY ======================
def bz_in_districts(address):
    a = norm(address)
    if not a:
        return True        # адрес неизвестен: не отсеиваем
    m = re.search(r"praha\s*-?\s*(\d{1,2})\b", a)
    if m:
        return int(m.group(1)) in DISTRICTS
    for d in DISTRICTS:
        for area in BZ_AREAS.get(d, []):
            if norm(area) in a:
                return True
    return False


def bz_item(d):
    if not ("uri" in d and "disposition" in d and "id" in d):
        return None
    disp = str(d["disposition"]).replace("DISP_", "").replace("_", "+").lower()
    if disp not in ALLOWED_DISP:
        return None
    price = getf(d, "price")
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    addr = getf(d, "address")
    addr = addr if isinstance(addr, str) else ""
    if not bz_in_districts(addr):
        return None
    surface = getf(d, "surface")
    charges = getf(d, "charges")
    return {
        "id": f"bz:{d['id']}", "source": "Bezrealitky", "ref": d["uri"],
        "title": disp + (f" · {surface} м²" if surface else ""),
        "locality": addr, "price": int(price),
        "charges": int(charges) if isinstance(charges, (int, float)) and charges else None,
        "text": " ".join(strings_of(d)),
        "url": f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{d['uri']}",
    }


def bezrealitky_graphql():
    items, seen_ids = [], set()
    disp = ", ".join(BEZ_DISPS)
    for page in range(MAX_PAGES):
        q = ("query { listAdverts(offerType: PRONAJEM, estateType: BYT, "
             f"disposition: [{disp}], priceTo: {PRICE_FURNISHED}, "
             'regionOsmIds: ["R435514"], '
             f"limit: 60, offset: {page * 60}, order: TIMEORDER_DESC) "
             "{ totalCount list { id uri disposition surface price charges "
             "gps { lat lng } address(locale: CS) } } }")
        r = S.post("https://api.bezrealitky.cz/graphql/", json={"query": q}, timeout=30,
                   headers={"Origin": "https://www.bezrealitky.cz",
                            "Referer": "https://www.bezrealitky.cz/"})
        print(f"POST {r.status_code} graphql page {page + 1}")
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:250]!r}")
        j = r.json()
        if j.get("errors"):
            raise RuntimeError("GraphQL: " + json.dumps(j["errors"], ensure_ascii=False)[:300])
        lst = (((j.get("data") or {}).get("listAdverts")) or {}).get("list") or []
        if not lst:
            break
        for d in lst:
            if d.get("id") in seen_ids:
                continue
            seen_ids.add(d.get("id"))
            it = bz_item(d)
            if it:
                items.append(it)
        time.sleep(1)
    return items


def bezrealitky_html():
    params = [("offerType", "PRONAJEM"), ("estateType", "BYT"),
              ("regionOsmIds", "R435514"),
              ("osm_value", "Praha, Hlavní město Praha, Česko"),
              ("currency", "CZK"), ("priceTo", PRICE_FURNISHED)]
    params += [("disposition", d) for d in BEZ_DISPS]
    last = None
    for path in ("/vyhledat", "/vyhledavani"):
        try:
            items, seen_ids = [], set()
            for page in range(1, MAX_PAGES + 1):
                r = fetch("https://www.bezrealitky.cz" + path, params=params + [("page", page)])
                data = next_data(r.text)
                if data is None:
                    raise RuntimeError("в HTML нет __NEXT_DATA__")
                new = 0
                for d in walk(data):
                    if "uri" in d and "disposition" in d and "id" in d:
                        if d["id"] in seen_ids:
                            continue
                        seen_ids.add(d["id"])
                        new += 1
                        it = bz_item(d)
                        if it:
                            items.append(it)
                if new == 0:
                    if page == 1:
                        raise RuntimeError("на странице нет объявлений")
                    break
                time.sleep(1)
            return items
        except Exception as e:  # noqa: BLE001
            last = RuntimeError(f"{path}: {e}")
    raise last


def bezrealitky():
    return first_working("Bezrealitky", (bezrealitky_graphql, bezrealitky_html))


def bezrealitky_detail(item):
    r = fetch(item["url"])
    data = next_data(r.text)
    aid = item["id"].split(":", 1)[1]
    if data:
        parts = []
        for d in walk(data):
            if str(d.get("id")) == aid and "uri" in d:
                parts += strings_of(d)
        if parts:
            return " ".join(parts)
    return page_text(r.text)


DETAIL = {"Sreality": sreality_detail, "Bezrealitky": bezrealitky_detail}


# ====================== ЛОГИКА ======================
def analyze(text):
    t = norm(text)
    if re.search(NOT_FURNISHED, t):
        furnished = False
    elif re.search(FURNISHED, t):
        furnished = True
    else:
        furnished = None
    equip = {k: bool(re.search(p, t)) for k, p in EQUIP.items()}
    balcony = bool(re.search(BALCONY, t))
    if re.search(NO_PETS, t):
        pets = "no"
    elif re.search(PETS_AGREEMENT, t):
        pets = "maybe"
    elif re.search(PETS_OK, t):
        pets = "yes"
    else:
        pets = None
    return furnished, equip, balcony, pets


def build_message(it, furnished, equip, balcony, pets=None):
    mark = lambda ok: "✅" if ok else "❓"
    esc = htmllib.escape
    price = f"{money(it['price'])} Kč"
    if it["charges"]:
        price += f" (+{money(it['charges'])} poplatky)"
    furn_txt = {True: "есть / частично", False: "нет (лимит 30 000)", None: "не указано"}[furnished]
    lines = [
        f"🏠 <b>{esc(it['title'])} · {price}</b>",
        f"📍 {esc(it['locality'])}",
        "  ".join(f"{k} {mark(v)}" for k, v in equip.items()),
        f"🌿 балкон/терраса {mark(balcony)}",
        "🐾 животные: " + {"yes": "✅ разрешены", "maybe": "⚠️ по договорённости",
                         "no": "❌ нельзя", None: "❓ не указано"}[pets],
        f"🛋 мебель: {furn_txt}",
        f"🔗 {it['source']}: {it['url']}",
        "❓ = в тексте не упомянуто, проверь в объявлении",
    ]
    return "\n".join(lines)


def tg(text):
    if not TOKEN or not CHAT_ID:
        print("--- (dry run, нет токена) ---\n" + text + "\n")
        return
    # TELEGRAM_CHAT_ID может содержать несколько ID через запятую: "111,222"
    for chat in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                          timeout=30)
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:200])


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}


def run_once():
    state = load_state()
    seen = state.setdefault("seen", {})
    errors = state.setdefault("errors", {})
    now = int(time.time())

    candidates = []
    for name, fn in (("Sreality", sreality), ("Bezrealitky", bezrealitky)):
        try:
            got = fn()
            print(f"{name}: найдено кандидатов {len(got)}")
            candidates += got
            if errors.get(name):
                tg(f"✅ {name}: снова работает")
            errors[name] = False
        except Exception as e:  # noqa: BLE001
            print(f"{name} error: {e}")
            last = errors.get(name)
            last = 0 if (last is True or not last) else last   # время последнего предупреждения
            if now - last > 6 * 3600:      # напоминаем об ошибке раз в 6 часов
                tg(f"⚠️ {name}: не удалось получить объявления — {str(e)[:700]}")
                last = now
            errors[name] = last or True

    sent = details = 0
    for it in candidates:
        if it["id"] in seen:
            continue
        if it["price"] > PRICE_FURNISHED:
            seen[it["id"]] = now
            continue
        if sent >= MAX_MESSAGES_PER_RUN or details >= MAX_DETAILS_PER_RUN:
            break  # остальные останутся "непросмотренными" и уйдут в следующий запуск
        try:
            text = it["text"] + " " + DETAIL[it["source"]](it)
        except Exception as e:  # noqa: BLE001
            print(f"detail error {it['id']}: {e}")
            text = it["text"]
        details += 1
        time.sleep(1)
        furnished, equip, balcony, pets = analyze(text)
        limit = PRICE_UNFURNISHED if furnished is False else PRICE_FURNISHED
        seen[it["id"]] = now
        if it["price"] > limit:
            continue
        if sum(equip.values()) < MIN_EQUIP_MENTIONED:
            continue
        if SKIP_IF_NO_PETS and pets == "no":
            continue
        tg(build_message(it, furnished, equip, balcony, pets))
        sent += 1
        time.sleep(0.5)

    cutoff = now - SEEN_TTL_DAYS * 86400
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    print(f"Готово: отправлено {sent}, открыто деталей {details}, кандидатов {len(candidates)}")


def main():
    # LOOP_SECONDS=180 -> работать бесконечно, проверяя сайты каждые ~3 минуты
    # (для домашнего компьютера / сервера). Без переменной: один проход.
    loop = int(os.environ.get("LOOP_SECONDS", "0") or 0)
    if not loop:
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print("Ошибка цикла:", e)
        time.sleep(loop + random.randint(0, 30))


if __name__ == "__main__":
    main()
