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
MAX_LINES_IMPORTANT = int(os.environ.get("MAX_LINES_IMPORTANT") or "12")
MAX_LINES_OTHER = int(os.environ.get("MAX_LINES_OTHER") or "15")
MAX_TRANSLATE = int(os.environ.get("MAX_TRANSLATE") or "30")         # Übersetzungen pro Lauf
TRANSLATE = (os.environ.get("TRANSLATE") or "1") != "0"              # TRANSLATE=0 schaltet aus
DRY = not NTFY_TOPIC                                                 # ohne Thema: nur ausgeben

BROWSER_UA = "Mozilla/5.0 (compatible; stock-alerts-bot/1.0)"
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
                 r"names? new|returns?|exits?|departs?|departure|retires?|retirement|successor|leaves|leaving)")
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
    (5, [r"\b(?:layoffs?|job cuts|restructuring|plant closure|strike)\b"]),
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


def score_item(it):
    if it["kind"] == "sec":
        return 10 if any(n in SEC_IMPORTANT for n in it.get("nums", [])) else 2
    return score_news(it["title"])


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
    return s


def save_state(state):
    cutoff = time.time() - SEEN_TTL
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
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
    if not TRANSLATE or _tr_fail >= 2 or _tr_count >= MAX_TRANSLATE or not text:
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
        if title and link:
            items.append({"title": title, "link": link, "ts": ts})
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
                        "src": "Google News", "pub": pub or "Google News", "title": clean})

    if info:
        filings = sec_filings(ticker, info[0])
        if filings is not None:
            ok += 1
            out.extend(filings)
    return out, ok


# ---------------------------------------------------------------- Zusammenfassen ähnlicher Meldungen

_STOP = set("the and for with from after over amid into than more new says say said will would could "
            "may can has have had not but about out off its this that these those are was were been "
            "stock stocks shares share".split())


def tokens(title):
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w
            for w in words if len(w) > 2 and w not in _STOP}


def similar(a, b):
    if not a or not b:
        return False
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
        if it["ticker"] not in target["tickers"]:
            target["tickers"].append(it["ticker"])
        if it.get("pub") and it["pub"] not in target["pubs"]:
            target["pubs"].append(it["pub"])
        target["norms"].add(norm(it["title"]))
    return clusters


# ---------------------------------------------------------------- Push-Aufbau

def short(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def tickers_label(c):
    t = ", ".join(c["tickers"][:3])
    return t + ("…" if len(c["tickers"]) > 3 else "")


def line(c):
    s = f"• {tickers_label(c)}: {c['title_de']}"
    meta = []
    if c["pubs"]:
        meta.append(", ".join(c["pubs"][:3]) + ("…" if len(c["pubs"]) > 3 else ""))
    extra = len(c["norms"]) - 1
    if extra > 0:
        meta.append(f"+{extra} ähnliche")
    return s + (f" ({'; '.join(meta)})" if meta else "")


def push_group(label, clusters, max_lines, priority, tags):
    """Sendet eine gebündelte Push für eine Gruppe. True bei Erfolg (oder wenn nichts zu senden ist)."""
    if not clusters:
        return True
    clusters.sort(key=lambda c: (-c["score"], -(c["rep"]["ts"] or 0)))
    shown = clusters[:max_lines]
    for c in shown:  # nur das übersetzen, was auch angezeigt wird
        c["title_de"] = c["rep"]["title"] if c["kind"] == "sec" else translate(c["rep"]["title"])

    n_topics = len(clusters)
    n_items = sum(len(c["norms"]) for c in clusters)
    head = f"{n_topics} Themen" + (f" aus {n_items} Meldungen" if n_items > n_topics else "")
    lines, size = [head, ""], len(head)
    for c in shown:
        ln = line(c)
        if size + len(ln.encode("utf-8")) > 3400:
            break
        lines.append(ln)
        size += len(ln.encode("utf-8")) + 1
    hidden = n_topics - (len(lines) - 2)
    if hidden > 0:
        lines.append(f"… und {hidden} weitere")

    top = shown[0]
    title = short(f"({label}) {tickers_label(top)}: {top['title_de']}", 110)
    actions = [{"action": "view", "label": f"Meldung {i + 1}", "url": c["rep"]["link"]}
               for i, c in enumerate(shown[:3])]
    return send(title, "\n".join(lines), top["rep"]["link"], tags=tags, priority=priority, actions=actions)


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
    queue = []
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
            queue.append((t, it))

    n_imp = n_oth = 0
    if queue:
        clusters = build_clusters(queue)
        important = [c for c in clusters if c["score"] >= IMPORTANT_MIN]
        other = [c for c in clusters if c["score"] < IMPORTANT_MIN]
        n_imp, n_oth = len(important), len(other)
        # Wichtige zuerst (bekommen das Übersetzungs-Kontingent zuerst)
        for label, group, lines, prio, tags in (
            ("Wichtig", important, MAX_LINES_IMPORTANT, 4, ["rotating_light"]),
            ("Unwichtig", other, MAX_LINES_OTHER, 2, ["newspaper"]),
        ):
            if push_group(label, group, lines, prio, tags):
                for c in group:  # auch nicht angezeigte Themen gelten als erledigt
                    for it in c["items"]:
                        state["seen"][it["id"]] = now

    print(f"Fertig: {len(queue)} neue Meldungen -> {n_imp} wichtige, {n_oth} unwichtige Themen. "
          f"Watchlist: {', '.join(wl) or '-'}")
    save_state(state)


if __name__ == "__main__":
    main()
