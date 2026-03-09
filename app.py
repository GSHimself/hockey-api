import os
import re
import asyncio
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlparse, urlunparse

import httpx
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException

TEAM_TAG = os.getenv("TEAM_TAG", "modo").lower()

# Swehockey-schema (säsong/serie 2025-2026)
DEFAULT_SCHEDULE_URL = "https://stats.swehockey.se/ScheduleAndResults/Schedule/18266"


def parse_schedule_urls() -> list[str]:
    """
    Läser en eller flera schema-URL:er från miljövariabler.

    Prioritet:
      1) SCHEDULE_URLS (kommaseparerad/semikolon/ny rad)
      2) SCHEDULE_URL (bakåtkompatibel)
      3) default-URL
    """
    schedule_urls_raw = os.getenv("SCHEDULE_URLS", "").strip()
    if schedule_urls_raw:
        parts = re.split(r"[,;\n]+", schedule_urls_raw)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    schedule_url = os.getenv("SCHEDULE_URL", "").strip()
    if schedule_url:
        return [schedule_url]

    return [DEFAULT_SCHEDULE_URL]


SCHEDULE_URLS = parse_schedule_urls()
SCHEDULE_FETCH_RETRIES = max(1, int(os.getenv("SCHEDULE_FETCH_RETRIES", "2")))
SCHEDULE_FETCH_BACKOFF_SECONDS = max(
    0.0, float(os.getenv("SCHEDULE_FETCH_BACKOFF_SECONDS", "0.5"))
)

# TheSportsDB
THESPORTSDB_API_KEY = os.getenv("THESPORTSDB_API_KEY", "123")
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
    return bool(SCORE_RE.match(s)) or s == "-"


