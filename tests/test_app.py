from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

import app as hockey


def html_from_lines(lines: list[str]) -> str:
    return "<html><body>" + "".join(f"<div>{line}</div>" for line in lines) + "</body></html>"


# Grundserieformat, så som Swehockeys schemasida ser ut efter textextraktion
REGULAR_SEASON_LINES = [
    "2025-09-19",
    "19:00", "19:00",
    "MoDo Hockey", "-", "Östersunds IK",
    "3 - 1", "(0-0, 1-1, 2-0)", "7298",
    "Hägglunds Arena",
    "2025-09-27",
    "16:30", "16:30",
    "Almtuna IS", "-", "MoDo Hockey",
    "2 - 3", "(2-1, 0-1, 0-1)", "1935",
    "Upplandsbilforum Arena",
    "2025-10-10",
    "19:00", "19:00",
    "MoDo Hockey", "-", "Södertälje SK",
    "-",
    "Hägglunds Arena",
    "19:00", "19:00",
    "AIK", "-", "BIK Karlskoga",
    "-",
    "Hovet",
]

# Slutspelsformat: runda före arena, resultat efter metadata
PLAYOFF_LINES = [
    "2026-03-20 19:00",
    "MoDo Hockey - Västerås IK",
    "Kvartsfinal 1",
    "Hägglunds Arena",
    "4 - 2",
    "(1-0, 2-1, 1-1)",
    "6100",
    "2026-03-22 15:00",
    "Västerås IK - MoDo Hockey",
    "Kvartsfinal 2",
    "ABB Arena Nord",
]


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch):
    hockey._schedule_cache.update(games=None, fetched_at=0.0)
    hockey.lookup_team_badge.cache_clear()
    monkeypatch.setattr(hockey, "TEAM_TAG", "modo")
    monkeypatch.setattr(hockey, "SCHEDULE_FETCH_BACKOFF_SECONDS", 0.0)
    yield
    hockey._schedule_cache.update(games=None, fetched_at=0.0)
    hockey.lookup_team_badge.cache_clear()


# ---------------------------------------------------------------- parser


def test_parse_regular_season():
    games = hockey.parse_games_from_html(html_from_lines(REGULAR_SEASON_LINES))

    assert len(games) == 4
    assert games[0] == {
        "date": "2025-09-19",
        "time": "19:00",
        "home_team": "MoDo Hockey",
        "away_team": "Östersunds IK",
        "home_score": 3,
        "away_score": 1,
        "venue": "Hägglunds Arena",
        "round_detail": "",
    }
    assert games[1]["home_team"] == "Almtuna IS"
    assert (games[1]["home_score"], games[1]["away_score"]) == (2, 3)
    assert games[1]["venue"] == "Upplandsbilforum Arena"
    # Ospelad match: "-" som resultat
    assert games[2]["home_score"] is None
    assert games[2]["venue"] == "Hägglunds Arena"
    # Datum ärvs av matcher utan egen datumrad
    assert games[3]["date"] == "2025-10-10"
    assert games[3]["home_team"] == "AIK"


def test_parse_playoff_format():
    games = hockey.parse_games_from_html(html_from_lines(PLAYOFF_LINES))

    assert len(games) == 2
    played, upcoming = games
    assert played["date"] == "2026-03-20"
    assert played["time"] == "19:00"
    assert played["round_detail"] == "Kvartsfinal 1"
    assert played["venue"] == "Hägglunds Arena"
    assert (played["home_score"], played["away_score"]) == (4, 2)
    assert upcoming["home_team"] == "Västerås IK"
    assert upcoming["round_detail"] == "Kvartsfinal 2"
    assert upcoming["venue"] == "ABB Arena Nord"
    assert upcoming["home_score"] is None


def test_merge_prefers_game_with_score():
    unplayed = {"date": "2025-10-10", "time": "19:00", "home_team": "A", "away_team": "B",
                "home_score": None, "away_score": None, "venue": "X"}
    played = dict(unplayed, home_score=2, away_score=1, venue="")

    assert hockey.merge_unique_games([unplayed, played]) == [played]
    assert hockey.merge_unique_games([played, unplayed]) == [played]


@pytest.mark.parametrize(
    "home_score, away_score, home, expected",
    [
        (3, 1, "MoDo Hockey", "win"),
        (1, 3, "MoDo Hockey", "loss"),
        (1, 3, "AIK", "win"),
        (2, 2, "MoDo Hockey", "draw"),
        (None, None, "MoDo Hockey", ""),
    ],
)
def test_compute_team_result(home_score, away_score, home, expected):
    away = "AIK" if home != "AIK" else "MoDo Hockey"
    game = {"home_team": home, "away_team": away,
            "home_score": home_score, "away_score": away_score}
    assert hockey.compute_team_result(game) == expected


# ---------------------------------------------------------------- badges


