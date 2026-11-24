#!/usr/bin/env python3
"""Aktien-News-Alarm: Yahoo Finance + Google News (RSS) + SEC-8-K  ->  Push per ntfy.sh.

Kein Account und keine Telefonnummer nötig. Nur Python-Standardbibliothek, keine KI.
Die Watchlist steht in watchlist.txt und wird bei jedem Lauf neu gelesen.

Pro Lauf gibt es höchstens ZWEI Pushs:
  (Wichtig)   ...  gebündelte Liste der wichtigen Meldungen
  (Unwichtig) ...  gebündelte Liste der übrigen Meldungen
Ähnliche Meldungen werden zusammengefasst, Titel ins Deutsche übersetzt.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("STATE_FILE") or BASE / "state.json")
WATCHLIST_FILE = Path(os.environ.get("WATCHLIST_FILE") or BASE / "watchlist.txt")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = (os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "stock-alerts-bot admin@example.com"
MAX_AGE_H = int(os.environ.get("MAX_AGE_HOURS") or "12")             # ältere Meldungen ignorieren
IMPORTANT_MIN = int(os.environ.get("IMPORTANT_MIN") or "5")          # ab dieser Punktzahl: "wichtig"
MAX_LINES_IMPORTANT = int(os.environ.get("MAX_LINES_IMPORTANT") or "10")   # Themen pro Push
MAX_LINES_OTHER = int(os.environ.get("MAX_LINES_OTHER") or "10")
MOVE_PCT = float(os.environ.get("MOVE_PCT") or "8")                   # Kurssprung ab x % gilt als Markt-Meldung
TOPIC_COOLDOWN_H = float(os.environ.get("TOPIC_COOLDOWN_HOURS") or "6")  # gleiches Thema pro Firma nur 1x wichtig
SEC_MARKET = (os.environ.get("SEC_MARKET") or "1") != "0"             # SEC_MARKET=0 schaltet SEC-Marktüberwachung aus
MAX_LINES_MARKET = int(os.environ.get("MAX_LINES_MARKET") or "8")
MARKET = (os.environ.get("MARKET") or "1") != "0"                    # MARKET=0 schaltet den Markt-Teil aus
MAX_TRANSLATE = int(os.environ.get("MAX_TRANSLATE") or "40")         # Übersetzungen pro Lauf
TRANSLATE = (os.environ.get("TRANSLATE") or "1") != "0"              # TRANSLATE=0 schaltet aus
DRY = not NTFY_TOPIC                                                 # ohne Thema: nur ausgeben

BROWSER_UA = "Mozilla/5.0 (compatible; stock-alerts-bot/1.0)"
START = time.time()
TIME_BUDGET = float(os.environ.get("TIME_BUDGET_SECONDS") or "150")  # danach keine Übersetzung/Markt-Abfrage mehr
SEEN_TTL = 7 * 86400
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,11}$")

# Bedeutung der 8-K-Item-Nummern
SEC_ITEMS = {
    "1.01": "Wichtiger Vertrag / Deal",
    "1.02": "Vertrag beendet",
    "1.03": "Insolvenz",
    "2.01": "Übernahme/Verkauf abgeschlossen",
    "2.02": "Quartalszahlen",
    "2.03": "Neue Schulden",
    "2.05": "Restrukturierung",
    "2.06": "Wertminderung",
    "3.01": "Delisting-Hinweis",
    "3.02": "Neue Aktien ausgegeben",
    "4.01": "Wechsel des Wirtschaftsprüfers",
    "4.02": "Bilanzkorrektur",
    "5.01": "Kontrollwechsel",
    "5.02": "Führungswechsel (CEO/CFO/Vorstand)",
    "5.03": "Satzungsänderung",
    "5.07": "Hauptversammlung",
    "7.01": "Mitteilung (Reg FD)",
    "8.01": "Sonstige Ereignisse",
}
# Diese 8-K-Typen gelten als "wichtig", der Rest als "unwichtig"
SEC_IMPORTANT = {"1.01", "1.02", "1.03", "2.01", "2.02", "2.05", "2.06", "3.01", "4.02", "5.01", "5.02"}


# ---------------------------------------------------------------- Bewertung (Wichtigkeit)

_CEO = r"(?:ceo|chief executive|cfo|chairman|founder)"
# "CEO ... tritt zurück/kommt zurück": Wechsel-Wort steht nach dem Titel
_CHANGE_AFTER = (r"(?:resign\w*|steps? down|stepping down|quits?|fired|ousted|replaced?|appoint\w*|"
                 r"names? new|return(?:s|ed)?|exits?|departs?|departure|retires?|retirement|successor|leaves|leaving)")
# "... Rücktritt des CEO": Wechsel-Wort steht vor dem Titel ("returns" bewusst nicht dabei)
_CHANGE_BEFORE = (r"(?:resign\w*|steps? down|stepping down|quits?|fired|ousted|appoint\w*|"
                  r"names? new|departure|retires?|retirement|successor)")

# (Punkte, [Muster]) – jede Regel zählt höchstens einmal pro Schlagzeile
RULES = [
    (12, [rf"\b{_CEO}\b[^.]{{0,70}}?\b{_CHANGE_AFTER}\b",
         rf"\b{_CHANGE_BEFORE}\b[^.]{{0,70}}?\b{_CEO}\b"]),                # Führungswechsel
    (5, [r"\b(?:acquir\w+|acquisition|merger|takeover|buyout|take[- ]private|bankruptcy|chapter 11|"
         r"insolven\w+|delist\w*|trading halt|halts? trading)\b"]),        # Übernahme / Pleite
    (5, [r"\b(?:lawsuit|sues?|sued|settle\w*|antitrust|investigation|probe|subpoena|indict\w*|"
         r"fraud|class action|fines?|fined|penalt\w+)\b"]),                # Recht / Untersuchung
    (5, [r"\b(?:earnings|quarterly results|guidance|outlook|profit warning|beats?|misses|"
         r"raises forecast|cuts forecast)\b"]),                            # Zahlen
    (5, [r"\b(?:recall\w*|data breach|cyberattack|hack(?:ed)?|outage|shutdown)\b"]),
    (5, [r"\b(?:layoffs?|job cuts|lays? off|laying off|restructuring|plant closure|strike)\b",
         r"\bcuts? [\d,.]+k? (?:\w+ )?(?:jobs|positions|workers|employees)\b"]),
    (4, [r"\b(?:stock split|buyback|share repurchase|special dividend|dividend (?:hike|cut|increase))\b"]),
    (4, [r"\b(?:billions?|trillions?)\b", r"\$\s?\d[\d.,]*\s?(?:bn|b)\b"]),             # große Summen
    (3, [r"\b(?:deal|partnership|agreement|contract|investment|invests?|stake)\b"]),
    (3, [r"\b(?:tariffs?|sanctions?|export (?:ban|controls?|curbs?|restrictions?)|bans?|banned|"
         r"approval|approves?|fda)\b"]),
    (3, [r"\b(?:upgrade[sd]?|downgrade[sd]?)\b"]),
    (3, [r"\b(?:launch\w*|unveil\w*|announce\w*|introduc\w+)\b"]),
    (1, [r"\b(?:ceo|cfo)\b"]),
    (1, [r"\b(?:price target|forecast|analyst)\b"]),
]
RULES = [(w, [re.compile(p, re.I) for p in pats]) for w, pats in RULES]

# Füllmaterial / Klickköder: zieht Punkte ab
NOISE = re.compile(
    r"\b(?:should you|better buy|stocks? to buy|millionaire|motley fool|zacks|reasons? to buy|"
    r"if you invested|worth buying|no[- ]brainer|buy now|top \d+ stocks?|\d+ (?:stocks?|reasons|things)|"
    r"what to know|here'?s why|ahead of earnings)\b", re.I)


def score_news(title):
    s = sum(w for w, pats in RULES if any(p.search(title) for p in pats))
    if NOISE.search(title):
        s -= 6
    return s




# ---------------------------------------------------------------- Markt-Teil (ohne Watchlist)

# Große Firmen: nur wenn eine davon (oder eine Milliardensumme) vorkommt, ist eine allgemeine Meldung interessant
_BIG_NAMES = """Apple|Microsoft|Nvidia|Alphabet|Google|Amazon|Meta Platforms|Facebook|Tesla|Berkshire Hathaway|
JPMorgan|Mastercard|Walmart|Exxon|Chevron|Johnson & Johnson|Pfizer|Eli Lilly|Novo Nordisk|UnitedHealth|
Procter & Gamble|Coca-Cola|PepsiCo|Disney|Netflix|Oracle|Salesforce|Adobe|Intel|AMD|Qualcomm|Broadcom|
TSMC|Taiwan Semiconductor|ASML|Samsung|SK Hynix|Sony|Toyota|Volkswagen|BMW|Mercedes-Benz|Siemens|SAP|
Airbus|Boeing|Lockheed Martin|Raytheon|BP|TotalEnergies|Saudi Aramco|Aramco|Alibaba|Tencent|BYD|Xiaomi|
Huawei|Nestle|Novartis|Roche|AstraZeneca|LVMH|HSBC|Goldman Sachs|Morgan Stanley|Bank of America|
Citigroup|Wells Fargo|BlackRock|OpenAI|Anthropic|xAI|SpaceX|Palantir|Uber|Airbnb|PayPal|Starbucks|
McDonald|Nike|Costco|Home Depot|Ford|General Motors|Stellantis|Ferrari|Deutsche Bank|Allianz|
Deutsche Telekom|Comcast|Verizon|AT&T|Cisco|IBM|Micron|Arm Holdings|SoftBank|Foxconn|Intel|
Merck|AbbVie|Bayer|Visa Inc|American Express|Shopify|Spotify|ByteDance|TikTok|Rivian|Lucid|
Lufthansa|Nissan|Honda|Hyundai|Rio Tinto|BHP|Glencore|Vale|Maersk|Walgreens|CVS|Target Corp""".replace("\n", "")
BIG_RE = re.compile(r"\b(?:" + "|".join(re.escape(n.strip()) for n in sorted(set(_BIG_NAMES.split("|")), key=len, reverse=True)
                                         if n.strip()) + r")\b", re.I)
_CUTS_JOBS = re.compile(r"\bcuts? [\d,.]+k? (?:\w+ )?(?:jobs|positions|workers|employees)\b", re.I)
BILLION_RE = re.compile(r"\b(?:billions?|trillions?)\b|\$\s?\d[\d.,]*\s?(?:bn|b)\b", re.I)

# (Muster, Bedingung)  Bedingung: "big" = große Firma nötig, "bill" = Milliardensumme nötig,
# "any" = eins von beiden, "free" = immer (z. B. Übernahme-Spekulation)
_SPEC = re.compile(r"\b(?:takeover (?:interest|talks|speculation|bid|offer|approach)|buyout (?:talks|interest|offer)|"
                   r"explor\w+ (?:a )?(?:sale|strategic alternatives)|strategic (?:alternatives|review)|activist|"
                   r"unsolicited (?:bid|offer|proposal)|in talks to (?:buy|acquire|merge|sell)|merger talks|"
                   r"sweetened (?:bid|offer)|tender offer|poison pill)\b", re.I)
_MOVER = re.compile(r"\b(?:soars?|jumps?|surges?|spikes?|rall(?:y|ies)|rockets?|skyrockets?|plunges?|tumbles?|sinks?|"
                    r"crashes?|slumps?|plummets?|climbs?|rises?|gains?|falls?|drops?|slides?|up|down)\b", re.I)
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")


def big_move(title):
    """Kurssprung/-absturz ab MOVE_PCT Prozent (z. B. 'Aktie steigt um 11 %')."""
    return bool(_MOVER.search(title)) and any(float(x) >= MOVE_PCT for x in _PCT.findall(title))


MARKET_EVENTS = [
    (RULES[0][1], "big"),                                                         # Führungswechsel
    ([re.compile(r"\b(?:acquir\w+|acquisition|merger|merges?|merged|takeover|take[- ]private|buyout|takes? over)\b", re.I)], "any"),
    ([_SPEC], "free"),                                                            # Übernahme-Spekulation, Aktivisten
    ([re.compile(r"\b(?:bankruptcy|chapter 11|insolven\w+|delist\w*|defaults?)\b", re.I)], "any"),
    ([re.compile(r"\b(?:deals?|partnership|agreement|contract|joint venture|invest(?:s|ed|ing|ments?)|stake|funding|financing)\b", re.I)], "bill"),
    ([re.compile(r"\b(?:lawsuit|sues?|sued|settle\w*|antitrust|investigation|probe|subpoena|indict\w*|fraud|fines?|fined|penalt\w+)\b", re.I)], "any"),
    ([re.compile(r"\b(?:tariffs?|sanctions?|export (?:ban|controls?|curbs?|restrictions?))\b", re.I)], "any"),
    ([re.compile(r"\b(?:recall\w*|data breach|cyberattack|outage)\b", re.I)], "big"),
    ([re.compile(r"\b(?:stock split|buyback|share repurchase|special dividend)\b", re.I)], "big"),
    ([re.compile(r"\b(?:layoffs?|job cuts|lays? off|laying off)\b", re.I), _CUTS_JOBS], "big"),
]


def market_hit(title):
    """Strenger Filter: große Ereignisse bei großen Firmen, Milliardensummen, Spekulation, Kurssprünge."""
    if NOISE.search(title):
        return False
    if big_move(title):
        return True
    big, bill = bool(BIG_RE.search(title)), bool(BILLION_RE.search(title))
    for pats, need in MARKET_EVENTS:
        if any(p.search(title) for p in pats):
            if need == "free" or (need == "big" and big) or (need == "bill" and bill) or (need == "any" and (big or bill)):
                return True
    return False


MARKET_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/bulletins"),
    ("Investing.com", "https://www.investing.com/rss/news_356.rss"),
]
MARKET_QUERIES = [
    "(acquire OR acquisition OR merger OR takeover OR buyout) billion when:1d",
    '(CEO OR "chief executive") (resigns OR "steps down" OR ousted OR appointed OR "names new") when:1d',
    "(deal OR partnership OR agreement) billion when:1d",
    '(bankruptcy OR antitrust OR recall OR "data breach" OR layoffs) when:1d',
]


def collect_market():
    """Liest die allgemeinen Markt-Feeds. Gibt (Meldungen, erreichbare Quellen, Quellen gesamt) zurück."""
    out, ok = [], 0
    for name, url in MARKET_FEEDS:
        raw = http_get(url, timeout=10)  # kurzes Limit: ein hängender Feed soll den Lauf nicht aufhalten
        if raw is None:
            continue
        ok += 1
        for i in parse_rss(raw):
            out.append({**i, "id": f"mkt|{norm(i['title'])}", "kind": "news", "src": name,
                        "pub": name, "mkt": True})
    for q in MARKET_QUERIES:
        raw = http_get("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
                       + "&hl=en-US&gl=US&ceid=US:en", timeout=10)
        if raw is None:
            continue
        ok += 1
        for i in parse_rss(raw):
            clean, pub = split_pub(i["title"])
            out.append({**i, "id": f"mkt|{norm(i['title'])}", "kind": "news", "src": "Google News",
                        "pub": pub or "Google News", "title": clean, "desc": "", "mkt": True})
    out = list({it["id"]: it for it in reversed(out)}.values())
    return out, ok, len(MARKET_FEEDS) + len(MARKET_QUERIES)


# Frühwarnung direkt aus der SEC-Datenbank (erscheint oft vor den Nachrichten):
# (Formular, Kategorie, Text, Punkte)
SEC_MARKET_FORMS = [
    ("SCHEDULE 13D", "Großaktionär", "Investor meldet Beteiligung über 5 % mit möglicher Einflussnahme (Schedule 13D)", 9),
    ("SC 13D", "Großaktionär", "Investor meldet Beteiligung über 5 % mit möglicher Einflussnahme (Schedule 13D)", 9),
    ("SC TO-T", "Übernahme", "Öffentliches Übernahmeangebot an die Aktionäre (Tender Offer)", 10),
    ("425", "Übernahme", "Mitteilung zu Fusion/Übernahme (Form 425)", 9),
    ("DEFM14A", "Übernahme", "Aktionäre stimmen über Fusion ab (Proxy DEFM14A)", 9),
]
_ATOM = "{http://www.w3.org/2005/Atom}"
_ATOM_TITLE = re.compile(r"^(?P<form>.+?)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{4,10})\)\s*(?:\((?P<role>[^)]*)\))?\s*$")


def collect_sec_market():
    """Neueste Übernahme-/Großaktionär-Meldungen aller börsennotierten US-Firmen.
    Gibt (Meldungen, erfolgreiche Abfragen, Abfragen gesamt, gelesene Einträge) zurück."""
    cmap = {cik: (t, title) for t, (cik, title) in sec_tickers().items()}
    out, ok, read = [], 0, 0
    for form, cat, label, score0 in SEC_MARKET_FORMS:
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=" + urllib.parse.quote(form)
               + "&company=&dateb=&owner=include&start=0&count=40&output=atom")
        raw = http_get(url, SEC_UA, timeout=10)
        if raw is None:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        ok += 1
        for e in root.iter(_ATOM + "entry"):
            read += 1
            m = _ATOM_TITLE.match((e.findtext(_ATOM + "title") or "").strip())
            if not m:
                continue
            f = m.group("form").strip().upper()
            role = (m.group("role") or "").lower()
            if not f.startswith(form) or f.endswith("/A") or "filed by" in role or "reporting" in role:
                continue
            info = cmap.get(int(m.group("cik")))
            if not info:
                continue  # nicht als börsennotiert bekannt
            ticker = info[0]
            register_company(ticker, info)
            link_el = e.find(_ATOM + "link")
            link = link_el.get("href") if link_el is not None else ""
            acc = re.search(r"(\d{10}-\d{2}-\d{6})", (e.findtext(_ATOM + "summary") or "") + link)
            ts = None
            try:
                ts = datetime.fromisoformat((e.findtext(_ATOM + "updated") or "").replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
            out.append({"id": f"secm|{acc.group(1) if acc else link}|{ticker}", "kind": "secm", "src": "SEC",
                        "pub": "SEC", "title": label, "link": link or "https://www.sec.gov/edgar/search/",
                        "ts": ts, "desc": "", "cat": cat, "score0": score0, "ticker_m": ticker, "mkt": True})
    out = list({it["id"]: it for it in out}.values())
    return out, ok, len(SEC_MARKET_FORMS), read


_TEASER = re.compile(r"^(?:what|why|how|here'?s|can|could|should|is|are|will|does|do|who|which|where)\b|"
                     r"^[^:]{0,40}:\s*(?:what|why|how|can|could|should|is|are|will|does|do)\b|"
                     r"\b(?:what (?:has |have )?(?:changed|happened)|what to know|what it means|key takeaways?)\b", re.I)


def is_teaser(title):
    """Klickköder-Titel ('Was sich geändert hat…'), die nichts Konkretes sagen."""
    t = title.strip()
    plain = re.sub(r"\b(?:vs|inc|corp|co|ltd|no|u\.s)\.", "", t, flags=re.I)  # Abkürzungen sind kein Satzende
    if t.endswith("?") and not re.search(r"[.!]\s+\S", plain):  # Frage ohne vorherigen Aussagesatz
        return True
    return bool(_TEASER.search(t))


def score_item(it):
    if it["kind"] == "sec":
        return 10 if any(n in SEC_IMPORTANT for n in it.get("nums", [])) else 2
    if it["kind"] == "secm":
        return it["score0"]
    s = score_news(it["title"])
    if it.get("mkt"):  # Markt-Meldungen: große Firmen / Milliarden zählen für die Reihenfolge mit
        s += (3 if BIG_RE.search(it["title"]) else 0) + (2 if BILLION_RE.search(it["title"]) else 0)
    if is_teaser(it["title"]):
        s -= 2
    return s


# ---------------------------------------------------------------- Hilfsfunktionen

def http_get(url, ua=BROWSER_UA, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # Netzwerkfehler dürfen den Lauf nicht abbrechen
        print(f"WARN {url[:70]}… -> {type(e).__name__}: {e}", file=sys.stderr)
        return None


def read_watchlist():
    out = []
    if WATCHLIST_FILE.exists():
        for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
            for t in re.split(r"[\s,]+", line.split("#")[0].upper()):
                if t and TICKER_RE.match(t) and t not in out:
                    out.append(t)
    return out


def load_state():
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("seen", {})
    s.setdefault("baselined", [])
    s.setdefault("greeted", False)
    s.setdefault("topics", {})
    return s


def save_state(state):
    cutoff = time.time() - SEEN_TTL
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    state["topics"] = {k: v for k, v in state.get("topics", {}).items() if v > time.time() - 86400}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- Push (ntfy)

def send(title, message, link=None, tags=None, priority=3, actions=None):
    """Schickt eine Push-Nachricht über ntfy. Gibt True zurück, wenn sie angenommen wurde."""
    if DRY:
        print(f"--- PUSH (Prio {priority}) ---\n{title}\n{message}\n{link or ''}\n")
        return True
    body = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority}
    if link:
        body["click"] = link  # Tippen auf die Push öffnet die wichtigste Meldung
    if tags:
        body["tags"] = tags
    if actions:
        body["actions"] = actions
    req = urllib.request.Request(
        NTFY_SERVER + "/",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"WARN ntfy-Push fehlgeschlagen: {type(e).__name__}", file=sys.stderr)
        return False


# ---------------------------------------------------------------- Übersetzung (Englisch -> Deutsch)

_tr_fail = 0
_tr_count = 0


def translate(text):
    """Übersetzt eine Schlagzeile ins Deutsche. Bei jedem Problem kommt der Originaltext zurück."""
    global _tr_fail, _tr_count
    if (not TRANSLATE or _tr_fail >= 2 or _tr_count >= MAX_TRANSLATE or not text
            or time.time() - START > TIME_BUDGET):
        return text
    _tr_count += 1
    out = None

    # 1) Google-Übersetzer (inoffizielle, schlüsselfreie Schnittstelle)
    raw = http_get("https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "de", "dt": "t", "q": text}))
    if raw:
        try:
            out = "".join(seg[0] for seg in json.loads(raw)[0] if seg and seg[0]) or None
        except Exception:
            out = None

    # 2) Ersatz: MyMemory (kostenlos, anonym begrenzt)
    if not out:
        raw = http_get("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
            {"q": text, "langpair": "en|de"}))
        if raw:
            try:
                d = json.loads(raw)
                cand = d["responseData"]["translatedText"]
                if str(d.get("responseStatus")) == "200" and cand and "MYMEMORY WARNING" not in cand.upper():
                    out = cand
            except Exception:
                out = None

    if out:
        _tr_fail = 0
        time.sleep(0.15)  # freundlich bleiben
        return html.unescape(out).strip()
    _tr_fail += 1
    return text


# ---------------------------------------------------------------- Quellen

def clean_html(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s or "")))
    return " ".join(s.split())


def parse_rss(raw):
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        ts = None
        pub = it.findtext("pubDate")
        if pub:
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except Exception:
                pass
        desc = clean_html(it.findtext("description"))
        if len(desc) < 30 or desc.lower().startswith(title.lower()[:40]):
            desc = ""  # nur echte Zusatzinfos behalten
        if title and link:
            items.append({"title": title, "link": link, "ts": ts, "desc": desc[:400]})
    return items


def norm(title):
    """Titel normalisieren, damit dieselbe Meldung aus zwei Quellen nur einmal kommt.
    (Format unverändert, damit die gespeicherten IDs in state.json gültig bleiben.)"""
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)  # Google hängt " - Quelle" an
    return re.sub(r"\W+", " ", t.lower()).strip()[:90]


_PUB = re.compile(r"\s+[-–—|]\s+([^-–—|]{2,40})$")


def split_pub(title):
    """'Nvidia CEO tritt zurück - Reuters' -> ('Nvidia CEO tritt zurück', 'Reuters')"""
    m = _PUB.search(title)
    if m:
        return title[:m.start()].strip(), m.group(1).strip()
    return title.strip(), None


_sec_map = {}
_sec_tried = False


def sec_tickers():
    global _sec_tried
    if not _sec_map and not _sec_tried:
        _sec_tried = True  # pro Lauf nur ein Versuch, auch bei Fehlern
        raw = http_get("https://www.sec.gov/files/company_tickers.json", SEC_UA)
        if raw:
            try:
                for v in json.loads(raw).values():
                    _sec_map[v["ticker"].upper()] = (int(v["cik_str"]), v["title"])
            except Exception:
                pass
    return _sec_map


_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|ltd|plc|holdings|the)\b\.?", re.I)


def short_name(sec_title):
    n = re.sub(r"/[A-Za-z]+/", "", sec_title).replace(",", "")
    return re.sub(r"\s+", " ", _SUFFIX.sub("", n)).strip()


def sec_filings(ticker, cik):
    raw = http_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", SEC_UA)
    if raw is None:
        return None
    try:
        rec = json.loads(raw)["filings"]["recent"]
    except Exception:
        return []
    out = []
    items_col = rec.get("items") or []
    for i in range(min(20, len(rec["form"]))):
        form = rec["form"][i]
        if form not in ("8-K", "8-K/A", "6-K"):
            continue
        acc = rec["accessionNumber"][i]
        nums = [x.strip() for x in (items_col[i] if i < len(items_col) else "").split(",")]
        labels = [SEC_ITEMS[x] for x in nums if x in SEC_ITEMS]
        try:
            ts = datetime.fromisoformat(rec["acceptanceDateTime"][i].replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = None
        out.append({
            "id": f"sec|{acc}",
            "kind": "sec",
            "src": "SEC",
            "pub": "SEC",
            "nums": nums,
            "ts": ts,
            "title": f"{form}: " + (", ".join(labels) or "Neue Pflichtmeldung"),
            "link": f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                    f"{acc.replace('-', '')}/{rec['primaryDocument'][i]}",
        })
    return out


def collect(ticker):
    """Sammelt Meldungen aus allen Quellen. Gibt (Liste, Anzahl erfolgreicher Quellen) zurück."""
    out, ok = [], 0
    info = sec_tickers().get(ticker)
    register_company(ticker, info)

    raw = http_get("https://feeds.finance.yahoo.com/rss/2.0/headline?s="
                   f"{urllib.parse.quote(ticker)}&region=US&lang=en-US")
    if raw is not None:
        ok += 1
        for i in parse_rss(raw):
            out.append({**i, "id": f"{ticker}|{norm(i['title'])}", "kind": "news",
                        "src": "Yahoo Finance", "pub": "Yahoo Finance"})

    query = f'"{short_name(info[1])}" stock' if info else f"{ticker} stock"
    raw = http_get("https://news.google.com/rss/search?q="
                   + urllib.parse.quote(query + " when:1d") + "&hl=en-US&gl=US&ceid=US:en")
    if raw is not None:
        ok += 1
        for i in parse_rss(raw):
            clean, pub = split_pub(i["title"])
            out.append({**i, "id": f"{ticker}|{norm(i['title'])}", "kind": "news",
                        "src": "Google News", "pub": pub or "Google News", "title": clean, "desc": ""})

    if info:
        filings = sec_filings(ticker, info[0])
        if filings is not None:
            ok += 1
            out.extend(filings)
    for it in out:
        it["rel"] = is_relevant(ticker, it)  # handelt die Meldung wirklich von dieser Firma?
    return out, ok


# ---------------------------------------------------------------- Zusammenfassen ähnlicher Meldungen

_STOP = set("the and for with from after over amid into than more new says say said will would could "
            "may can has have had not but about out off its this that these those are was were been "
            "stock stocks shares share".split())


# Großgeschriebene Wörter, die keine Firmennamen sind
_NOT_NAMES = _STOP | set("ceo cfo us uk eu ai etf ipo why how what who when here report reports update "
                         "breaking exclusive watch live opinion".split())


def tokens(title):
    """Wortmenge einer Schlagzeile. Firmennamen (großgeschrieben) werden mit '§' markiert."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    out = {w[:-1] if len(w) > 4 and w.endswith("s") else w
           for w in words if (len(w) > 2 or w.isdigit()) and w not in _STOP}
    for n in re.findall(r"\b[A-Z][A-Za-z0-9&]+", title):
        if n.lower() not in _NOT_NAMES:
            out.add("§" + n.lower())
    return out