def parse_matches_from_lines(lines):
    """
    Läser rad för rad och bygger matcher så här (DIN ORIGINELLA LOGIK):

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

        if is_date(s):
            current_date = s
            i += 1
            continue

        if is_time(s) and i + 1 < L and is_time(lines[i + 1]):
            time = s
            i += 2

            if i >= L:
                break

            if " - " in lines[i]:
                home_team, away_team = [p.strip() for p in lines[i].split(" - ", 1)]
                i += 1
            elif i + 2 < L and lines[i + 1] == "-":
                home_team = lines[i]
                away_team = lines[i + 2]
                i += 3
            else:
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


def is_team_game(game) -> bool:
    """
    True om det här är en match för laget vi bryr oss om (TEAM_TAG).
    """
    tag = TEAM_TAG
    return tag in game["home_team"].lower() or tag in game["away_team"].lower()


def compute_team_result(game) -> str:
    """
    Returnerar resultat ur "vårt" lags perspektiv (TEAM_TAG):

      - "win"  om laget vann
      - "loss" om laget förlorade
      - "draw" vid oavgjort
      - "" om laget inte är med i matchen eller inget resultat ännu
    """
    hs = game.get("home_score")
    as_ = game.get("away_score")
    if hs is None or as_ is None:
        return ""

    home_name = (game.get("home_team") or "").lower()
    away_name = (game.get("away_team") or "").lower()
    tag = TEAM_TAG

    team_home = tag in home_name
    team_away = tag in away_name

    if not (team_home or team_away):
        return ""

    if hs == as_:
        return "draw"

    team_won = (team_home and hs > as_) or (team_away and as_ > hs)
    return "win" if team_won else "loss"


def guess_team_name(games) -> str:
    """
    Försöker hitta ett "snyggt" lagnamn (t.ex. "MoDo Hockey")
    baserat på TEAM_TAG och de matcher vi har.
    """
    tag = TEAM_TAG
    for g in games:
        h = g.get("home_team", "")
        a = g.get("away_team", "")
        if tag in h.lower():
            return h
        if tag in a.lower():
            return a
    # fallback: bara returnera taggen
    return tag


def game_datetime_key(game) -> datetime:
    """
    Sorteringsnyckel för matcher, med robust fallback vid saknat format.
    """
    date = game.get("date") or "9999-12-31"
    time = game.get("time") or "23:59"

    if not DATE_RE.match(date):
        date = "9999-12-31"
    if not TIME_RE.match(time):
        time = "23:59"

    return datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")


def parse_games_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n").replace("\u00a0", " ")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return parse_matches_from_lines(lines)


def merge_unique_games(games):
    """
    Slår ihop matcher från flera källor och tar bort dubbletter.
    Om samma match finns flera gånger prioriteras versionen med resultat.
    """
    merged = {}
    for game in games:
        key = (
            game.get("date", ""),
            game.get("time", ""),
            game.get("home_team", ""),
            game.get("away_team", ""),
        )
        existing = merged.get(key)
        if not existing:
            merged[key] = game
            continue

        existing_has_score = (
            existing.get("home_score") is not None and existing.get("away_score") is not None
        )
        game_has_score = game.get("home_score") is not None and game.get("away_score") is not None
        if game_has_score and not existing_has_score:
            merged[key] = game
            continue

        if len((game.get("venue") or "").strip()) > len((existing.get("venue") or "").strip()):
            merged[key] = game

    return list(merged.values())


async def fetch_schedule_html(client: httpx.AsyncClient, url: str) -> str | None:
    """
    Hämtar schema-HTML med enkel retry/backoff.
    Returnerar None vid permanent fel för att möjliggöra partial success.
    """
    for attempt in range(1, SCHEDULE_FETCH_RETRIES + 1):
        try:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            return response.text
        except Exception as e:
            if attempt >= SCHEDULE_FETCH_RETRIES:
                print(f"[Schedule] Failed to fetch {url} after {attempt} attempts: {e}")
                return None
            await asyncio.sleep(SCHEDULE_FETCH_BACKOFF_SECONDS * attempt)


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


async def attach_badges_async(game):
    """
    Async-wrapper som undviker att blockera event loopen vid badge-hämtning.
    """
    if not game:
        return game

    home = game.get("home_team") or ""
    away = game.get("away_team") or ""

    home_task = asyncio.to_thread(get_team_badge, home) if home else None
    away_task = asyncio.to_thread(get_team_badge, away) if away else None

    home_badge, away_badge = await asyncio.gather(
        home_task if home_task else asyncio.sleep(0, result=None),
        away_task if away_task else asyncio.sleep(0, result=None),
    )

    game["home_badge"] = home_badge
    game["away_badge"] = away_badge
    return game


@app.get("/team")
async def team_endpoint():
    """
    Generisk endpoint: returnerar senaste & nästa match för laget TEAM_TAG.
    Styrs av miljövariabeln TEAM_TAG och SCHEDULE_URL/SCHEDULE_URLS.
    """
    # 1. Hämta HTML för alla konfigurerade scheman
    all_games = []
    timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        html_pages = await asyncio.gather(
            *[fetch_schedule_html(client, url) for url in SCHEDULE_URLS]
        )

    for html in html_pages:
        if not html:
            continue
        all_games.extend(parse_games_from_html(html))

    if not all_games:
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch schedule(s): all configured schedule URLs failed",
        )

    all_games = merge_unique_games(all_games)

    # 2. Filtrera ut matcher för TEAM_TAG
    team_games = [g for g in all_games if is_team_game(g)]
    dated_team_games = sorted(((game_datetime_key(g), g) for g in team_games), key=lambda x: x[0])

    # 3. Dela upp i spelade & kommande
    now = datetime.now()
    played = [
        g
        for _, g in dated_team_games
        if g["home_score"] is not None and g["away_score"] is not None
    ]
    upcoming = [g for dt, g in dated_team_games if g["home_score"] is None and dt >= now]
    if not upcoming:
        upcoming = [g for _, g in dated_team_games if g["home_score"] is None]

    last_game = played[-1] if played else empty_game()
    next_game = upcoming[0] if upcoming else empty_game()

    # 4. Loggor
    last_game, next_game = await asyncio.gather(
        attach_badges_async(last_game),
        attach_badges_async(next_game),
    )

    # 5. Räkna ut resultat ur lagets perspektiv
    team_result = compute_team_result(last_game)

    # 6. Gissa ett lagnamn
    team_name = guess_team_name([g for _, g in dated_team_games])

    # 7. Return
    return {
        "team_tag": TEAM_TAG,
        "team_name": team_name,
        "schedule_urls": SCHEDULE_URLS,
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
            "team_result": team_result,
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
