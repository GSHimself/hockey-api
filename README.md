# Hockey API

Ett litet Python-API (FastAPI) som hämtar **senaste spelade** och **nästa kommande** MODO-match från Swehockeys officiella spelschema:

👉 [https://stats.swehockey.se/ScheduleAndResults/Schedule/18266](https://stats.swehockey.se/ScheduleAndResults/Schedule/18266)

API:et scrapar HTML-innehållet direkt från Swehockey och gör om det till ett strukturerat JSON-svar som kan användas t.ex. i **Glance/Custom API-widgets**.

---

## 🚀 Funktioner

* Hämtar live-data från Swehockey (ingen cache på API-sidan).
* Identifierar alla matcher där **MoDo Hockey** är hemma eller bortalag.
* Hittar:

  * **Senaste spelade match** (med resultat).
  * **Nästa kommande match** (utan resultat).
* Parsern hanterar Swehockeys icke-standardiserade HTML-layout.
* Returnerar ren, enkel och widget-vänlig JSON.

---

## 📡 API-endpoint

```
GET /modo
```

Svar:

```json
{
  "last_game": {
    "date": "2025-11-12",
    "time": "19:00",
    "home_team": "Östersunds IK",
    "away_team": "MoDo Hockey",
    "home_score": 0,
    "away_score": 3,
    "venue": "Östersund Arena Hall A"
  },
  "next_game": {
    "date": "2025-11-14",
    "time": "19:00",
    "home_team": "MoDo Hockey",
    "away_team": "Kalmar HC",
    "home_score": null,
    "away_score": null,
    "venue": "Hägglunds Arena"
  }
}
```

---

## 🐳 Kör med Docker

Bygg:

```bash
docker build -t modo-swehockey-api .
```

Starta:

```bash
docker run -d -p 8000:8000 --name modo-api modo-swehockey-api
```

API finns då på:

```
http://localhost:8000/modo
```

---

## 🧩 Användning i Glance (Custom API)

```yaml
- type: custom-api
  title: MODO - matcher
  cache: 30m
  url: http://modo-swehockey-api:8000/modo
```

Widgeten hämtar data var **30:e minut** (styrt av `cache`-värdet).

---

## 📁 Projektstruktur

```
.
├── app.py               # API-logik och HTML-parser
├── requirements.txt     # Python-dependencies
└── Dockerfile           # Produktion-redo container
```

---

## 📝 Licens

Fri att använda för personliga projekt, dashboardar, Glance-screens och liknande.