def similar(a, b):
    if not a or not b:
        return False
    names_a = {t for t in a if t.startswith("§")}
    names_b = {t for t in b if t.startswith("§")}
    if names_a and names_b and not (names_a & names_b):
        return False  # andere Firmen -> andere Geschichte
    inter = len(a & b)
    return inter / len(a | b) >= 0.5 or (inter >= 4 and inter / min(len(a), len(b)) >= 0.75)


def build_clusters(entries):
    """entries: Liste von (Ticker, Meldung). Gibt Themen-Gruppen zurück (ähnliche Meldungen vereint)."""
    prepared = []
    for t, it in entries:
        it = dict(it)
        it["ticker"] = t
        it["score"] = score_item(it)
        it["tok"] = tokens(it["title"]) if it["kind"] == "news" else set()
        prepared.append(it)
    prepared.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))

    clusters = []
    for it in prepared:
        target = None
        if it["kind"] == "news":
            for c in clusters:
                if c["kind"] == "news" and similar(it["tok"], c["rep"]["tok"]):
                    target = c
                    break
        if target is None:
            target = {"kind": it["kind"], "rep": it, "score": it["score"], "items": [],
                      "tickers": [], "pubs": [], "norms": set()}
            clusters.append(target)
        target["items"].append(it)
        if it["ticker"] and it["ticker"] not in target["tickers"]:
            target["tickers"].append(it["ticker"])
        if it.get("pub") and it["pub"] not in target["pubs"]:
            target["pubs"].append(it["pub"])
        target["norms"].add(it["id"] if it["kind"] == "secm" else norm(it["title"]))  # SEC-Meldungen einzeln zählen
    return clusters


