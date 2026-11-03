#!/usr/bin/env python3
"""Мониторинг аренды квартир в Праге: Sreality + Bezrealitky -> Telegram.

Запускается раз в час (GitHub Actions). Помнит уже отправленные объявления
в state.json, поэтому дублей не будет.
"""
import html as htmllib
import json
import math
import os
import re
import time
import unicodedata
from pathlib import Path

import requests

# ====================== НАСТРОЙКИ ======================
KOBYLISY = (50.1256, 14.4660)   # метро Кобылисы (lat, lon), приблизительно
MAX_KM = 12.0                   # отсев по прямой от Кобылис (грубая замена "30 минут")
PRICE_FURNISHED = 35000         # лимит, если мебель есть / не указана
PRICE_UNFURNISHED = 30000       # лимит, если квартира пустая
MIN_EQUIP_MENTIONED = 0         # сколько из 4 (ванна/духовка/посудомойка/стиралка)
                                # должно быть упомянуто в тексте; 0 = не фильтровать
MAX_MESSAGES_PER_RUN = 20       # защита от потока сообщений
MAX_PAGES = 5
STATE_FILE = Path("state.json")
SEEN_TTL_DAYS = 120

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Диспозиции: 1+1, 2+kk, 2+1, 3+kk, 3+1, 4+kk, 4+1, 5+kk, 5+1
ALLOWED_DISP = {"1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "4+1", "5+kk", "5+1"}
SREALITY_SUBS = "3|4|5|6|7|8|9|10|11"          # коды sreality для списка выше
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


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


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


def strings_of(d):
    """Все строки словаря + имена булевых полей со значением True."""
    out = []
    for k, v in d.items():
        if isinstance(v, str):
            out.append(v)
        elif v is True:
            out.append(k)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            continue
    return out


def money(v):
    return f"{int(v):,}".replace(",", " ")


# ====================== SREALITY ======================
def sreality():
    items, seen_ids = [], set()
    for page in range(1, MAX_PAGES + 1):
        r = S.get("https://www.sreality.cz/api/cs/v2/estates", params={
            "category_main_cb": 1, "category_type_cb": 2,
            "category_sub_cb": SREALITY_SUBS,
            "locality_region_id": 10,
            "czk_price_summary_order2": f"0|{PRICE_FURNISHED}",
            "per_page": 60, "page": page, "sort": 0,
        }, timeout=30)
        r.raise_for_status()
        estates = r.json().get("_embedded", {}).get("estates", [])
        if not estates:
            break
        for e in estates:
            hid = str(e.get("hash_id"))
            if hid in seen_ids:
                continue
            seen_ids.add(hid)
            name = e.get("name", "")
            m = re.search(r"\d\+(?:kk|\d)", name, re.I)
            disp = m.group(0).lower() if m else ""
            if disp and disp not in ALLOWED_DISP:
                continue
            price = e.get("price") or (e.get("price_czk") or {}).get("value_raw")
            if not price:
                continue
            gps = e.get("gps") or {}
            loc = (gps["lat"], gps["lon"]) if gps.get("lat") and gps.get("lon") else None
            slug = (e.get("seo") or {}).get("locality", "praha")
            items.append({
                "id": f"sr:{hid}", "source": "Sreality", "ref": hid,
                "title": name, "locality": e.get("locality", ""),
                "price": int(price), "charges": None, "gps": loc,
                "text": name + " " + e.get("locality", ""),
                "url": f"https://www.sreality.cz/detail/pronajem/byt/{disp or 'x'}/{slug}/{hid}",
            })
        time.sleep(1)
    if not items:
        raise RuntimeError("Sreality вернул 0 объявлений (блокировка или изменился API)")
    return items


def sreality_detail(item):
    r = S.get(f"https://www.sreality.cz/api/cs/v2/estates/{item['ref']}", timeout=30)
    r.raise_for_status()
    j = r.json()
    parts = [(j.get("text") or {}).get("value", "")]
    for it in j.get("items", []):
        v = it.get("value")
        if isinstance(v, bool):
            if v:
                parts.append(str(it.get("name", "")))
        else:
            parts.append(f"{it.get('name', '')} {json.dumps(v, ensure_ascii=False)}")
    return " ".join(parts)


# ====================== BEZREALITKY ======================
def bezrealitky():
    items, seen_ids = [], set()
    base = [("offerType", "PRONAJEM"), ("estateType", "BYT"),
            ("regionOsmIds", "R435514"),
            ("osm_value", "Praha, Hlavní město Praha, Česko"),
            ("currency", "CZK"), ("priceTo", PRICE_FURNISHED)]
    base += [("disposition", d) for d in BEZ_DISPS]
    for page in range(1, MAX_PAGES + 1):
        r = S.get("https://www.bezrealitky.cz/vyhledavani",
                  params=base + [("page", page)], timeout=30)
        r.raise_for_status()
        data = next_data(r.text)
        if data is None:
            raise RuntimeError("Bezrealitky: не найден __NEXT_DATA__ (блокировка или новая вёрстка)")
        new = 0
        for d in walk(data):
            if not ("uri" in d and "disposition" in d and "id" in d):
                continue
            aid = str(d["id"])
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            new += 1
            disp = str(d["disposition"]).replace("DISP_", "").replace("_", "+").lower()
            if disp not in ALLOWED_DISP:
                continue
            price = getf(d, "price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            gps = getf(d, "gps")
            loc = None
            if isinstance(gps, dict):
                lat, lon = gps.get("lat"), gps.get("lng", gps.get("lon"))
                if lat and lon:
                    loc = (float(lat), float(lon))
            addr = getf(d, "address") or ""
            surface = getf(d, "surface")
            title = f"{disp}" + (f" · {surface} м²" if surface else "")
            charges = getf(d, "charges")
            items.append({
                "id": f"bz:{aid}", "source": "Bezrealitky", "ref": d["uri"],
                "title": title, "locality": addr if isinstance(addr, str) else "",
                "price": int(price),
                "charges": int(charges) if isinstance(charges, (int, float)) and charges else None,
                "gps": loc,
                "text": " ".join(strings_of(d)),
                "url": f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{d['uri']}",
            })
        if page == 1 and new == 0:
            raise RuntimeError("Bezrealitky: на странице нет объявлений (изменилась структура?)")
        if new == 0:
            break
        time.sleep(1)
    return items


def bezrealitky_detail(item):
    r = S.get(item["url"], timeout=30)
    r.raise_for_status()
    data = next_data(r.text)
    aid = item["id"].split(":", 1)[1]
    if data:
        parts = []
        for d in walk(data):
            if str(d.get("id")) == aid and "uri" in d:
                parts += strings_of(d)
        if parts:
            return " ".join(parts)
    return re.sub(r"<[^>]+>", " ", r.text)


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
    return furnished, equip, balcony


def build_message(it, furnished, equip, balcony, dist):
    mark = lambda ok: "✅" if ok else "❓"
    esc = htmllib.escape
    price = f"{money(it['price'])} Kč"
    if it["charges"]:
        price += f" (+{money(it['charges'])} poplatky)"
    furn_txt = {True: "есть / частично", False: "нет (лимит 30 000)", None: "не указано"}[furnished]
    lines = [
        f"🏠 <b>{esc(it['title'])} · {price}</b>",
        f"📍 {esc(it['locality'])}" + (f" · {dist:.1f} км от Кобылис по прямой" if dist is not None else " · координат нет"),
        "  ".join(f"{k} {mark(v)}" for k, v in equip.items()),
        f"🌿 балкон/терраса {mark(balcony)}",
        f"🛋 мебель: {furn_txt}",
        f"🔗 {it['source']}: {it['url']}",
    ]
    if it["gps"]:
        o = f"{it['gps'][0]},{it['gps'][1]}"
        d = f"{KOBYLISY[0]},{KOBYLISY[1]}"
        lines.append(f"🚇 https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}&travelmode=transit")
    lines.append("❓ = в тексте не упомянуто, проверь в объявлении")
    return "\n".join(lines)


def tg(text):
    if not TOKEN or not CHAT_ID:
        print("--- (dry run, нет токена) ---\n" + text + "\n")
        return
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}


def main():
    state = load_state()
    seen = state.setdefault("seen", {})
    errors = state.setdefault("errors", {})
    now = int(time.time())

    candidates = []
    for name, fn in (("Sreality", sreality), ("Bezrealitky", bezrealitky)):
        try:
            candidates += fn()
            if errors.get(name):
                tg(f"✅ {name}: снова работает")
            errors[name] = False
        except Exception as e:  # noqa: BLE001
            print(f"{name} error: {e}")
            if not errors.get(name):
                tg(f"⚠️ {name}: не удалось получить объявления — {e}")
            errors[name] = True

    sent = 0
    for it in candidates:
        if it["id"] in seen:
            continue
        # дешёвые проверки без запроса детали
        dist = km(it["gps"], KOBYLISY) if it["gps"] else None
        if it["price"] > PRICE_FURNISHED or (dist is not None and dist > MAX_KM):
            seen[it["id"]] = now
            continue
        if sent >= MAX_MESSAGES_PER_RUN:
            break  # остальные останутся "непросмотренными" и уйдут в следующий запуск
        try:
            text = it["text"] + " " + DETAIL[it["source"]](it)
        except Exception as e:  # noqa: BLE001
            print(f"detail error {it['id']}: {e}")
            text = it["text"]
        time.sleep(1)
        furnished, equip, balcony = analyze(text)
        limit = PRICE_UNFURNISHED if furnished is False else PRICE_FURNISHED
        seen[it["id"]] = now
        if it["price"] > limit:
            continue
        if sum(equip.values()) < MIN_EQUIP_MENTIONED:
            continue
        tg(build_message(it, furnished, equip, balcony, dist))
        sent += 1
        time.sleep(0.5)

    cutoff = now - SEEN_TTL_DAYS * 86400
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    print(f"Готово: отправлено {sent}, кандидатов {len(candidates)}")


if __name__ == "__main__":
    main()
