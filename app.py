import re
from functools import lru_cache
from urllib.parse import urlparse, urlunparse

import httpx
import requests
from fastapi import FastAPI, HTTPException
from bs4 import BeautifulSoup

URL = "https://stats.swehockey.se/ScheduleAndResults/Schedule/18266"
MODO_TAG = "modo"

THESPORTSDB_API_KEY = "123"  # byt till egen nyckel om du har
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json"

app = FastAPI()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
SCORE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")

# Ny regex för hela Game-raden (både spelade & kommande matcher)
GAME_LINE_RE = re.compile(
    r"""^
    (?P<home>.+?)          # hemalag (så kort som möjligt)
    \s+-\s+
    (?P<away>.+?)          # bortalag (så kort som möjligt)
    (?:                    # ev. resultat + perioder + publik (endast spelade matcher)
        \s+
        (?P<hs>\d+)\s*-\s*(?P<as>\d+)   # resultat, t.ex. 3 - 2
        \s+\(.*?\)\s+                   # periodresultat inom parentes
        (?P<spec>\d+)                   # publiksiffra
    )?
    \s+
    (?P<venue>.+)          # arena (alltid sist)
    $""",
    re.VERBOSE,
)


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


def is_time(s: str) -> bool:
    return bool(TIME_RE.match(s))


def parse_matches_from_lines(lines):
    """
    Ny version anpassad till nuvarande Swehockey-layout:

    - Datumrader ser ut som "2025-11-28 2025-11-28" → vi tar första 10 tecknen.
    - För varje tid ("19:00", "20:30", ...) kommer två identiska rader → vi tar en.
    - Direkt efter tiden kommer en "Game-rad":
        * Spelad:
          "MoDo Hockey  -  Östersunds IK 3 - 1 (0-0, 1-1, 2-0) 7298 Hägglunds Arena"
        * Kommande:
          "MoDo Hockey  -  IF Björklöven Hägglunds Arena"

      Vi parsar hela den raden med GAME_LINE_RE och får ut:
        - home_team, away_team
        - ev. home_score, away_score, spectators
        - venue
    """
    games = []
    current_date = ""
    last_time = ""

    i = 0
    L = len(lines)

    while i < L:
        s = lines[i]

        # Datumrad (t.ex. "2025-11-28 2025-11-28")
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            current_date = s[:10]  # ta första datumet
            i += 1
            continue

        # Tidsrad (t.ex. "19:00")
        if is_time(s):
            last_time = s

            # hoppa ev. dublett av samma tid
            if i + 1 < L and lines[i + 1] == s:
                i += 2
            else:
                i += 1

            # nu förväntar vi oss en Game-rad med "Lag A - Lag B ..."
            if i < L and " - " in lines[i]:
                match_line = lines[i]
                i += 1

                m = GAME_LINE_RE.match(match_line)
                if not m:
                    # Om vi inte kan tolka matchraden hoppar vi vidare
                    continue

                home_team = (m.group("home") or "").strip()
                away_team = (m.group("away") or "").strip()
                hs = m.group("hs")
                as_ = m.group("as")
                home_score = int(hs) if hs is not None else None
                away_score = int(as_) if as_ is not None else None
                spectators = m.group("spec")
                spectators = int(spectators) if spectators is not None else None
                venue = (m.group("venue") or "").strip()

                games.append(
                    {
                        "date": current_date,
                        "time": last_time,
                        "home_team": home_team,
                        "away_team": away_team,
                        "home_score": home_score,
                        "away_score": away_score,
                        "venue": venue,
                        "spectators": spectators,
                    }
                )

            continue

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
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return url

    if "thesportsdb.com" not in (parsed.netloc or ""):
        return url

    netloc = "r2.thesportsdb.com"
    normalized = parsed._replace(netloc=netloc)
    return urlunparse(normalized)


@lru_cache(maxsize=128)
def get_team_badge(team_name: str) -> str | None:
    if not team_name:
        return None

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
    if not game:
        return game

    home = game.get("home_team") or ""
    away = game.get("away_team") or ""

    game["home_badge"] = get_team_badge(home) if home else None
    game["away_badge"] = get_team_badge(away) if away else None
    return game


@app.get("/modo")
async def modo_endpoint():
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(URL, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html = r.text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch schedule: {e}")

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n").replace("\u00a0", " ")
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    all_games = parse_matches_from_lines(lines)
    modo_games = [g for g in all_games if is_modo_game(g)]

    played = [g for g in modo_games if g["home_score"] is not None]
    upcoming = [g for g in modo_games if g["home_score"] is None]

    last_game = played[-1] if played else empty_game()
    next_game = upcoming[0] if upcoming else empty_game()

    last_game = attach_badges(last_game)
    next_game = attach_badges(next_game)

    modo_result = compute_modo_result(last_game)

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