# ---------------------------------------------------------------- Themen pro Firma

# Begriffe, an denen man erkennt, dass eine Meldung wirklich von dieser Firma handelt
ALIASES = {
    "NVDA": ["nvidia", "jensen huang", "huang", "geforce", "cuda", "blackwell"],
    "TSLA": ["tesla", "cybertruck", "robotaxi", "optimus"],
    "AAPL": ["apple", "iphone", "ipad", "macbook", "tim cook", "app store", "vision pro"],
    "MSFT": ["microsoft", "azure", "copilot", "xbox", "nadella", "linkedin"],
    "GOOGL": ["google", "alphabet", "youtube", "waymo", "gemini", "deepmind"],
    "GOOG": ["google", "alphabet", "youtube", "waymo", "gemini", "deepmind"],
    "AMZN": ["amazon", "aws", "alexa", "jassy"],
    "META": ["meta platforms", "meta", "facebook", "instagram", "whatsapp", "zuckerberg"],
    "ASML": ["asml"],
    "TSM": ["tsmc", "taiwan semiconductor"],
    "AMD": ["amd", "ryzen", "radeon", "lisa su", "epyc"],
    "INTC": ["intel"],
    "NFLX": ["netflix"],
    "PLTR": ["palantir"],
}
_GENERIC = set("united american general first national global international great western eastern "
               "southern northern pacific atlantic".split())
