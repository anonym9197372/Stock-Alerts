#!/usr/bin/env python3
"""Aktien-News-Alarm: Yahoo Finance + Google News (RSS) + SEC-8-K  ->  Push per ntfy.sh.

Kein Account und keine Telefonnummer nötig. Nur Python-Standardbibliothek.
Optional: eine lokale KI (Ollama) bewertet die Themen und schreibt die deutschen Zeilen; ohne Ollama
läuft alles wie bisher mit Regeln + Google-Übersetzung.
Die Watchlist steht in watchlist.txt und wird bei jedem Lauf neu gelesen.

Zwei Arten von Pushs:
  (Wichtig) 🔴        ...  eine eigene Push pro wirklich dringender Meldung (höchstens MAX_URGENT pro Lauf),
                           mit verständlicher Erklärung: Was ist passiert? Warum wichtig?
  (Zusammenfassung)  ...  alles andere, gesammelt und höchstens alle DIGEST_HOURS Stunden als EINE Push
Ähnliche Meldungen werden zusammengefasst, alles ist auf Deutsch.
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
AI = (os.environ.get("AI") or "1") != "0"                            # AI=0 schaltet die lokale KI aus
OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL") or "qwen2.5:3b"                # Ollama-Modell (muss per 'ollama pull' da sein)
AI_BUDGET = float(os.environ.get("AI_BUDGET_SECONDS") or "120")      # so lange darf die KI pro Lauf höchstens rechnen
AI_CALL_TIMEOUT = float(os.environ.get("AI_CALL_TIMEOUT") or "90")   # Limit pro KI-Anfrage (inkl. Modell laden)
AI_BATCH = int(os.environ.get("AI_BATCH") or "6")                    # Themen pro KI-Anfrage
AI_MAX = int(os.environ.get("AI_MAX_TOPICS") or "24")                # höchstens so viele Themen pro Lauf bewerten
AI_MODEL_URGENT = os.environ.get("AI_MODEL_URGENT") or AI_MODEL       # optional: größeres Modell nur für Eilmeldungen
AI_DEEP_BUDGET = float(os.environ.get("AI_DEEP_BUDGET_SECONDS") or "100")  # Zeit für Eilmeldungs-Texte + Überblick
URGENT_MIN = int(os.environ.get("URGENT_MIN") or "10")                # ohne KI: ab dieser Regel-Punktzahl = dringend
MAX_URGENT = int(os.environ.get("MAX_URGENT") or "3")                 # höchstens so viele Einzel-Pushs pro Lauf
DIGEST_H = float(os.environ.get("DIGEST_HOURS") or "3")               # Zusammenfassung höchstens alle x Stunden
DIGEST_MAX_LINES = int(os.environ.get("DIGEST_MAX_LINES") or "18")    # Zeilen pro Zusammenfassung
DIGEST_FORCE = int(os.environ.get("DIGEST_FORCE_AT") or "40")         # ab so vielen gesammelten Meldungen sofort senden
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
    s.setdefault("pending", [])      # Meldungen für die nächste Zusammenfassung
    s.setdefault("urgent_log", [])   # schon gesendete Eilmeldungen (gegen Wiederholungen)
    return s


def save_state(state):
    cutoff = time.time() - SEEN_TTL
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    state["topics"] = {k: v for k, v in state.get("topics", {}).items() if v > time.time() - 86400}
    state["urgent_log"] = [l for l in state.get("urgent_log", []) if l["ts"] > time.time() - 86400]
    state["pending"] = [r for r in state.get("pending", []) if r["t"] > time.time() - 36 * 3600][:120]
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

_gtx_dead = False
_mm_used = 0
MM_MAX = 8  # MyMemory ist anonym stark begrenzt: nur wenige Einzelübersetzungen pro Lauf


def _gtx_batch(texts):
    """Alle Titel in EINER Anfrage an den Google-Übersetzer (inoffiziell, ohne Schlüssel)."""
    q = "\n".join(" ".join(t.split()) for t in texts)
    raw = http_get("https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "de", "dt": "t", "q": q}))
    if not raw:
        return None
    try:
        lines, tr, src = [], "", ""
        for seg in json.loads(raw)[0]:
            if not seg or seg[0] is None:
                continue
            tr += seg[0]
            src += seg[1] or ""
            if src.endswith("\n"):  # Zeilenende der Quelle = ein Titel ist fertig
                lines.append(tr.strip())
                tr = src = ""
        if tr or src:
            lines.append(tr.strip())
        return lines if len(lines) == len(texts) else None
    except Exception:
        return None


def _mymemory(text):
    raw = http_get("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
        {"q": text, "langpair": "en|de"}))
    if not raw:
        return None
    try:
        d = json.loads(raw)
        cand = d["responseData"]["translatedText"]
        if str(d.get("responseStatus")) == "200" and cand and "MYMEMORY WARNING" not in cand.upper():
            return cand
    except Exception:
        pass
    return None


def translate_many(texts):
    """Übersetzt Schlagzeilen ins Deutsche. Bei jedem Problem bleibt der englische Originaltext."""
    global _gtx_dead, _mm_used
    out = list(texts)
    if not TRANSLATE or not texts or time.time() - START - _ai["spent"] > TIME_BUDGET:
        return out
    texts = texts[:MAX_TRANSLATE]
    if not _gtx_dead:
        res = _gtx_batch(texts)
        if res:
            for k, r in enumerate(res):
                out[k] = html.unescape(r).strip() or out[k]
            return out
        _gtx_dead = True  # blockiert (z. B. 429): in diesem Lauf nicht mehr versuchen
    for k, t in enumerate(texts):  # Ersatz: MyMemory, nur wenige
        if _mm_used >= MM_MAX:
            break
        _mm_used += 1
        r = _mymemory(t)
        if r:
            out[k] = html.unescape(r).strip()
    return out


def translate(text):
    return translate_many([text])[0]


# ---------------------------------------------------------------- Lokale KI (Ollama, optional)
# Läuft komplett auf dem eigenen Rechner bzw. dem GitHub-Runner: kein Konto, kein Schlüssel, keine Kosten.
# Die KI darf NUR bewerten (0-5) und eine deutsche Zeile formulieren. Sie kann nichts löschen, was die
# Regeln für wichtig halten, und bei jedem Problem (nicht erreichbar, zu langsam, Unsinn) gilt weiter die Regel-Logik.

_ai = {"ready": None, "dead": False, "spent": 0.0, "deep_spent": 0.0, "rated": 0, "dropped": 0, "deep": 0}
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # lokaler Dienst: nie über einen Proxy

AI_CATS = ["Führung", "Übernahme", "Recht", "Zahlen", "Störfall", "Stellen", "Kapital", "Deal", "Politik",
           "Produkt", "Analysten", "Kurs", "Sonstiges"]
AI_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object",
    "properties": {"n": {"type": "integer"}, "w": {"type": "integer"}, "k": {"type": "string", "enum": AI_CATS},
                   "de": {"type": "string"}, "who": {"type": "string"}},
    "required": ["n", "w", "k", "de", "who"]}}}, "required": ["items"]}

AI_SYSTEM = (
    "Du bist Nachrichten-Redakteur für Privatanleger, die viele der Firmen nicht kennen. Antworte ausschließlich "
    "mit JSON. Zu jeder nummerierten Meldung lieferst du:\n"
    "w = Wichtigkeit 0-5. SEI STRENG: von 10 Meldungen ist höchstens eine eine 5, die meisten sind 2 oder 3. "
    "Eine Milliardensumme allein macht eine Meldung nicht wichtig.\n"
    "  5 = muss ein Anleger sofort wissen (CEO-/Vorstandswechsel, Übernahme/Fusion einer großen Firma, Pleite, "
    "Gewinnwarnung, überraschende Quartalszahlen, Verbot, Sanktion, große Klage, Großrückruf)\n"
    "  4 = wichtig, konkrete Firmenneuigkeit mit echter Auswirkung\n"
    "  3 = normale Nachricht   2 = Randnotiz, Analystenmeinung   1 = Klickköder, Füllmaterial, Ratgeber\n"
    "  0 = betrifft die genannte Firma gar nicht\n"
    "k = die passende Kategorie.\n"
    "de = EIN einfacher deutscher Satz, höchstens 20 Wörter, ohne Fachjargon: was ist passiert und, wenn erkennbar, "
    "warum es wichtig ist. Nicht wörtlich übersetzen, sondern in einfachen Worten erklären. Firmennamen, Zahlen und "
    "Währungen behalten, nichts erfinden. 'billion' heißt Milliarde (Mrd.), 'trillion' heißt Billion.\n"
    "who = nur wenn bei der Meldung 'Firma: -' steht und die Firma kein bekannter Weltkonzern ist: "
    "'Name: kurze Beschreibung' (höchstens 7 Wörter, z. B. 'Flipkart: indischer Online-Händler, gehört Walmart'). "
    "Bist du nicht sicher oder ist die Firma allgemein bekannt: leere Zeichenkette.\n"
    'Beispiel: {"items":[{"n":1,"w":5,"k":"Führung","de":"Nvidia-Chef Huang tritt überraschend zurück, '
    'ein Nachfolger ist noch nicht genannt.","who":""}]}')


def _ollama_get(path, timeout=3):
    with _local.open(urllib.request.Request(OLLAMA_URL + path), timeout=timeout) as r:
        return json.loads(r.read())


def _ollama_post(path, body, timeout):
    req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with _local.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def ai_ready():
    """Ist Ollama erreichbar und das Modell vorhanden? (Ergebnis wird pro Lauf gemerkt.)"""
    if _ai["ready"] is None:
        ok = False
        if AI:
            try:
                names = [m.get("name", "") for m in _ollama_get("/api/tags").get("models", [])]
                ok = any(n == AI_MODEL or n == AI_MODEL + ":latest" or (":" not in AI_MODEL and n.split(":")[0] == AI_MODEL)
                         for n in names)
                print(f"KI: Ollama erreichbar, Modell {AI_MODEL} " + ("bereit" if ok else
                      f"FEHLT (vorhanden: {', '.join(names) or 'keins'}) -> Regel-Modus"))
            except Exception as e:
                print(f"KI: Ollama nicht erreichbar ({type(e).__name__}) -> Regel-Modus")
        _ai["ready"] = ok
    return _ai["ready"]


def _ai_line(i, c):
    meta = [display(c["company"]) if c["company"] else "-", c["cat"]]
    return f"{i}. Firma: {meta[0]} | Art: {meta[1]} | {short(topic_raw(c), 260)}"


def _numkeys(text):
    return {re.sub(r"\D", "", n).lstrip("0") for n in re.findall(r"\d[\d.,]*", text or "")} - {""}


def _nums_ok(out, source):
    """Erfundene Zahlen abfangen: jede Zahl (ab 2 Ziffern) im KI-Text muss auch in der Quelle stehen ('$7.5B' = '7,5')."""
    src = _numkeys(source)
    return all(k in src or len(k) < 2 for k in _numkeys(out))


def _ai_source(c):
    return " ".join(f"{it['title']} {it.get('desc', '')}" for it in c["items"])


def _ai_clean(s):
    s = re.sub(r"https?://\S+", "", str(s or "")).replace("\n", " ").strip(" \"'")
    return " ".join(s.split())


def _ai_batch(batch, timeout):
    """Eine Anfrage an die KI. Gibt {Nummer: (Wichtigkeit, Kategorie, deutsche Zeile, Firmenhinweis)} zurück."""
    msg = "Meldungen:\n" + "\n".join(_ai_line(i + 1, c) for i, c in enumerate(batch))
    resp = _ollama_post("/api/chat", {
        "model": AI_MODEL, "stream": False, "format": AI_SCHEMA, "keep_alive": "10m",
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 120 * len(batch)},
        "messages": [{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": msg}]}, timeout)
    data = json.loads(resp["message"]["content"])
    out = {}
    for it in data.get("items", []):
        try:
            n, w = int(it["n"]), int(it["w"])
        except Exception:
            continue
        de, who = _ai_clean(it.get("de")), _ai_clean(it.get("who"))
        k = it.get("k") if it.get("k") in AI_CATS else ""
        if 1 <= n <= len(batch) and 0 <= w <= 5 and n not in out:
            ok_de = (8 <= len(de) <= 240 and not _looks_english(de, batch[n - 1])
                     and _nums_ok(de, _ai_source(batch[n - 1])))
            ok_who = 3 <= len(who) <= 70 and ":" in who and not batch[n - 1]["company"]
            out[n] = (w, k, de if ok_de else "", who if ok_who else "")
    return out


def ai_rate(topics):
    """Bewertet die Themen (wichtigste zuerst, solange das Zeitbudget reicht). Setzt c['w'] und c['text_de']."""
    if not AI or not topics or not ai_ready():
        return
    cand = [c for c in sorted(topics, key=lambda c: (-c["score"], -(c["rep"]["ts"] or 0)))
            if c["rep"]["kind"] not in ("sec", "secm")][:AI_MAX]  # SEC-Meldungen sind harte Fakten: bleiben bei den Regeln
    t0 = time.time()
    for i in range(0, len(cand), AI_BATCH):
        left = AI_BUDGET - (time.time() - t0)
        if left < 8 or _ai["dead"]:
            break
        batch = cand[i:i + AI_BATCH]
        try:
            res = _ai_batch(batch, min(left, AI_CALL_TIMEOUT))
        except Exception as e:  # zu langsam / Unsinn / Dienst weg: Rest läuft mit Regeln
            print(f"WARN KI-Anfrage fehlgeschlagen ({type(e).__name__}) -> Rest im Regel-Modus", file=sys.stderr)
            _ai["dead"] = True
            break
        for n, (w, k, de, who) in res.items():
            c = batch[n - 1]
            c["w"] = w
            if k and k != "Sonstiges" and c["cat"] not in _NO_MERGE:  # "Sonstiges" der KI überschreibt keine konkrete Regel-Kategorie
                c["cat"] = k
            if de:
                c["text_de"] = de
            if who:
                c["who"] = who
            _ai["rated"] += 1
    _ai["spent"] += time.time() - t0
    print(f"KI: {_ai['rated']} von {len(cand)} Themen bewertet in {time.time() - t0:.0f} s")


_EN_STOP = set("the a an of for and with from after over amid into than to in on at as by is are was were be "
               "its his her their says say said will would could may new".split())


def _looks_english(de, c):
    """Hat die KI den englischen Titel nur abgeschrieben? (Dann lieber verwerfen und übersetzen.)"""
    w_de = set(re.findall(r"[a-zäöüß0-9]+", de.lower()))
    w_en = set(re.findall(r"[a-z0-9]+", topic_raw(c).lower()))
    if not w_de:
        return True
    overlap = len(w_de & w_en) / len(w_de)
    stop = len([w for w in re.findall(r"[a-z]+", de.lower()) if w in _EN_STOP])
    return overlap > 0.6 or stop >= 3


def ai_cap(group, keep):
    """Die KI vergibt gern zu viele 5er. Höchstens `keep` bleiben 'extrem wichtig', der Rest wird 4."""
    top = sorted([c for c in group if c.get("w") == 5], key=lambda c: (-c["score"], -(c["rep"]["ts"] or 0)))
    for c in top[keep:]:
        c["w"] = 4


def ai_translate(cand):
    """Themen ohne brauchbare KI-Zeile: die KI übersetzt/erklärt nur den Titel (kurze Anfrage). Fehler -> bleibt leer."""
    todo = [c for c in cand if "text_de" not in c and c["rep"]["kind"] not in ("sec", "secm")]
    if not AI or not todo or _ai["dead"] or not ai_ready():
        return
    t0 = time.time()
    for i in range(0, len(todo), 8):
        left = AI_BUDGET - _ai["spent"] - (time.time() - t0)
        if left < 8:
            break
        batch = todo[i:i + 8]
        msg = "Erkläre jede Schlagzeile in EINEM einfachen deutschen Satz (höchstens 20 Wörter), antworte als JSON " \
              '{"items":[{"n":1,"de":"..."}]}:\n' + "\n".join(f"{k + 1}. {short(topic_raw(c), 260)}" for k, c in enumerate(batch))
        try:
            resp = _ollama_post("/api/chat", {
                "model": AI_MODEL, "stream": False, "keep_alive": "10m",
                "format": {"type": "object", "properties": {"items": {"type": "array", "items": {
                    "type": "object", "properties": {"n": {"type": "integer"}, "de": {"type": "string"}},
                    "required": ["n", "de"]}}}, "required": ["items"]},
                "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 70 * len(batch)},
                "messages": [{"role": "system", "content": "Du übersetzt und erklärst Börsennachrichten für Laien auf Deutsch. "
                              "'billion' heißt Milliarde, 'trillion' heißt Billion. Nichts erfinden."},
                             {"role": "user", "content": msg}]}, min(left, AI_CALL_TIMEOUT))
            for it in json.loads(resp["message"]["content"]).get("items", []):
                n, de = int(it["n"]), _ai_clean(it.get("de"))
                if (1 <= n <= len(batch) and 8 <= len(de) <= 240 and not _looks_english(de, batch[n - 1])
                        and _nums_ok(de, _ai_source(batch[n - 1]))):
                    batch[n - 1]["text_de"] = de
        except Exception as e:
            print(f"WARN KI-Übersetzung fehlgeschlagen ({type(e).__name__}) -> Google", file=sys.stderr)
            _ai["dead"] = True
            break
    _ai["spent"] += time.time() - t0


def rank(c):
    """Sortierwert in einer Push: KI-Wichtigkeit, sonst aus den Regeln abgeleitet."""
    w = c.get("w")
    return w if w is not None else (4 if c["score"] >= IMPORTANT_MIN else 2)


def ai_drop(state, group, now):
    """Entfernt Themen, die die KI als 'betrifft die Firma gar nicht' (0) einstuft UND die Regeln nicht für wichtig halten."""
    keep = []
    for c in group:
        if c.get("w") == 0 and c["score"] < IMPORTANT_MIN:
            for it in c["items"]:
                state["seen"][it["id"]] = now
            _ai["dropped"] += 1
        else:
            keep.append(c)
    return keep


DEEP_SCHEMA = {"type": "object", "properties": {
    "urgent": {"type": "boolean"}, "head": {"type": "string"}, "was": {"type": "string"},
    "warum": {"type": "string"}, "wer": {"type": "string"}},
    "required": ["urgent", "head", "was", "warum", "wer"]}

DEEP_SYSTEM = (
    "Du schreibst Eilmeldungen für einen Privatanleger OHNE Vorwissen. Nutze AUSSCHLIESSLICH die gelieferten "
    "Schlagzeilen und Texte. Erfinde nichts: keine Zahlen, Namen, Gründe oder Folgen, die dort nicht stehen. "
    "Einfaches Deutsch, kurze Sätze, Fachwörter kurz erklären. Antworte nur mit JSON:\n"
    "urgent = true nur, wenn ein Anleger das noch heute wissen sollte (Führungswechsel, Übernahme/Fusion, Pleite, "
    "Gewinnwarnung, große Klage/Verbot/Sanktion, Großrückruf, Deal über Milliarden mit klarer Wirkung). "
    "Bei bloßem Gerede, Analystenmeinungen, Ratgebern oder unklarer Lage: false.\n"
    "head = Titelzeile mit Firmennamen, höchstens 80 Zeichen.\n"
    "was = 1-2 Sätze: Was ist passiert?\n"
    "warum = 1 Satz: Warum kann das für Aktionäre wichtig sein? Vorsichtig formulieren ('kann', 'könnte'), "
    "keine Kursprognose.\n"
    "wer = 1 Satz, was die Firma macht, nur wenn sie nicht allgemein bekannt ist und du sicher bist, ohne Zahlen. "
    "Sonst leere Zeichenkette.\n"
    "'billion' heißt Milliarde (Mrd.), 'trillion' heißt Billion.")


def _deep_budget_left():
    return AI_DEEP_BUDGET - _ai["deep_spent"]


def ai_deep(c):
    """Eilmeldungs-Text für EIN Thema: {urgent, head, was, warum, wer} oder None (-> Regel-Text als Ersatz)."""
    if not AI or _ai["dead"] or not ai_ready() or _deep_budget_left() < 10:
        return None
    t0 = time.time()
    heads = [f"- {it['title']} ({it.get('pub', '')})" for it in
             sorted(c["items"], key=lambda x: -(x.get("ts") or 0))[:5]]
    texts = [it["desc"] for it in c["items"] if it.get("desc")][:2]
    msg = (f"Firma: {display(c['company']) if c['company'] else '(nicht angegeben, aus den Schlagzeilen entnehmen)'}\nKategorie: {c['cat']}\nSchlagzeilen:\n"
           + "\n".join(heads) + ("\nTexte:\n" + "\n".join(f"- {t}" for t in texts) if texts else ""))
    try:
        resp = _ollama_post("/api/chat", {
            "model": AI_MODEL_URGENT, "stream": False, "format": DEEP_SCHEMA, "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 450},
            "messages": [{"role": "system", "content": DEEP_SYSTEM}, {"role": "user", "content": msg}]},
            min(_deep_budget_left(), AI_CALL_TIMEOUT))
        d = json.loads(resp["message"]["content"])
        out = {"urgent": d["urgent"], "head": _ai_clean(d["head"]), "was": _ai_clean(d["was"]),
               "warum": _ai_clean(d["warum"]), "wer": _ai_clean(d.get("wer"))}
    except Exception as e:
        print(f"WARN KI-Eilmeldung fehlgeschlagen ({type(e).__name__}) -> Regel-Text", file=sys.stderr)
        _ai["dead"] = True
        return None
    finally:
        dt = time.time() - t0
        _ai["deep_spent"] += dt
        _ai["spent"] += dt
    src = _ai_source(c)
    ok = (isinstance(out["urgent"], bool) and 10 <= len(out["head"]) <= 120 and 20 <= len(out["was"]) <= 450
          and 10 <= len(out["warum"]) <= 340 and not _looks_english(out["was"], c)
          and _nums_ok(out["head"] + " " + out["was"] + " " + out["warum"], src)
          and not re.search(r"\d", out["wer"]) and len(out["wer"]) <= 160)
    if not ok:
        print("WARN KI-Eilmeldung verworfen (Format/Zahlen passen nicht zur Quelle) -> Regel-Text", file=sys.stderr)
        return None
    _ai["deep"] += 1
    return out


def ai_overview(recs):
    """1-2 Sätze Überblick über die wichtigsten Zeilen der Zusammenfassung ('' bei jedem Problem)."""
    if not AI or len(recs) < 3 or _ai["dead"] or not ai_ready() or _deep_budget_left() < 10:
        return ""
    src = "\n".join(f"- {r['name'] or 'Markt'}: {r['text']}" for r in recs[:8])
    t0 = time.time()
    try:
        resp = _ollama_post("/api/chat", {
            "model": AI_MODEL_URGENT, "stream": False, "keep_alive": "10m",
            "format": {"type": "object", "properties": {"ueberblick": {"type": "string"}}, "required": ["ueberblick"]},
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 150},
            "messages": [{"role": "system", "content": "Du fasst Börsennachrichten für einen Privatanleger zusammen. "
                          "Schreibe höchstens 2 kurze Sätze in einfachem Deutsch: was war insgesamt los? Nutze nur die "
                          "Liste, erfinde nichts, keine Kursprognose. Antworte als JSON {\"ueberblick\": \"...\"}."},
                         {"role": "user", "content": src}]}, min(_deep_budget_left(), AI_CALL_TIMEOUT))
        out = _ai_clean(json.loads(resp["message"]["content"]).get("ueberblick"))
    except Exception as e:
        print(f"WARN KI-Überblick fehlgeschlagen ({type(e).__name__})", file=sys.stderr)
        return ""
    finally:
        dt = time.time() - t0
        _ai["deep_spent"] += dt
        _ai["spent"] += dt
    if not (20 <= len(out) <= 340) or not _nums_ok(out, src) or len(re.findall(r"\b(?:the|and|for|with)\b", out.lower())) >= 2:
        return ""
    return out


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
    nums = len([t for t in a & b if any(ch.isdigit() for ch in t)])
    if names_a & names_b and nums >= 2 and inter >= 4:
        return True  # gleiche Firma und gleiche Summe (z. B. '$7.5B') -> dieselbe Geschichte, nur anders formuliert
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


def topic_raw(c):
    """Englischer Rohtext zum Thema. Bei Klickköder-Titeln wird der Anfang des Artikeltexts genommen."""
    r = c["rep"]
    if r["kind"] in ("sec", "secm"):
        return r["title"]
    txt = r["title"]
    if is_teaser(txt) and r.get("desc"):
        txt = lead_sentence(r["desc"]) or txt
    return txt


def ensure_german(cands):
    """Sorgt für eine deutsche Zeile pro Thema: KI-Zeile > KI-Übersetzung > Google > englischer Originaltitel."""
    cands = [c for c in cands if "text_de" not in c]
    ai_translate(cands)
    todo = [c for c in cands if "text_de" not in c and c["rep"]["kind"] not in ("sec", "secm")]
    for c, de in zip(todo, translate_many([topic_raw(c) for c in todo])):
        c["text_de"] = de
    for c in cands:
        c.setdefault("text_de", c["rep"]["title"])


def src_names(c):
    s = ", ".join(c["pubs"][:3]) + ("…" if len(c["pubs"]) > 3 else "")
    return s + (f" (+{c['n'] - 1} weitere Meldungen)" if c["n"] > 1 else "")


def is_urgent(c, state, now):
    """Dringend = Einzel-Push. Mit KI: Bewertung 5. Ohne KI (und bei SEC-Meldungen): hohe Regel-Punktzahl.
    Folgemeldungen zu einer schon gesendeten Eilmeldung kommen nur noch in die Zusammenfassung."""
    r, w = c["rep"], c.get("w")
    ok = c["score"] >= URGENT_MIN if (w is None or r["kind"] in ("sec", "secm")) else w >= 5
    if not ok:
        return False
    tok = tokens(r["title"]) if r["kind"] == "news" else set()
    for l in state.get("urgent_log", []):
        if now - l["ts"] <= 86400 and l["co"] == c["company"] and tok and similar(tok, set(l["tok"])):
            return False
    return True


def send_urgent(c):
    """Eine Eilmeldung. Rückgabe: True = gesendet, False = Fehler (wird erneut versucht), None = 'doch nicht dringend'."""
    r = c["rep"]
    deep = ai_deep(c) if r["kind"] == "news" else None
    if deep and not deep["urgent"]:
        return None
    name = display(c["company"]).upper() if c["company"] else ""
    if deep:
        head = deep["head"] if (not name or name.lower() in deep["head"].lower()) else f"{name}: {deep['head']}"
        body = [deep["was"], "", "Warum wichtig: " + deep["warum"]] + (["", deep["wer"]] if deep["wer"] else [])
        title = short("(Wichtig) 🔴 " + head, 110)
    else:
        ensure_german([c])
        title = short("(Wichtig) 🔴 " + (f"{name}: " if name else "") + c["text_de"], 110)
        body = [c["text_de"]]
        if r["kind"] in ("sec", "secm"):
            body += ["", "Offizielle Pflichtmeldung bei der US-Börsenaufsicht SEC. Die Details stehen im verlinkten Dokument."]
        if c.get("who"):
            body += ["", c["who"]]
    body += ["", "Quelle: " + src_names(c)]
    links = list(dict.fromkeys(it["link"] for it in sorted(c["items"], key=lambda x: -(x.get("ts") or 0))))[:3]
    actions = [{"action": "view", "label": f"Quelle {k + 1}", "url": u} for k, u in enumerate(links)]
    return send(title, "\n".join(body), r["link"], tags=["rotating_light"], priority=4, actions=actions)


def to_record(c, now):
    """Thema -> kleiner Eintrag für die Zusammenfassung (wird in state.json gesammelt)."""
    return {"name": display(c["company"]) if c["company"] else "", "cat": c["cat"], "w": c.get("w"), "sc": c["score"],
            "text": c["text_de"], "who": c.get("who", ""), "mkt": bool(c.get("mkt")), "link": c["rep"]["link"],
            "pubs": ", ".join(c["pubs"][:3]) + ("…" if len(c["pubs"]) > 3 else "") + (f" +{c['n'] - 1}" if c["n"] > 1 else ""),
            "ts": c["rep"]["ts"] or now, "t": now}


def rec_rank(r):
    w = r["w"]
    return w if w is not None else (4 if r["sc"] >= IMPORTANT_MIN else 2)


def rec_line(r, with_name=False):
    mark = "🟠 " if rec_rank(r) >= 4 else ""
    cat = "" if r["cat"] == "SEC" else f"{r['cat']}: "
    nm = f"{r['name'].upper()} – " if with_name and r["name"] else ""
    s = f"• {mark}{nm}{cat}{r['text']}"
    if r.get("who"):
        s += f" [{r['who']}]"
    return s + (f" ({r['pubs']})" if r["pubs"] else "")


def maybe_digest(state, now):
    """Sendet die gesammelten Meldungen als EINE Zusammenfassung (höchstens alle DIGEST_H Stunden)."""
    pend = state["pending"]
    pend[:] = [r for r in pend if now - r["t"] < 36 * 3600]
    if not pend:
        return False
    if now - (state.get("last_digest") or now) < DIGEST_H * 3600 and len(pend) < DIGEST_FORCE:
        return False
    pend.sort(key=lambda r: (-rec_rank(r), -r["sc"], -r["ts"]))
    chosen, size = [], 700
    for r in pend:
        b = len(rec_line(r, True).encode("utf-8")) + 1
        if len(chosen) >= DIGEST_MAX_LINES or size + b > 3500:
            break
        chosen.append(r)
        size += b
    ov = ai_overview(chosen)
    lines = ([f"Überblick: {ov}", ""] if ov else [])
    for mkt, head in ((False, "DEINE AKTIEN"), (True, "MARKT (nicht auf deiner Liste)")):
        grp = [r for r in chosen if r["mkt"] == mkt]
        if not grp:
            continue
        lines.append(head)
        if mkt:
            lines += [rec_line(r, True) for r in grp]
        else:
            for nm in dict.fromkeys(r["name"] for r in grp):
                lines.append(nm.upper())
                lines += [rec_line(r) for r in grp if r["name"] == nm]
        lines.append("")
    left = len(pend) - len(chosen)
    if left:
        lines.append(f"… und {left} weitere Meldungen (kommen in der nächsten Zusammenfassung)")
    if any(rec_rank(r) >= 4 for r in chosen):
        lines += ["", "🟠 = wichtig, aber nicht dringend"]
    top = chosen[0]
    title = short(f"(Zusammenfassung) {len(chosen)} Meldungen – " + (f"{top['name'].upper()}: " if top["name"] else "")
                  + top["text"], 110)
    actions = [{"action": "view", "label": f"Meldung {k + 1}", "url": r["link"]} for k, r in enumerate(chosen[:3])]
    if send(title, "\n".join(lines).strip(), top["link"], tags=["newspaper"], priority=2, actions=actions):
        ids = {id(r) for r in chosen}
        pend[:] = [r for r in pend if id(r) not in ids]
        state["last_digest"] = now
        return True
    return False


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
                c["cooled"] = True


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

    market = []
    if MARKET and time.time() - START < TIME_BUDGET:
        try:
            market = prepare_market(state, wl, queue, offtopic, now)
            apply_cooldown(state, market, now)
        except Exception as e:  # der Markt-Teil darf den Rest nie stören
            print(f"WARN Markt-Teil fehlgeschlagen: {type(e).__name__}: {e}", file=sys.stderr)
    for c in market:
        c["mkt"] = True

    # Optionale lokale KI: bewertet Watchlist- und Markt-Themen in einem Rutsch (Fehler -> weiter mit Regeln)
    try:
        ai_rate(topics + market)
        topics, market = ai_drop(state, topics, now), ai_drop(state, market, now)
    except Exception as e:
        print(f"WARN KI-Schicht fehlgeschlagen: {type(e).__name__}: {e}", file=sys.stderr)
    cands = topics + market
    ai_cap(cands, MAX_URGENT)

    # 1) Dringendes: je eine eigene Push mit verständlichem Text
    urg = sorted([c for c in cands if is_urgent(c, state, now)],
                 key=lambda c: (-rank(c), -c["score"], -(c["rep"]["ts"] or 0)))
    urgent, overflow = urg[:MAX_URGENT], urg[MAX_URGENT:]
    ids = {id(c) for c in urgent}
    rest = [c for c in cands if id(c) not in ids]  # inkl. Überzähliger: nichts geht verloren
    n_urgent = 0
    for c in urgent:
        res = send_urgent(c)
        if res is True:
            n_urgent += 1
            for it in c["items"]:
                state["seen"][it["id"]] = now
            if c["company"] and c["cat"] not in _NO_MERGE:
                state["topics"][f"{c['company']}|{c['cat']}"] = now
            state["urgent_log"].append({"ts": now, "co": c["company"],
                                        "tok": sorted(tokens(c["rep"]["title"]))[:30] if c["rep"]["kind"] == "news" else []})
        elif res is None:
            rest.append(c)  # KI: doch nicht dringend -> Zusammenfassung
        # False: Push gescheitert -> nicht als gesehen markieren, nächster Lauf versucht es erneut

    # 2) Alles andere: deutsche Zeile sicherstellen und für die Zusammenfassung sammeln
    ensure_german(rest)
    state.setdefault("last_digest", now)
    for c in sorted(rest, key=lambda c: (-rank(c), -c["score"])):
        state["pending"].append(to_record(c, now))
        for it in c["items"]:
            state["seen"][it["id"]] = now
    sent_digest = maybe_digest(state, now)

    print(f"Fertig: {len(queue)} neue Watchlist-Meldungen -> {n_urgent} dringend gesendet, {len(rest)} in die "
          f"Zusammenfassung (gesammelt: {len(state['pending'])}, gesendet: {'ja' if sent_digest else 'nein'}); "
          f"KI: {_ai['rated']} bewertet, {_ai['deep']} Eilmeldungstexte, {_ai['dropped']} aussortiert. "
          f"Watchlist: {', '.join(wl) or '-'}")
    save_state(state)


if __name__ == "__main__":
    main()
