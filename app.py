import os
import re
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import lru_cache
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request

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
        parts = re.split(r"[,;\n]+", schedule_url)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    return [DEFAULT_SCHEDULE_URL]


SCHEDULE_URLS = parse_schedule_urls()
SCHEDULE_FETCH_RETRIES = max(1, int(os.getenv("SCHEDULE_FETCH_RETRIES", "2")))
SCHEDULE_FETCH_BACKOFF_SECONDS = max(
    0.0, float(os.getenv("SCHEDULE_FETCH_BACKOFF_SECONDS", "0.5"))
)
SCHEDULE_CACHE_SECONDS = max(0.0, float(os.getenv("SCHEDULE_CACHE_SECONDS", "300")))

# Swehockey anger matchtider i svensk tid, oberoende av podens tidszon
SWEDEN_TZ = ZoneInfo("Europe/Stockholm")

# TheSportsDB
THESPORTSDB_API_KEY = os.getenv("THESPORTSDB_API_KEY", "123")
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json"


def _parse_team_name_overrides() -> dict[str, str]:
    """
    Läser TEAM_NAME_OVERRIDES från miljövariabeln och returnerar en dict.

    Format: kommaseparerade par "Originalnamn=Sökterm", t.ex.:
      TEAM_NAME_OVERRIDES="MoDo Hockey=Modo,Djurgårdens IF=Djurgarden"
    """
    raw = os.getenv("TEAM_NAME_OVERRIDES", "").strip()
    overrides: dict[str, str] = {}
    if not raw:
        return overrides
    for pair in re.split(r"[,;\n]+", raw):
        pair = pair.strip()
        if "=" in pair:
            original, _, replacement = pair.partition("=")
            original = original.strip()
            replacement = replacement.strip()
            if original and replacement:
                overrides[original.lower()] = replacement
    return overrides


TEAM_NAME_OVERRIDES: dict[str, str] = _parse_team_name_overrides()