TERMS, DISPLAY = {}, {}


def nice_name(s):
    s = s.strip()
    return s.title() if s.isupper() and len(s) > 4 else s


def register_company(ticker, info):
    """Merkt sich Anzeigenamen und Erkennungsbegriffe einer Firma."""
    if ticker in TERMS:
        return
    terms = set(ALIASES.get(ticker, []))
    base = ticker.split(".")[0].lower()
    if len(base) >= 3 and base.isalnum():
        terms.add(base)
    if info:
        nm = short_name(info[1]).lower()
        if len(nm) >= 3:
            terms.add(nm)
        first = nm.split()[0] if nm.split() else ""
        if len(first) >= 5 and first not in _GENERIC:
            terms.add(first)
        DISPLAY.setdefault(ticker, nice_name(short_name(info[1])))
    if ticker in ALIASES:
        DISPLAY.setdefault(ticker, ALIASES[ticker][0].title() if ticker not in ("ASML", "AMD", "TSM") else
                           {"ASML": "ASML", "AMD": "AMD", "TSM": "TSMC"}[ticker])
    if not info and ticker not in ALIASES:
        terms = set()  # Firma unbekannt -> Relevanz nicht beurteilbar, nichts aussortieren
    TERMS[ticker] = (re.compile(r"\b(?:" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")\b",
                                re.I) if terms else None)


