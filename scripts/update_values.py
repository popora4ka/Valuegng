#!/usr/bin/env python3
"""Sync MM2 values from supremevalues.com into prices.json.

Output format (read by mm2_prices.lua):
    {"Item Name": {"godly_chroma": 340}, "Other": {"legendary": "x4 T1 Legendaries"}, ...}

Respects robots.txt: if the site disallows this user agent, the script exits
without touching prices.json.

Usage:
    python scripts/update_values.py            # normal run
    python scripts/update_values.py --dump     # also save raw HTML to debug/
"""
import argparse
import json
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://supremevalues.com"
UA = "MM2ValuesSync/1.0 (personal GitHub Action; honors robots.txt)"
OUT = Path(__file__).resolve().parent.parent / "prices.json"
DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug"

# Category pages are also auto-discovered from /mm2/values.
SEED_PATHS = [
    "/mm2/ancients", "/mm2/godlies", "/mm2/chromas", "/mm2/legendaries",
    "/mm2/rares", "/mm2/uncommons", "/mm2/sets",
]
DISCOVERY_PATH = "/mm2/values"  # trending page; used only to find links
SETS_PATH = "/mm2/sets"

# Values like "x4 T1 Legendaries" and "Priceless" are kept as text, as on the site.
CHROMAS_PATH = "/mm2/chromas"
# Site category -> rarity key used by the Lua script (colors of the item frame).
# Chromas share the godly frame color, so both map to "godly_chroma".
RARITY_BY_PAGE = {
    "godlies": "godly_chroma", "chromas": "godly_chroma", "ancients": "ancient_evo",
    "vintages": "vintage", "legendaries": "legendary", "rares": "rare",
    "uncommons": "uncommon", "commons": "common",
}

MIN_ITEMS = 300  # safety net: never overwrite prices.json with a broken scrape
DELAY = 2.0      # seconds between requests

ITEM_RE = re.compile(
    r"^(?P<name>.+?)\s+Value\s*-\s*"
    r"(?P<value>Priceless|x\d+\s+T\d+\s+\w+|[\d,]+(?:\.\d+)?\s*[KkMm]?)\s+"
    r"(?:Range\s*-\s*\[(?P<range>[^\]]*)\]\s+)?"
    r"Stability\s*-\s*(?P<stab>.+?)\s+Item Stability",
    re.I,
)


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def clean_name(raw: str) -> str:
    n = raw.split("·")[-1].strip()
    n = re.sub(r"^Item Set\s+", "", n)
    w = n.split()
    h = len(w) // 2
    if len(w) % 2 == 0 and w[:h] == w[h:]:  # image alt + label duplicated
        n = " ".join(w[:h])
    return n


def name_keys(n: str):
    """Keys the Lua script may look up for one item (it strips a trailing
    "(...)" from in-game labels, so the stored keys have none)."""
    n = norm_ws(re.sub(r"^C\.\s+", "Chroma ", n))
    keys = set()
    base = norm_ws(re.sub(r"\s*\([^)]*\)\s*$", "", n))
    keys.add(base)
    m = re.search(r"\((Gun|Knife)\)\s*$", n)
    if m:  # also "Cursed Knife" style
        keys.add(norm_ws(base + " " + m.group(1)))
    for k in list(keys):
        keys.add(k.replace("'", "").replace("’", ""))
    return {k for k in keys if k}


def parse_value(tok: str):
    t = tok.strip()
    if t.lower() == "priceless":
        return "Priceless"
    if re.fullmatch(r"x\d+\s+T\d+\s+\w+", t, re.I):
        return norm_ws(t)  # text label, e.g. "x4 T1 Legendaries"
    m = re.fullmatch(r"([\d,]+(?:\.\d+)?)\s*([KkMm]?)", t)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    v *= {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]
    return int(v) if v == int(v) else v


def parse_page(html: str):
    soup = BeautifulSoup(html, "lxml")
    cands = {}
    for tag in soup.find_all(True):
        m = ITEM_RE.match(norm_ws(tag.get_text(" ")))
        if m:
            cands[id(tag)] = (tag, m)
    items = []
    for tag, m in cands.values():
        # keep only the innermost matching element (a single item card)
        if any(id(c) in cands for c in tag.find_all(True)):
            continue
        items.append(m)
    return items


class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.rp = urllib.robotparser.RobotFileParser()
        r = self.s.get(BASE + "/robots.txt", timeout=30)
        if r.status_code in (401, 403):
            self.rp.disallow_all = True
        elif r.status_code >= 400:
            self.rp.allow_all = True
        else:
            self.rp.parse(r.text.splitlines())
        self.last = 0.0

    def allowed(self, url):
        return self.rp.can_fetch(UA, url)

    def get(self, path):
        url = urljoin(BASE, path)
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows fetching {url}")
        wait = DELAY - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(url, timeout=30)
        self.last = time.time()
        r.raise_for_status()
        return r.text


def discover(f: Fetcher):
    paths = list(SEED_PATHS)
    try:
        html = f.get(DISCOVERY_PATH)
    except PermissionError:
        raise
    except Exception as e:  # discovery is best-effort
        print(f"discovery failed: {e}", file=sys.stderr)
        return paths
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        h = a["href"].split("#")[0].split("?")[0].replace(BASE, "").rstrip("/")
        if re.fullmatch(r"/mm2/[a-z0-9-]+", h) and h != DISCOVERY_PATH and h not in paths:
            paths.append(h)
    return paths


def better(new, old):
    """Collision rule: a number beats text; otherwise the larger number wins."""
    if old is None:
        return True
    if isinstance(new, str):
        return False
    if isinstance(old, str):
        return True
    return new > old


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="save raw HTML to debug/")
    args = ap.parse_args()

    f = Fetcher()
    prices = {}  # name -> {rarity: value}
    count = 0
    try:
        paths = discover(f)
        for p in paths:
            page = p.strip("/").split("/")[-1]
            if p == SETS_PATH:
                continue  # sets are not inventory items
            rarity = RARITY_BY_PAGE.get(page, "unknown")
            try:
                html = f.get(p)
            except PermissionError:
                raise
            except Exception as e:
                print(f"skip {p}: {e}", file=sys.stderr)
                continue
            if args.dump:
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / (p.strip("/").replace("/", "_") + ".html")).write_text(html, encoding="utf-8")
            found = parse_page(html)
            print(f"{p}: {len(found)} items -> {rarity}")
            for m in found:
                raw = clean_name(m["name"])
                val = parse_value(m["value"])
                if not raw or val is None:
                    print(f"  skipped {m['name']!r} value={m['value']!r}", file=sys.stderr)
                    continue
                if p == CHROMAS_PATH and not re.match(r"(chroma|c\.)\s", raw, re.I):
                    raw = "Chroma " + raw
                count += 1
                for key in name_keys(raw):
                    slot = prices.setdefault(key, {})
                    if better(val, slot.get(rarity)):
                        slot[rarity] = val
    except PermissionError as e:
        print(f"ERROR: {e}. prices.json left untouched.", file=sys.stderr)
        return 2

    if count < MIN_ITEMS:
        print(f"ERROR: only {count} items parsed (< {MIN_ITEMS}); site layout "
              f"probably changed. prices.json left untouched. Run with --dump.", file=sys.stderr)
        return 1

    data = dict(sorted(prices.items(), key=lambda kv: kv[0].lower()))
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == text:
        print("No changes.")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {count} items ({len(data)} keys).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
