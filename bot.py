#!/usr/bin/env python3
"""Aktien-News-Alarm: Yahoo Finance + Google News (RSS) + SEC-8-K  ->  Push per ntfy.sh.

Kein Account und keine Telefonnummer nötig. Nur Python-Standardbibliothek.
Die Watchlist steht in watchlist.txt und wird bei jedem Lauf neu gelesen.
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
MAX_AGE_H = int(os.environ.get("MAX_AGE_HOURS") or "12")      # ältere Meldungen ignorieren
MAX_PUSH = int(os.environ.get("MAX_PUSH_PER_RUN") or "20")    # Schutz vor Spam
DRY = not NTFY_TOPIC                                          # ohne Thema: nur ausgeben

BROWSER_UA = "Mozilla/5.0 (compatible; stock-alerts-bot/1.0)"
SEEN_TTL = 7 * 86400
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,11}$")

# Bedeutung der 8-K-Item-Nummern (nur die, die für Anleger interessant sind)
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

def send(title, message, link=None, tags=None, priority=3):
    """Schickt eine Push-Nachricht über ntfy. Gibt True zurück, wenn sie angenommen wurde."""
    if DRY:
        print(f"--- PUSH ---\n{title}\n{message}\n{link or ''}\n")
        return True
    body = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority}
    if link:
        body["click"] = link  # Tippen auf die Push öffnet den Artikel
    if tags:
        body["tags"] = tags
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
    """Titel normalisieren, damit dieselbe Meldung aus zwei Quellen nur einmal kommt."""
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)  # Google hängt " - Quelle" an
    return re.sub(r"\W+", " ", t.lower()).strip()[:90]


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
            out.append({**i, "id": f"{ticker}|{norm(i['title'])}", "kind": "news", "src": "Yahoo Finance"})

    query = f'"{short_name(info[1])}" stock' if info else f"{ticker} stock"
    raw = http_get("https://news.google.com/rss/search?q="
                   + urllib.parse.quote(query + " when:1d") + "&hl=en-US&gl=US&ceid=US:en")
    if raw is not None:
        ok += 1
        for i in parse_rss(raw):
            out.append({**i, "id": f"{ticker}|{norm(i['title'])}", "kind": "news", "src": "Google News"})

    if info:
        filings = sec_filings(ticker, info[0])
        if filings is not None:
            ok += 1
            out.extend(filings)
    return out, ok


# ---------------------------------------------------------------- Hauptprogramm

def push_item(ticker, it):
    if it["kind"] == "sec":
        return send(f"{ticker} · SEC-Meldung", it["title"], it["link"],
                    tags=["rotating_light"], priority=4)
    return send(f"{ticker} · {it['src']}", it["title"], it["link"],
                tags=["newspaper"], priority=3)


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

    queue.sort(key=lambda x: x[1]["ts"] or now)
    for t, it in queue[:MAX_PUSH]:
        if push_item(t, it):
            state["seen"][it["id"]] = now
            if not DRY:
                time.sleep(0.5)  # nicht zu schnell hintereinander senden
    rest = queue[MAX_PUSH:]
    if rest:
        send("Weitere Meldungen übersprungen",
             f"{len(rest)} weitere Meldungen (Limit pro Lauf: {MAX_PUSH}).", tags=["information_source"])
        for _, it in rest:
            state["seen"][it["id"]] = now

    print(f"Fertig: {len(queue)} neue Meldungen, Watchlist: {', '.join(wl) or '-'}")
    save_state(state)


if __name__ == "__main__":
    main()