def display(ticker):
    return DISPLAY.get(ticker) or ticker


def is_relevant(ticker, it):
    """Yahoo liefert zu einem Ticker auch Randthemen. Nur Meldungen, die die Firma nennen, gelten als relevant."""
    if it["kind"] != "news":
        return True
    rx = TERMS.get(ticker)
    return True if rx is None else bool(rx.search(it["title"]))


def _c(*pats):
    return [re.compile(p, re.I) for p in pats]


# Themen-Kategorien (Reihenfolge = Vorrang)
CATS = [
    ("Führung", RULES[0][1] + _c(
        r"\b(?:return(?:s|ed)?|comeback)\b[^.]{0,40}\b(?:ceo|chief executive|huang|founder)\b",
        r"\b(?:ceo|chief executive|huang|founder)\w*['’]?s? (?:return|comeback)\b")),
    ("Übernahme", _c(r"\b(?:acquir\w+|acquisition|merger|merges?|merged|takeover|take[- ]private|buyout|takes? over)\b") + [_SPEC]),
    ("Recht", _c(r"\b(?:lawsuit|sues?|sued|settle\w*|antitrust|investigation|probe|subpoena|indict\w*|fraud|class action|fines?|fined|penalt\w+)\b")),
    ("Zahlen", _c(r"\b(?:earnings|quarterly results|guidance|outlook|profit warning|beats?|misses|revenue|forecast)\b")),
    ("Störfall", _c(r"\b(?:recall\w*|data breach|cyberattack|hack(?:ed)?|outage|shutdown)\b")),
    ("Stellen", _c(r"\b(?:layoffs?|job cuts|lays? off|laying off|restructuring)\b") + [_CUTS_JOBS]),
    ("Kapital", _c(r"\b(?:stock split|buyback|share repurchase|special dividend|dividend)\b")),
    ("Deal", _c(r"\b(?:deals?|partnership|agreement|contract|joint venture|invest(?:s|ed|ing|ments?)|stake|funding|financing)\b")),
    ("Politik", _c(r"\b(?:tariffs?|sanctions?|export (?:ban|controls?|curbs?|restrictions?)|bans?|banned)\b")),
    ("Produkt", _c(r"\b(?:launch\w*|unveil\w*|announce\w*|introduc\w+|release[sd]?)\b")),
    ("Analysten", _c(r"\b(?:upgrade[sd]?|downgrade[sd]?|price target|rating|analysts?|overweight|underweight|outperform)\b")),
    ("Kurs", _c(r"\b(?:soars?|jumps?|surges?|rall(?:y|ies)|plunges?|tumbles?|sinks?|slumps?|climbs?|rises?|falls?|drops?|gains?)\b",
                r"\d\s?%")),
]
_NO_MERGE = ("Sonstiges", "SEC")


