import re
from functools import lru_cache
from urllib.parse import urlparse, urlunparse

import httpx
import requests
from fastapi import FastAPI, HTTPException
from bs4 import BeautifulSoup

URL = "https://stats.swehockey.se/ScheduleAndResults/Schedule/18266"
MODO_TAG = "modo"  # används för case-insensitive matchning

THESPORTSDB_API_KEY = "123"  # byt till egen nyckel om du har
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json"

app = FastAPI()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
SCORE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


def empty_game():
    return {
        "date": "",
        "time": "",
        "home_team": "",
        "away_team": "",
        "home_score": None,
        "away_score": None,
        "venue": "",
    }


def is_date(s: str) -> bool:
    return bool(DATE_RE.match(s))


def is_time(s: str) -> bool:
    return bool(TIME_RE.match(s))


def is_score_line(s: str) -> bool:
    # score ("3 - 2") eller "-" (kommande match)
    return bool(SCORE_RE.match(s)) or s == "-"


def parse_matches_from_lines(lines):
    """
    Läser rad för rad och bygger matcher så här:

    current_date uppdateras när vi ser YYYY-MM-DD.
    När vi ser TIME + TIME startar vi en ny match:
      - läser Game (antingen "A - B" eller "A", "-", "B")
      - läser Result ("X - Y" eller "-")
      - hoppar periodraden om den börjar med "("
      - hoppar publiksiffra om den är heltal
      - nästa rad = arena
    """
    games = []
    current_date = ""
    i = 0
    L = len(lines)

    while i < L:
        s = lines[i]

        # Uppdatera datum när vi ser YYYY-MM-DD
        if is_date(s):
            current_date = s
            i += 1
            continue

        # Start av match: två tidsrader i följd
        if is_time(s) and i + 1 < L and is_time(lines[i + 1]):
            time = s
            i += 2  # hoppa båda tidsraderna

            if i >= L:
                break

            # -------- Game (lag) --------
            # Två varianter:
            # 1) "Lag A - Lag B" på samma rad
            # 2) "Lag A" / "-" / "Lag B" på tre rader
            if " - " in lines[i]:
                home_team, away_team = [p.strip() for p in lines[i].split(" - ", 1)]
                i += 1
            elif i + 2 < L and lines[i + 1] == "-":
                home_team = lines[i]
                away_team = lines[i + 2]
                i += 3
            else:
                # Om vi inte kan tolka lagen hoppar vi vidare
                i += 1
                continue

            # -------- Resultat --------
            if i >= L:
                break

            result_line = lines[i]
            i += 1

            m = SCORE_RE.match(result_line)
            if m:
                home_score = int(m.group(1))
                away_score = int(m.group(2))
            else:
                home_score = None
                away_score = None

            # -------- Periodresultat (valfritt) --------
            if i < L and lines[i].startswith("("):
                i += 1

            # -------- Publik (valfritt) --------
            spectators = None
            if i < L and lines[i].isdigit():
                spectators = int(lines[i])
                i += 1

            # -------- Arena --------
            venue = ""
            if i < L:
                venue = lines[i]
                i += 1

            games.append(
                {
                    "date": current_date,
                    "time": time,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_score": home_score,
                    "away_score": away_score,
                    "venue": venue,
                    "spectators": spectators,
                }
            )

            continue

        # Ingen match, bara vidare
        i += 1

    return games


def is_modo_game(game) -> bool:
    return (
        MODO_TAG in game["home_team"].lower()
        or MODO_TAG in game["away_team"].lower()
    )


def compute_modo_result(game) -> str:
    """
    Returnerar:
      - "win"  om MODO vann
      - "loss" om MODO förlorade
      - "draw" vid oavgjort
      - "" om ingen MODO i matchen eller inget resultat ännu
    """
    hs = game.get("home_score")
    as_ = game.get("away_score")
    if hs is None or as_ is None:
        return ""

    home_name = (game.get("home_team") or "").lower()
    away_name = (game.get("away_team") or "").lower()
    modo_home = MODO_TAG in home_name
    modo_away = MODO_TAG in away_name

    if not (modo_home or modo_away):
        return ""

    if hs == as_:
        return "draw"

    modo_won = (modo_home and hs > as_) or (modo_away and as_ > hs)
    return "win" if modo_won else "loss"


def normalize_badge_url(url: str | None) -> str | None:
    """
    Fixar badge-URL så att den alltid pekar på r2.thesportsdb.com.
    Returnerar None om url är tom eller ogiltig.
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return url

    if "thesportsdb.com" not in (parsed.netloc or ""):
        return url

    # Tvinga CDN-domänen
    netloc = "r2.thesportsdb.com"
    normalized = parsed._replace(netloc=netloc)
    return urlunparse(normalized)


@lru_cache(maxsize=128)
def get_team_badge(team_name: str) -> str | None:
    """
    Hämtar lagets logga (badge) från TheSportsDB och cachar resultatet.
    """
    if not team_name:
        return None

    # Enkel specialhantering för MoDo
    query = team_name
    if "modo" in team_name.lower():
        query = "Modo"

    url = f"{THESPORTSDB_BASE}/{THESPORTSDB_API_KEY}/searchteams.php"
    try:
        resp = requests.get(url, params={"t": query}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[TheSportsDB] Error fetching badge for {team_name}: {e}")
        return None

    teams = data.get("teams") or []
    if not teams:
        return None

    team = teams[0]
    badge = team.get("strBadge") or team.get("strTeamBadge")
    return normalize_badge_url(badge)


def attach_badges(game):
    """
    Lägger till home_badge och away_badge i game-dict.
    """
    if not game:
        return game

    home = game.get("home_team") or ""
    away = game.get("away_team") or ""

    game["home_badge"] = get_team_badge(home) if home else None
    game["away_badge"] = get_team_badge(away) if away else None
    return game


@app.get("/modo")
async def modo_endpoint():
    # 1. Hämta HTML
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(URL, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html = r.text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch schedule: {e}")

    # 2. Plocka ut ren text & rader
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n").replace("\u00a0", " ")
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 3. Parsea alla matcher
    all_games = parse_matches_from_lines(lines)

    # 4. Filtrera ut MODO-matcher
    modo_games = [g for g in all_games if is_modo_game(g)]

    # 5. Dela upp i spelade & kommande
    played = [g for g in modo_games if g["home_score"] is not None]
    upcoming = [g for g in modo_games if g["home_score"] is None]

    last_game = played[-1] if played else empty_game()
    next_game = upcoming[0] if upcoming else empty_game()

    # 6. Berika med loggor
    last_game = attach_badges(last_game)
    next_game = attach_badges(next_game)

    # 7. Räkna ut MODO-resultat för senaste matchen
    modo_result = compute_modo_result(last_game)

    # 8. Returnera format som din custom-api förväntar sig
    return {
        "last_game": {
            "date": last_game["date"],
            "time": last_game["time"],
            "home_team": last_game["home_team"],
            "away_team": last_game["away_team"],
            "home_score": last_game["home_score"],
            "away_score": last_game["away_score"],
            "venue": last_game["venue"],
            "home_badge": last_game.get("home_badge"),
            "away_badge": last_game.get("away_badge"),
            "modo_result": modo_result,
        },
        "next_game": {
            "date": next_game["date"],
            "time": next_game["time"],
            "home_team": next_game["home_team"],
            "away_team": next_game["away_team"],
            "home_score": next_game["home_score"],
            "away_score": next_game["away_score"],
            "venue": next_game["venue"],
            "home_badge": next_game.get("home_badge"),
            "away_badge": next_game.get("away_badge"),
        },
    }