def test_badge_failure_is_not_cached(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            calls.append(params["t"])
            request = httpx.Request("GET", url)
            if len(calls) == 1:
                return httpx.Response(429, request=request)
            return httpx.Response(
                200, request=request,
                json={"teams": [{"strBadge": "https://www.thesportsdb.com/images/modo.png"}]},
            )

    monkeypatch.setattr(hockey.httpx, "Client", FakeClient)

    assert hockey.get_team_badge("MoDo Hockey") is None
    assert hockey.get_team_badge("MoDo Hockey") == "https://r2.thesportsdb.com/images/modo.png"
    # Lyckat svar cachas
    assert hockey.get_team_badge("MoDo Hockey") == "https://r2.thesportsdb.com/images/modo.png"
    assert len(calls) == 2


# ---------------------------------------------------------------- tid


def test_now_in_sweden_is_naive_swedish_time():
    now = hockey.now_in_sweden()
    expected = datetime.now(hockey.SWEDEN_TZ).replace(tzinfo=None)
    assert now.tzinfo is None
    assert abs((expected - now).total_seconds()) < 5


# ---------------------------------------------------------------- endpoint


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hockey, "get_team_badge", lambda name: f"badge:{name}")
    monkeypatch.setattr(hockey, "now_in_sweden", lambda: datetime(2025, 10, 1, 12, 0))
    return TestClient(hockey.app)


def fake_fetcher(pages: dict[str, str | None], calls: list[str]):
    async def fetch(client, url):
        calls.append(url)
        html = pages.get(url)
        return (html, None) if html else (None, "ConnectError: boom")

    return fetch


def test_team_endpoint(client, monkeypatch):
    calls = []
    monkeypatch.setattr(hockey, "SCHEDULE_URLS", ["u1"])
    monkeypatch.setattr(
        hockey, "fetch_schedule_html",
        fake_fetcher({"u1": html_from_lines(REGULAR_SEASON_LINES)}, calls),
    )

    body = client.get("/team").json()

    assert body["team_name"] == "MoDo Hockey"
    assert body["warnings"] == []
    assert body["last_game"]["date"] == "2025-09-27"
    assert body["last_game"]["team_result"] == "win"
    assert body["last_game"]["home_badge"] == "badge:Almtuna IS"
    assert body["next_game"]["date"] == "2025-10-10"
    assert body["next_game"]["away_team"] == "Södertälje SK"
    assert "team_result" not in body["next_game"]


def test_upcoming_uses_swedish_time(client, monkeypatch):
    # 18:30 svensk tid: matchen 19:00 samma dag är fortfarande kommande
    monkeypatch.setattr(hockey, "now_in_sweden", lambda: datetime(2025, 10, 10, 18, 30))
    monkeypatch.setattr(hockey, "SCHEDULE_URLS", ["u1"])
    monkeypatch.setattr(
        hockey, "fetch_schedule_html",
        fake_fetcher({"u1": html_from_lines(REGULAR_SEASON_LINES)}, []),
    )
    assert client.get("/team").json()["next_game"]["date"] == "2025-10-10"


def test_schedule_is_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(hockey, "SCHEDULE_URLS", ["u1"])
    monkeypatch.setattr(
        hockey, "fetch_schedule_html",
        fake_fetcher({"u1": html_from_lines(REGULAR_SEASON_LINES)}, calls),
    )

    client.get("/team")
    client.get("/team")
    assert calls == ["u1"]


def test_partial_failure_returns_warning_and_is_not_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(hockey, "SCHEDULE_URLS", ["u1", "u2"])
    monkeypatch.setattr(
        hockey, "fetch_schedule_html",
        fake_fetcher({"u1": html_from_lines(REGULAR_SEASON_LINES), "u2": None}, calls),
    )

    body = client.get("/team").json()
    assert body["warnings"] == ["Failed to fetch u2: ConnectError: boom"]
    assert body["last_game"]["date"] == "2025-09-27"

    client.get("/team")
    assert calls.count("u1") == 2


def test_stale_cache_served_when_all_sources_fail(client, monkeypatch):
    calls = []
    pages = {"u1": html_from_lines(REGULAR_SEASON_LINES)}
    monkeypatch.setattr(hockey, "SCHEDULE_URLS", ["u1"])
    monkeypatch.setattr(hockey, "SCHEDULE_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(hockey, "fetch_schedule_html", fake_fetcher(pages, calls))

    assert client.get("/team").status_code == 200
    pages["u1"] = None

    body = client.get("/team").json()
    assert body["last_game"]["date"] == "2025-09-27"
    assert body["warnings"][0] == "Failed to fetch u1: ConnectError: boom"
    assert body["warnings"][1].startswith("Serving cached schedule")


def test_all_sources_fail_without_cache(client, monkeypatch):
    monkeypatch.setattr(hockey, "SCHEDULE_URLS", ["u1"])
    monkeypatch.setattr(hockey, "fetch_schedule_html", fake_fetcher({}, []))

    resp = client.get("/team")
    assert resp.status_code == 500
    assert resp.json()["detail"]["errors"] == {"u1": "ConnectError: boom"}