def category_of(it):
    if it.get("cat"):
        return it["cat"]
    if it["kind"] == "sec":
        return "SEC"
    for label, pats in CATS:
        if any(p.search(it["title"]) for p in pats):
            return label
    return "Sonstiges"


def primary_ticker(c):
    """Zu welcher Firma gehört das Thema? Die, deren Name im Titel zuerst vorkommt."""
    best, pos = "", 10 ** 9
    for t in c["tickers"]:
        rx = TERMS.get(t)
        m = rx.search(c["rep"]["title"]) if rx else None
        p = m.start() if m else 10 ** 8
        if p < pos:
            best, pos = t, p
    return best


def lead_sentence(desc):
    m = re.match(r"(.{40,220}?[.!?])(?:\s|$)", desc)
    return m.group(1) if m else desc[:200].rsplit(" ", 1)[0]


def _finalize(c, first=True):
    c["rep"] = min(c["items"], key=lambda x: (is_teaser(x["title"]), -x["score"], -(x["ts"] or 0)))
    c["score"] = max(x["score"] for x in c["items"])
    c["n"] = len(c["norms"])
    if first:
        c["cat"] = category_of(c["rep"])
        c["company"] = primary_ticker(c)


def build_topics(entries):
    """Meldungen -> Themen: ähnliche zusammenfassen, dann pro Firma und Kategorie bündeln."""
    clusters = build_clusters(entries)
    for c in clusters:
        _finalize(c)
    out, keyed = [], {}
    for c in sorted(clusters, key=lambda c: (-c["score"], -(c["rep"]["ts"] or 0))):
        if c["company"] and c["cat"] not in _NO_MERGE:
            key = (c["company"], c["cat"])
            if key in keyed:
                t = keyed[key]
                t["items"].extend(c["items"])
                t["norms"] |= c["norms"]
                t["pubs"].extend(p for p in c["pubs"] if p not in t["pubs"])
                t["tickers"].extend(k for k in c["tickers"] if k not in t["tickers"])
                continue
            keyed[key] = c
        out.append(c)
    for c in out:
        _finalize(c, first=False)
    return out


