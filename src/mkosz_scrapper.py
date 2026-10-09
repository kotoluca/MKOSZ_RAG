"""MKOSZ meccseredmények és játékosstatisztikák letöltése (webscraping).

    python src/mkosz_scrapper.py --limit 3 --save-html   # próbafuttatás 3 meccsen, a HTML-t is elmenti
    python src/mkosz_scrapper.py                                       # az 26/27 szezon NB I/B piros csoportja - default
    python src/mkosz_scrapper.py --season x2526 x2627 --league hun     # két szezon, csak NB I/A

Kimenet: data/matches_nb1A.csv (meccsenként egy sor), data/player_stats.csv (játékosonként és meccsenként egy sor).
"""
import argparse  
import io
import re
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BASE_URL = "https://mkosz.hu/bajnoksag-musor/{szezon}/{kod}/"  # több szezon és liga is letölthető
DEFAULT_SEASON = "x2627"                                        # Jelenlegi 26/27-es szezon
DEFAULT_LEAGUE = "hun2a"                                        # férfi NB I/B piros csoport
LEAGUES = {                                                     # Ligák kódjai a weboldalon (a href-ben)
    "hun": "ferfi_nb1a",                        # férfi NB I/A
    "whun": "noi_nb1a",                         # női NB I/A
    "hun2a": "ferfi_nb1b_piros",                # férfi NB I/B piros csoport
    "hun2b": "ferfi_nb1b_zold",                 # férfi NB I/B zöld csoport
}

HEADERS = {"User-Agent": "Mozilla/5.0 (egyetemi beadando; adatgyujtes oktatasi celra)"} 

MATCH_RE = re.compile(r"/merkozes/[^/]+/[^/]+/[a-z0-9]+_(\d+)")            # Meccs URL: /merkozes/nb1a/2023-2024/nb1a_12345
SCORE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")                             # Hazai és vendég pontszám: 78 - 65
DATE_RE = re.compile(r"\d{4}\.\s*\w+\s*\d{1,2}\.")                      # Dátum: 2024. szeptember 15.
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")                                # Kezdés időpontja: 18:30

# Az oldal második fejlécsora egy oszloppal el van csúszva (a Közeli csoportnál hiányzik a %),
# ezért az oszlopneveket csak a felső fejléc csoportjaiból és a pozícióból képezzük.
# Az adatok sorrendje csoporton belül: S K %, Lepattanó V T Ö, Labda SZ EL, Fault SA KI, Blokk SA KA.
GROUPS = {
    "kozeli": ["kozeli_sikeres", "kozeli_kiserlet", "kozeli_szazalek"],
    "kozeptav": ["kozeptav_sikeres", "kozeptav_kiserlet", "kozeptav_szazalek"],
    "tavoli": ["tavoli_sikeres", "tavoli_kiserlet", "tavoli_szazalek"],
    "osszes": ["mezony_sikeres", "mezony_kiserlet", "mezony_szazalek"],
    "bunteto": ["bunteto_sikeres", "bunteto_kiserlet", "bunteto_szazalek"],
    "lepattano": ["lepattano_vedo", "lepattano_tamado", "lepattano_osszes"],
    "labda": ["labdaszerzes", "labdaeladas"],
    "fault": ["fault_elkovetett", "fault_kiharcolt"],
    "blokk": ["blokk_adott", "blokk_kapott"],
}
SINGLE = {
    "jatekos": "jatekos", "k": "kezdo", "p": "pont", "gp": "golpassz",
    "val": "teljesitmenyindex", "ido": "perc", "+/-": "plusz_minusz",
}


def slug(text: str) -> str:
    """A szöveges mezők slug formátumra alakítása: kisbetűsítés, ékezetek eltávolítása, szóközök aláhúzásra cserélése."""
    # Ékezetek eltávolítása és csak ASCII (angol) karakterek megtartása
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()

    # Felesleges szóközök levágása, kisbetűsítés, % és pont karakterek cseréje/törlése
    text = text.strip().lower().replace("%", "pct").replace(".", "")

    # A megmaradt szóközök aláhúzásra (_) cserélése Reguláris Kifejezés (Regex) segítségével
    return re.sub(r"\s+", "_", text)


def fetch(session: requests.Session, url: str) -> str:
    """Egy oldal letöltése, a HTML-t szövegként adja vissza."""
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def parse_game_schedule_row(a) -> dict | None:
    """ kinyeri a meccsek adatait a html-ből dict formátumban, ha a sor nem tartalmazza a szükséges adatokat, None-t ad vissza"""
    m = MATCH_RE.search(a["href"])
    score = SCORE_RE.search(a.get_text(" ", strip=True))
    row = a.find_parent("tr")
    if not m or not score or row is None:
        return None

    teams = [t.get("title") or t.get_text(strip=True) for t in row.find_all("a", href=re.compile(r"/csapat/"))]
    cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
    date = next((DATE_RE.search(c).group(0) for c in cells if DATE_RE.search(c)), None)
    tip = next((c for c in cells if TIME_RE.match(c)), None)
    return {
        "meccs_id": m.group(1),
        "url": a["href"] if a["href"].startswith("http") else "https://mkosz.hu" + a["href"],
        "hazai": teams[0] if len(teams) > 0 else None,
        "vendeg": teams[1] if len(teams) > 1 else None,
        "datum": date,
        "kezdes": tip,
        "hazai_pont": int(score.group(1)),
        "vendeg_pont": int(score.group(2)),
    }