app = FastAPI()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("hockey-api")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
SCORE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})$")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.perf_counter()
    logger.info("Request started: %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.exception(
            "Request failed: %s %s in %.1fms",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.info(
        "Request completed: %s %s -> %s in %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


def empty_game():
    return {
        "date": "",
        "time": "",
        "home_team": "",
        "away_team": "",
        "home_score": None,
        "away_score": None,
        "venue": "",
        "round_detail": "",
    }


def is_date(s: str) -> bool:
    return bool(DATE_RE.match(s))


def is_time(s: str) -> bool:
    return bool(TIME_RE.match(s))


def is_score_line(s: str) -> bool:
    return bool(SCORE_RE.match(s)) or s == "-"


def parse_datetime_line(s: str) -> tuple[str, str] | None:
    m = DATETIME_RE.match(s)
    if not m:
        return None
    return m.group(1), m.group(2)


def looks_like_new_match_start(s: str) -> bool:
    return is_date(s) or is_time(s) or parse_datetime_line(s) is not None


ROUND_DETAIL_RE = re.compile(
    r"^(kvartsfinal|semifinal|final|a?ttondelsfinal|omg[aå]ng)\b",
    re.IGNORECASE,
)


def is_round_detail_line(s: str) -> bool:
    return bool(ROUND_DETAIL_RE.match(s.strip()))


def parse_match_metadata(lines, i: int) -> tuple[str, str, int]:
    """
    Läser metadata-rader efter en matchrad och plockar ut arena + omgångsdetalj.
    """
    metadata_lines = []
    L = len(lines)
    while i < L and not looks_like_new_match_start(lines[i]) and " - " not in lines[i]:
        metadata_lines.append(lines[i])
        i += 1
        if len(metadata_lines) >= 2:
            break

    venue = ""
    round_detail = ""
    if not metadata_lines:
        return venue, round_detail, i

    first = metadata_lines[0]
    second = metadata_lines[1] if len(metadata_lines) > 1 else ""

    if is_round_detail_line(first):
        round_detail = first
        venue = second
    else:
        venue = first
        if second and is_round_detail_line(second):
            round_detail = second

    return venue, round_detail, i


def parse_result_block(lines, i: int) -> tuple[int | None, int | None, int]:
    """
    Läser resultatrad ("X - Y" eller "-") samt valfri periodrad och publiksiffra.
    Förutsätter att lines[i] är en resultatrad.
    """
    home_score = None
    away_score = None
    L = len(lines)

    m = SCORE_RE.match(lines[i])
    if m:
        home_score = int(m.group(1))
        away_score = int(m.group(2))
    i += 1

    # -------- Periodresultat (valfritt) --------
    if i < L and lines[i].startswith("("):
        i += 1

    # -------- Publik (valfritt, används inte) --------
    if i < L and lines[i].isdigit():
        i += 1

    return home_score, away_score, i


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
    (resultat/period/publik läses av parse_result_block)
    """
    games = []
    current_date = ""
    i = 0
    L = len(lines)

    while i < L:
        s = lines[i]

        parsed_datetime = parse_datetime_line(s)

        if is_date(s):
            current_date = s
            i += 1
            continue

        time = None
        if parsed_datetime:
            current_date, time = parsed_datetime
            i += 1
        elif is_time(s) and i + 1 < L and is_time(lines[i + 1]):
            time = s
            i += 2

        if time:

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

            home_score = None
            away_score = None

            venue = ""
            round_detail = ""

            # -------- Resultat (valfritt) --------
            if i < L and is_score_line(lines[i]):
                home_score, away_score, i = parse_result_block(lines, i)

                # -------- Arena --------
                if i < L and not looks_like_new_match_start(lines[i]):
                    venue, round_detail, i = parse_match_metadata(lines, i)
            elif i < L and not looks_like_new_match_start(lines[i]) and " - " not in lines[i]:
                # Slutspelsformat: runda/arena kan komma före eller utan resultat.
                venue, round_detail, i = parse_match_metadata(lines, i)

                # Spelade slutspelsmatcher har resultatet efter metadata.
                if i < L and is_score_line(lines[i]):
                    home_score, away_score, i = parse_result_block(lines, i)

            games.append(
                {
                    "date": current_date,
                    "time": time,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_score": home_score,
                    "away_score": away_score,
                    "venue": venue,
                    "round_detail": round_detail,
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
    text = soup.get_text("\n").replace(" ", " ")
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
        if game_has_score != existing_has_score:
            if game_has_score:
                merged[key] = game
            continue

        if len((game.get("venue") or "").strip()) > len((existing.get("venue") or "").strip()):
            merged[key] = game

    return list(merged.values())


async def fetch_schedule_html(
    client: httpx.AsyncClient, url: str
) -> tuple[str | None, str | None]:
    """
    Hämtar schema-HTML med enkel retry/backoff.
    Returnerar (html, error). Vid permanent fel returneras (None, feltext)
    för att möjliggöra partial success och tydligare felsökning.
    """
    last_error = None
    for attempt in range(1, SCHEDULE_FETCH_RETRIES + 1):
        try:
            logger.debug(
                "Fetching schedule url=%s attempt=%s/%s",
                url,
                attempt,
                SCHEDULE_FETCH_RETRIES,
            )
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            logger.info("Fetched schedule url=%s status=%s", url, response.status_code)
            return response.text, None
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt >= SCHEDULE_FETCH_RETRIES:
                logger.exception(
                    "Failed to fetch schedule url=%s after %s attempts error=%s",
                    url,
                    attempt,
                    last_error,
                )
                return None, last_error
            logger.warning(
                "Fetch failed url=%s attempt=%s/%s error=%s",
                url,
                attempt,
                SCHEDULE_FETCH_RETRIES,
                e,
            )
            await asyncio.sleep(SCHEDULE_FETCH_BACKOFF_SECONDS * attempt)
    return None, last_error


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
def lookup_team_badge(query: str) -> str | None:
    """
    Slår upp badge hos TheSportsDB. Kastar undantag vid nätverks-/HTTP-fel,
    vilket gör att lru_cache bara sparar lyckade svar (även "inget lag hittades")
    och att tillfälliga fel försöks igen vid nästa anrop.
    """
    url = f"{THESPORTSDB_BASE}/{THESPORTSDB_API_KEY}/searchteams.php"
    with httpx.Client(timeout=10) as client:
        resp = client.get(url, params={"t": query})
    resp.raise_for_status()
    data = resp.json()

    teams = data.get("teams") or []
    if not teams:
        logger.debug("No TheSportsDB team found for query=%s", query)
        return None

    team = teams[0]
    logger.debug(
        "TheSportsDB matched query=%s -> team=%s sport=%s",
        query,
        team.get("strTeam"),
        team.get("strSport"),
    )
    badge = team.get("strBadge") or team.get("strTeamBadge")
    return normalize_badge_url(badge)


def get_team_badge(team_name: str) -> str | None:
    """
    Hämtar lagets logga (badge) från TheSportsDB.

    Sökterm kan åsidosättas via TEAM_NAME_OVERRIDES (case-insensitive på originalnamnet).
    """
    if not team_name:
        return None

    query = TEAM_NAME_OVERRIDES.get(team_name.lower(), team_name)
    logger.debug("Fetching badge for team=%s query=%s", team_name, query)
    try:
        return lookup_team_badge(query)
    except Exception:
        logger.exception("Error fetching badge for team=%s", team_name)
        return None


async def attach_badges_async(game):
    """
    Async-wrapper som undviker att blockera event loopen vid badge-hämtning.
    Returnerar en kopia av matchen så att cachade matcher inte muteras.
    """
    game = dict(game)

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


def last_game_too_old(game, now: datetime) -> bool:
    """
    True om senaste matchen spelades för mer än 1 månad sedan.
    Används för att dölja last_game när säsongen är slut.
    """
    date_str = game.get("date")
    if not date_str or not DATE_RE.match(date_str):
        return False
    try:
        game_dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return False
    delta = now - game_dt
    # 31 dagar som approximation för "1 månad"
    return delta.days > 31


def now_in_sweden() -> datetime:
    """
    Aktuell tid i svensk tid, utan tzinfo, så att den kan jämföras med
    matchtiderna från Swehockey (som är naiva svenska tider).
    """
    return datetime.now(SWEDEN_TZ).replace(tzinfo=None)


# Cache för parsade matcher: undviker att skrapa Swehockey vid varje anrop och
# gör att senast lyckade schemat kan serveras om alla källor fallerar.
_schedule_cache: dict = {"games": None, "fetched_at": 0.0}
_schedule_lock = asyncio.Lock()


async def fetch_all_games() -> tuple[list[dict], dict[str, str]]:
    """
    Hämtar och parsar alla SCHEDULE_URLS parallellt.
    Returnerar (matcher, fel per URL).
    """
    timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        fetch_results = await asyncio.gather(
            *[fetch_schedule_html(client, url) for url in SCHEDULE_URLS]
        )

    all_games = []
    fetch_errors = {}
    for url, (html, error) in zip(SCHEDULE_URLS, fetch_results):
        if error:
            fetch_errors[url] = error
        if html:
            all_games.extend(parse_games_from_html(html))

    return merge_unique_games(all_games), fetch_errors


async def load_games() -> tuple[list[dict], list[str]]:
    """
    Returnerar (matcher, varningar) med TTL-cache (SCHEDULE_CACHE_SECONDS).

    - Bara helt lyckade hämtningar cachas, så delfel försöks igen nästa anrop.
    - Om alla källor fallerar serveras senast cachade schema (om det finns).
    """
    async with _schedule_lock:
        cached = _schedule_cache["games"]
        age = time.monotonic() - _schedule_cache["fetched_at"]
        if cached is not None and age < SCHEDULE_CACHE_SECONDS:
            logger.debug("Using cached schedule age=%.0fs", age)
            return cached, []

        games, fetch_errors = await fetch_all_games()
        warnings = [f"Failed to fetch {url}: {err}" for url, err in fetch_errors.items()]

        if games and not fetch_errors:
            _schedule_cache["games"] = games
            _schedule_cache["fetched_at"] = time.monotonic()
            return games, warnings

        if games:
            logger.warning("Partial schedule fetch, errors=%s", fetch_errors)
            return games, warnings

        if cached is not None:
            logger.warning(
                "All schedule fetches failed, serving cached schedule age=%.0fs errors=%s",
                age,
                fetch_errors,
            )
            warnings.append(f"Serving cached schedule from {age:.0f}s ago")
            return cached, warnings

    logger.error(
        "All schedule URL fetches failed for urls=%s errors=%s",
        SCHEDULE_URLS,
        fetch_errors,
    )
    raise HTTPException(
        status_code=500,
        detail={
            "message": "Failed to fetch schedule(s): all configured schedule URLs failed",
            "urls": SCHEDULE_URLS,
            "errors": fetch_errors,
        },
    )


def game_payload(game) -> dict:
    """
    Fälten som exponeras i API-svaret för en match.
    """
    return {
        "date": game["date"],
        "time": game["time"],
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "home_score": game["home_score"],
        "away_score": game["away_score"],
        "venue": game["venue"],
        "round_detail": game.get("round_detail", ""),
        "home_badge": game.get("home_badge"),
        "away_badge": game.get("away_badge"),
    }


@app.get("/team")
async def team_endpoint():
    """
    Generisk endpoint: returnerar senaste & nästa match för laget TEAM_TAG.
    Styrs av miljövariabeln TEAM_TAG och SCHEDULE_URL/SCHEDULE_URLS.
    """
    logger.info("Building /team response for team_tag=%s", TEAM_TAG)
    # 1. Hämta (eller läs cachade) matcher för alla konfigurerade scheman
    all_games, warnings = await load_games()
    logger.info("Parsed total unique games=%s", len(all_games))

    # 2. Filtrera ut matcher för TEAM_TAG
    team_games = [g for g in all_games if is_team_game(g)]
    dated_team_games = sorted(((game_datetime_key(g), g) for g in team_games), key=lambda x: x[0])
    logger.info("Filtered team games=%s", len(team_games))

    # 3. Dela upp i spelade & kommande
    now = now_in_sweden()
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

    # Om senaste matchen är äldre än 1 månad visas den inte (säsongen är förmodligen slut)
    if last_game_too_old(last_game, now):
        logger.info(
            "last_game date=%s is older than 1 month, hiding it", last_game.get("date")
        )
        last_game = empty_game()

    # 4. Hämta badges
    last_game, next_game = await asyncio.gather(
        attach_badges_async(last_game),
        attach_badges_async(next_game),
    )

    # 5. Gissa ett lagnamn
    team_name = guess_team_name([g for _, g in dated_team_games])
    logger.info(
        "Prepared /team response: team_name=%s last_game_date=%s next_game_date=%s",
        team_name,
        last_game.get("date"),
        next_game.get("date"),
    )

    # 6. Return
    return {
        "team_tag": TEAM_TAG,
        "team_name": team_name,
        "schedule_urls": SCHEDULE_URLS,
        "warnings": warnings,
        "last_game": {
            **game_payload(last_game),
            "team_result": compute_team_result(last_game),
        },
        "next_game": game_payload(next_game),
    }


@app.get("/")
async def root_endpoint():
    """
    Enkel health/info endpoint för att undvika 404 på root-path.
    """
    return {
        "status": "ok",
        "endpoints": ["/team"],
        "schedule_urls": SCHEDULE_URLS,
    }