# ---------------------------------------------------------------- Push-Aufbau

def short(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def topic_text(c):
    """Deutscher Satz zum Thema. Bei Klickköder-Titeln wird der Anfang des Artikeltexts genommen."""
    r = c["rep"]
    if r["kind"] in ("sec", "secm"):
        return r["title"]
    txt = r["title"]
    if is_teaser(txt) and r.get("desc"):
        txt = lead_sentence(r["desc"]) or txt
    return translate(txt)


def topic_line(c):
    meta = []
    if c["pubs"]:
        meta.append(", ".join(c["pubs"][:3]) + ("…" if len(c["pubs"]) > 3 else ""))
    if c["n"] > 1:
        meta.append(f"+{c['n'] - 1} weitere")
    s = f"• {c['text_de']}" if c["cat"] == "SEC" else f"• {c['cat']}: {c['text_de']}"
    return s + (f" ({'; '.join(meta)})" if meta else "")


def push_group(label, topics, max_lines, priority, tags, carry=False, per_company=2):
    """Sendet eine gebündelte Push (pro Firma ein Block). Gibt (Erfolg, enthaltene Themen) zurück."""
    if not topics:
        return True, []
    topics.sort(key=lambda c: (-c["score"], -(c["rep"]["ts"] or 0)))
    cand, per = [], {}
    for c in topics:
        k = c["company"]
        if (k and per.get(k, 0) >= per_company) or len(cand) >= max_lines:
            continue
        per[k] = per.get(k, 0) + 1
        cand.append(c)
    for c in cand:  # nur übersetzen, was auch angezeigt wird
        c["text_de"] = topic_text(c)

    n_items = sum(c["n"] for c in topics)
    head = f"{len(topics)} Themen" + (f" aus {n_items} Meldungen" if n_items > len(topics) else "")
    blocks, order, inc, size = {}, [], [], len(head)
    for c in cand:
        ln = topic_line(c)
        if size + len(ln.encode("utf-8")) > 3400:
            break
        size += len(ln.encode("utf-8")) + 1
        if c["company"] not in blocks:
            blocks[c["company"]] = []
            order.append(c["company"])
        blocks[c["company"]].append(ln)
        inc.append(c)

    lines = [head]
    for comp in order:
        lines.append("")
        if comp:
            lines.append(display(comp).upper())
        lines.extend(blocks[comp])
    hidden = len(topics) - len(inc)
    if hidden > 0:
        lines += ["", f"… und {hidden} weitere Themen" + (" (kommen im nächsten Lauf)" if carry else "")]

    top = inc[0]
    title = short(f"({label}) " + (f"{display(top['company']).upper()}: " if top["company"] else "")
                  + top["text_de"], 110)
    actions = [{"action": "view", "label": f"Meldung {k + 1}", "url": c["rep"]["link"]}
               for k, c in enumerate(inc[:3])]
    ok = send(title, "\n".join(lines), top["rep"]["link"], tags=tags, priority=priority, actions=actions)
    return ok, inc


def prepare_market(state, wl, queue, offtopic, now):
    """Allgemeine Markt-Feeds + SEC-Frühwarnung + Randthemen aus der Watchlist -> nur wirklich große Themen."""
    raw, ok, total = collect_market()
    print(f"Markt-Quellen: {ok} von {total} erreichbar")
    if SEC_MARKET:
        try:
            s_items, s_ok, s_total, s_read = collect_sec_market()
            print(f"SEC-Markt: {s_ok} von {s_total} Abfragen ok, {s_read} Einträge gelesen, "
                  f"{len(s_items)} börsennotierte Treffer")
            raw, ok = raw + s_items, ok + s_ok
        except Exception as e:
            print(f"WARN SEC-Markt fehlgeschlagen: {type(e).__name__}: {e}", file=sys.stderr)
    raw = list({it["id"]: it for it in raw + offtopic}.values())
    if ok == 0:
        return []
    if not state.get("market_baselined"):
        for it in raw:  # erster Lauf: Altbestand stumm als gesehen markieren
            state["seen"][it["id"]] = now
        state["market_baselined"] = True
        print(f"Markt-Baseline: {len(raw)} Meldungen übersprungen")
        return []
    wl_tok = [tokens(it["title"]) for _, it in queue if it["kind"] == "news"]
    entries = []
    for it in raw:
        if it["id"] in state["seen"]:
            continue
        if it["kind"] == "secm":
            old = it["ts"] and it["ts"] < now - MAX_AGE_H * 3600
            if old:
                state["seen"][it["id"]] = now
            else:
                entries.append((it["ticker_m"], it))
            continue
        key = it["id"][4:]
        done = ((not it.get("off") and any(f"{t}|{key}" in state["seen"] for t in wl))  # kam schon über die Watchlist
                or (it["ts"] and it["ts"] < now - MAX_AGE_H * 3600)       # zu alt
                or not market_hit(it["title"])                            # nicht groß genug
                or any(similar(tokens(it["title"]), w) for w in wl_tok))  # steckt schon in deiner Push
        if done:
            state["seen"][it["id"]] = now
            continue
        entries.append(("", it))
    return build_topics(entries) if entries else []


def apply_cooldown(state, topics, now):
    """Dasselbe Thema (Firma + Kategorie) soll nicht mehrfach als 'wichtig' kommen."""
    for c in topics:
        if c["company"] and c["cat"] not in _NO_MERGE:
            ts = state["topics"].get(f"{c['company']}|{c['cat']}")
            if ts and now - ts < TOPIC_COOLDOWN_H * 3600:
                c["score"] = min(c["score"], IMPORTANT_MIN - 1)


# ---------------------------------------------------------------- Hauptprogramm

def main():
    if NTFY_TOPIC and len(NTFY_TOPIC) < 16:
        print("WARN: NTFY_TOPIC ist kurz und leicht zu erraten. Nimm einen langen, zufälligen Namen.",
              file=sys.stderr)

    state = load_state()
    wl = read_watchlist()
    if not wl:
        print("WARN: watchlist.txt ist leer oder fehlt.", file=sys.stderr)
    # Aktien, die entfernt wurden, beim erneuten Hinzufügen wieder neu "baselinen"
    state["baselined"] = [t for t in state["baselined"] if t in wl]

    if not DRY and not state["greeted"]:
        if send("Aktien-Bot läuft", "Überwache: " + (", ".join(wl) or "leer"),
                tags=["white_check_mark"]):
            state["greeted"] = True

    now = time.time()
    queue, offtopic = [], []
    for t in wl:
        items, ok = collect(t)
        if ok == 0:
            continue  # alle Quellen down -> nichts als "gesehen" markieren
        items = list({it["id"]: it for it in reversed(items)}.values())  # Duplikate raus
        if t not in state["baselined"]:
            # Erster Lauf für diese Aktie: Altbestand stumm als gesehen markieren
            for it in items:
                state["seen"][it["id"]] = now
            state["baselined"].append(t)
            print(f"Baseline für {t}: {len(items)} Meldungen übersprungen")
            continue
        for it in items:
            if it["id"] in state["seen"]:
                continue
            if it["ts"] and it["ts"] < now - MAX_AGE_H * 3600:
                state["seen"][it["id"]] = now
                continue
            if not it.get("rel", True):
                # Randthema (z. B. andere Firma): nicht in die Watchlist-Push, aber als Markt-Kandidat prüfen
                state["seen"][it["id"]] = now
                offtopic.append({**it, "id": f"mkt|{norm(it['title'])}", "mkt": True, "off": True})
                continue
            queue.append((t, it))

    topics = build_topics(queue) if queue else []
    apply_cooldown(state, topics, now)
    important = [c for c in topics if c["score"] >= IMPORTANT_MIN]
    other = [c for c in topics if c["score"] < IMPORTANT_MIN]

    market = []
    if MARKET and time.time() - START < TIME_BUDGET:
        try:
            market = prepare_market(state, wl, queue, offtopic, now)
            apply_cooldown(state, market, now)
        except Exception as e:  # der Markt-Teil darf den Rest nie stören
            print(f"WARN Markt-Teil fehlgeschlagen: {type(e).__name__}: {e}", file=sys.stderr)

    # Reihenfolge: Wichtig, Markt, Unwichtig (die ersten bekommen das Übersetzungs-Kontingent zuerst).
    # carry=True: was nicht in die Push passt, kommt im nächsten Lauf.
    for label, group, max_lines, prio, tags, carry, per_co in (
        ("Wichtig", important, MAX_LINES_IMPORTANT, 4, ["rotating_light"], True, 2),
        ("Markt", market, MAX_LINES_MARKET, 3, ["globe_with_meridians"], True, 2),
        ("Unwichtig", other, MAX_LINES_OTHER, 2, ["newspaper"], False, 1),
    ):
        ok, shown = push_group(label, group, max_lines, prio, tags, carry, per_co)
        if ok:
            for c in (shown if carry else group):
                for it in c["items"]:
                    state["seen"][it["id"]] = now
                if label != "Unwichtig" and c["company"] and c["cat"] not in _NO_MERGE:
                    state["topics"][f"{c['company']}|{c['cat']}"] = now

    print(f"Fertig: {len(queue)} neue Watchlist-Meldungen -> {len(important)} wichtige, {len(other)} unwichtige "
          f"Themen; Markt: {len(market)} Themen. Watchlist: {', '.join(wl) or '-'}")
    save_state(state)


if __name__ == "__main__":
    main()