def parse_game_schedule(html: str) -> list[dict]:
    """listába gyűjti a meccsek adatait, kiszűrve a duplikált meccseket (azonos meccs_id)"""
    soup = BeautifulSoup(html, "lxml")
    matches, seen = [], set()
    for a in soup.find_all("a", href=MATCH_RE):
        match = parse_game_schedule_row(a)
        if match is None or match["meccs_id"] in seen:
            continue
        seen.add(match["meccs_id"])
        matches.append(match)
    return matches


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Kétszintű fejléc -> olvasható oszlopnevek, a felső szint és a csoporton belüli pozíció alapján."""
    cols, seen = [], {}
    for col in df.columns:
        top = col[0] if isinstance(col, tuple) else col
        key = "" if str(top).startswith("Unnamed") else slug(top)
        idx = seen.get(key, 0)
        seen[key] = idx + 1
        if key in GROUPS and idx < len(GROUPS[key]):
            cols.append(GROUPS[key][idx])
        else:
            cols.append(SINGLE.get(key, key))
    df.columns = cols
    return df.loc[:, df.columns != ""]  # üres (név nélküli) oszlopok eldobása


def parse_box_scores(html: str) -> list[pd.DataFrame]:
    """A meccsoldal játékosstatisztika-táblázatai, a megjelenés sorrendjében (hazai, vendég)."""
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return []
    boxes = []
    for t in tables:
        t = flatten_columns(t)
        if "jatekos" not in t.columns:
            continue
        t = t[~t["jatekos"].astype(str).str.contains("sszesen", na=False)]  # az „Összesen” sor nem játékos
        boxes.append(t)
    return boxes

def scrape_match(session, match: dict, args, html_dir: Path) -> list[pd.DataFrame]:  # ÚJ
    """Letölti egy meccs oldalát, kinyeri a játékosstatisztikát és visszaadja a hazai és vendég csapat statisztikáját DataFrame-ként."""
    html = fetch(session, match["url"])
    if args.save_html:
        (html_dir / f"meccs_{match['meccs_id']}.html").write_text(html, encoding="utf-8")
    boxes = parse_box_scores(html)
    if len(boxes) != 2:
        print(f"  ! {match['meccs_id']}: {len(boxes)} statisztika-táblázatot találtam (2 kellene)")

    result = []
    for team, opponent, box in zip([match["hazai"], match["vendeg"]], [match["vendeg"], match["hazai"]], boxes):
        box = box.copy()
        box.insert(0, "szezon", match["szezon"])
        box.insert(1, "bajnoksag", match["bajnoksag"])
        box.insert(2, "meccs_id", match["meccs_id"])
        box.insert(3, "csapat", team)
        box.insert(4, "ellenfel", opponent)
        result.append(box)
    return result


def scrape_league(session, szezon: str, kod: str, args, html_dir: Path) -> tuple[list[dict], list[pd.DataFrame]]:  # ÚJ
    """Letölti egy szezon és liga meccseinek listáját és minden meccshez letölti a játékosstatisztikát"""
    nev = LEAGUES[kod]
    schedule_html = fetch(session, BASE_URL.format(szezon=szezon, kod=kod))
    if args.save_html:
        (html_dir / f"musor_{szezon}_{kod}.html").write_text(schedule_html, encoding="utf-8")
    matches = parse_game_schedule(schedule_html)
    for m in matches:
        m["szezon"] = szezon
        m["bajnoksag"] = nev
    print(f"{szezon} {nev}: {len(matches)} lejátszott meccs a műsorban")
    if args.limit:
        matches = matches[:args.limit]

    stats = []
    for i, match in enumerate(matches, 1):
        time.sleep(args.delay)
        stats.extend(scrape_match(session, match, args, html_dir))
        print(f"[{i}/{len(matches)}] {match['hazai']} - {match['vendeg']} {match['hazai_pont']}-{match['vendeg_pont']}")
    return matches, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", nargs="*", default=[DEFAULT_SEASON], help="szezonkódok az URL-ből, pl. x2526 x2627")
    ap.add_argument("--league", nargs="*", default=[DEFAULT_LEAGUE], choices=list(LEAGUES),help="bajnokságkódok, alapból a férfi NB I/B piros")
    ap.add_argument("--limit", type=int, default=None, help="csak az első N meccset tölti le")
    ap.add_argument("--delay", type=float, default=1.0, help="várakozás két letöltés között (mp)")
    ap.add_argument("--save-html", action="store_true", help="a letöltött oldalakat data/html/ alá menti")
    args = ap.parse_args()

    html_dir = DATA_DIR / "html"
    if args.save_html:
        html_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    all_matches, all_stats = [], [] 
    for szezon in args.season:
        for kod in args.league:
            scrape_league(session, szezon, kod, args, html_dir)


if __name__ == "__main__":
    main()