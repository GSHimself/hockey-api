# 🏒 Hockey Schedule API

A small FastAPI microservice that fetches hockey schedules from **stats.swehockey.se**, parses the raw HTML, and exposes a clean JSON API for a specific team.
Designed to be lightweight and stateless — run it in Docker, Kubernetes, or k3s.
Works great as a backend for **Glance** dashboards, Home Assistant, or any custom UI.

The API supports **any team**, based on a configurable substring (e.g., `"modo"`, `"aik"`, `"björklöven"`).
No code changes required — everything is configured via environment variables.

![exampleIMg](./example/example.jpeg)

---

## ✨ Features

* Fetches & parses games from one or more Swehockey schedule URLs
* Supports *any* team through a simple environment variable (`TEAM_TAG`)
* Returns both **last played game** and **next upcoming game**
* Automatically fetches **team badges/logos** from TheSportsDB
* Computes match result from your team's perspective: `"win"`, `"loss"`, or `"draw"`
* Lightweight, fast, cache-friendly
* Stateless — easy to deploy in Docker, Kubernetes, or k3s

---

## 🚀 Quick Start (Docker)

The image is published to GitHub Container Registry and requires no authentication to pull.

Run the API for MoDo Hockey:

```bash
docker run -d \
  -p 8000:8000 \
  -e TEAM_TAG="modo" \
  -e SCHEDULE_URLS="https://stats.swehockey.se/ScheduleAndResults/Schedule/18266,https://stats.swehockey.se/ScheduleAndResults/Schedule/18267" \
  -e THESPORTSDB_API_KEY="YOUR_API_KEY" \
  ghcr.io/gshimself/hockey-api:latest
```

Then open:

```
http://localhost:8000/team
```

Schedule URLs are found on [stats.swehockey.se](https://stats.swehockey.se) — navigate to your league's schedule page and copy the URL.

---

## ⚙️ Configuration

The service is configured entirely with environment variables:

| Variable              | Required    | Description                                                             | Example                                                 |
| --------------------- | ----------- | ----------------------------------------------------------------------- | ------------------------------------------------------- |
| `TEAM_TAG`            | Yes         | Substring used to identify the team (case-insensitive)                  | `modo`, `aik`, `björklöven`                             |
| `SCHEDULE_URLS`       | Recommended | One or more Swehockey schedule URLs (comma/semicolon/newline separated) | `https://.../Schedule/18266,https://.../Schedule/18267` |
| `SCHEDULE_URL`        | Optional    | Backward-compatible single schedule URL                                 | `https://.../Schedule/18266`                            |
| `THESPORTSDB_API_KEY` | Optional    | API key for badge/logo fetching                                         | `123` (free tier)                                       |

### How TEAM_TAG works

The API matches all games where `TEAM_TAG` appears in either team name.

| TEAM_TAG | Matches                          |
| -------- | -------------------------------- |
| `modo`   | "MoDo Hockey", "MODO Hockey Dam" |
| `löven`  | "IF Björklöven"                  |
| `aik`    | "AIK", "AIK Hockey"              |

---

## 📡 API Endpoints

### `GET /team`

Returns the last played match and the next upcoming match for `TEAM_TAG`.

#### Example JSON output

```json
{
  "team_tag": "modo",
  "team_name": "MoDo Hockey",
  "schedule_urls": [
    "https://stats.swehockey.se/ScheduleAndResults/Schedule/18266",
    "https://stats.swehockey.se/ScheduleAndResults/Schedule/18267"
  ],
  "last_game": {
    "date": "2025-11-26",
    "time": "19:00",
    "home_team": "AIK",
    "away_team": "MoDo Hockey",
    "home_score": 1,
    "away_score": 2,
    "venue": "Hovet, Johanneshov",
    "round_detail": "",
    "home_badge": "https://r2.thesportsdb.com/images/media/team/badge123.png",
    "away_badge": "https://r2.thesportsdb.com/images/media/team/badge456.png",
    "team_result": "win"
  },
  "next_game": {
    "date": "2025-11-28",
    "time": "20:30",
    "home_team": "MoDo Hockey",
    "away_team": "IF Björklöven",
    "home_score": null,
    "away_score": null,
    "venue": "Hägglunds Arena",
    "round_detail": "Quarter-final 2",
    "home_badge": "...",
    "away_badge": "..."
  }
}
```

---

## 🧩 How It Works

1. Fetches Swehockey schedule HTML from all configured `SCHEDULE_URLS` in parallel
2. Parses HTML with BeautifulSoup into structured game records (date, time, teams, scores, venue)
3. Merges duplicate games across multiple URL sources
4. Filters games by `TEAM_TAG` (case-insensitive substring match)
5. Identifies `last_game` (most recent played) and `next_game` (next upcoming)
6. Fetches team badges from TheSportsDB concurrently (LRU-cached per process lifetime)
7. Computes `team_result` from the filtered team's perspective
8. Returns clean JSON

---

## 📦 Docker Compose

Save as `docker-compose.yml` and run `docker compose up -d`:

```yaml
services:
  hockey-api:
    image: ghcr.io/gshimself/hockey-api:latest
    restart: unless-stopped
    environment:
      TEAM_TAG: "modo"
      SCHEDULE_URLS: "https://stats.swehockey.se/ScheduleAndResults/Schedule/18266,https://stats.swehockey.se/ScheduleAndResults/Schedule/18267"
      THESPORTSDB_API_KEY: "YOUR_API_KEY"
    ports:
      - "8000:8000"
```

Then open `http://localhost:8000/team`.

---

## ☸️ Kubernetes Deployment

The image is pulled directly from GHCR — no build step required.

### 1. Create manifest

Save as `k8s/hockey-api.yaml`:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: hockey
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: hockey-api-config
  namespace: hockey
data:
  TEAM_TAG: "modo"
  SCHEDULE_URLS: "https://stats.swehockey.se/ScheduleAndResults/Schedule/18266,https://stats.swehockey.se/ScheduleAndResults/Schedule/18267"
---
apiVersion: v1
kind: Secret
metadata:
  name: hockey-api-secret
  namespace: hockey
type: Opaque
stringData:
  THESPORTSDB_API_KEY: "YOUR_API_KEY"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hockey-api
  namespace: hockey
spec:
  replicas: 1
  selector:
    matchLabels:
      app: hockey-api
  template:
    metadata:
      labels:
        app: hockey-api
    spec:
      containers:
        - name: hockey-api
          image: ghcr.io/gshimself/hockey-api:latest
          imagePullPolicy: Always
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef:
                name: hockey-api-config
            - secretRef:
                name: hockey-api-secret
          readinessProbe:
            httpGet:
              path: /
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 20
---
apiVersion: v1
kind: Service
metadata:
  name: hockey-api
  namespace: hockey
spec:
  selector:
    app: hockey-api
  ports:
    - name: http
      port: 8000
      targetPort: 8000
  type: ClusterIP
```

### 2. Apply and verify

```bash
kubectl apply -f k8s/hockey-api.yaml
kubectl -n hockey rollout status deploy/hockey-api
kubectl -n hockey get pods,svc
```

### 3. Test via port-forward

```bash
kubectl -n hockey port-forward svc/hockey-api 8000:8000
curl http://localhost:8000/team
```

Once running, other pods in the cluster can reach the API at `http://hockey-api.hockey.svc.cluster.local:8000` (or simply `http://hockey-api:8000` from within the same namespace).

### Optional: expose through Ingress

If you run an ingress controller (nginx/traefik), add an `Ingress` resource for `hockey-api` on port `8000`.

---

## 🖥️ Using with Glance Dashboard

Below is a full Glance widget example with team logos and colour-coded scores.
This example uses internal Kubernetes cluster DNS (`http://hockey-api:8000`); adjust the URL if you run the service differently (e.g., `http://localhost:8000`).

Adapt the title, labels, and links to match your own team.

```yaml
- type: custom-api
  title: MODO - Matches
  cache: 30m
  url: http://hockey-api:8000/team
  template: |
    {{ $lastDate := .JSON.String "last_game.date" }}
    {{ $nextDate := .JSON.String "next_game.date" }}

    {{ $lastHomeBadge := .JSON.String "last_game.home_badge" }}
    {{ $lastAwayBadge := .JSON.String "last_game.away_badge" }}
    {{ $nextHomeBadge := .JSON.String "next_game.home_badge" }}
    {{ $nextAwayBadge := .JSON.String "next_game.away_badge" }}

    {{ $teamResult := .JSON.String "last_game.team_result" }}

    <style>
      .team { display: inline-flex; align-items: center; gap: .3rem; }
      .muted2 { opacity: .8; }
      ul.fixtures { list-style: none; padding-left: 0; margin: .25rem 0 0; }
      ul.fixtures li { margin: .25rem 0; }

      .team-logo {
        height: 18px;
        width: auto;
        border-radius: 3px;
      }

      .score {
        font-weight: 600;
      }

      .score.team-win {
        color: #29a329;
      }

      .score.team-loss {
        color: #cc0000;
      }

      .score.team-neutral {
        color: inherit;
      }
    </style>

    <div class="stack gap-2">

      <!-- LAST GAME -->
      <div>
        <p class="muted">Last game</p>
        <ul class="fixtures">
          {{ if ne $lastDate "" }}
            <li>
              {{ $lh := .JSON.String "last_game.home_team" }}
              {{ $la := .JSON.String "last_game.away_team" }}

              <span class="team">
                {{ if ne $lastHomeBadge "" }}
                  <img class="team-logo" src="{{ $lastHomeBadge }}" alt="{{ $lh }}" />
                {{ end }}
                {{ $lh }}
              </span>

              <span class="score
                {{ if eq $teamResult "win" }} team-win
                {{ else if eq $teamResult "loss" }} team-loss
                {{ else }} team-neutral
                {{ end }}">
                {{ .JSON.Int "last_game.home_score" }}–{{ .JSON.Int "last_game.away_score" }}
              </span>

              <span class="team">
                {{ if ne $lastAwayBadge "" }}
                  <img class="team-logo" src="{{ $lastAwayBadge }}" alt="{{ $la }}" />
                {{ end }}
                {{ $la }}
              </span>

              <span class="muted2">
                · {{ .JSON.String "last_game.date" }} {{ .JSON.String "last_game.time" }} · {{ .JSON.String "last_game.venue" }}
              </span>
            </li>
          {{ else }}
            <li class="muted">No played match found.</li>
          {{ end }}
        </ul>
      </div>

      <hr style="margin: .5rem 0 1rem;" />

      <!-- NEXT GAME -->
      <div>
        <p class="muted">Next game</p>
        <ul class="fixtures">
          {{ if ne $nextDate "" }}
            <li>
              {{ $nh := .JSON.String "next_game.home_team" }}
              {{ $na := .JSON.String "next_game.away_team" }}

              <span class="team">
                {{ if ne $nextHomeBadge "" }}
                  <img class="team-logo" src="{{ $nextHomeBadge }}" alt="{{ $nh }}" />
                {{ end }}
                {{ $nh }}
              </span>
              vs
              <span class="team">
                {{ if ne $nextAwayBadge "" }}
                  <img class="team-logo" src="{{ $nextAwayBadge }}" alt="{{ $na }}" />
                {{ end }}
                {{ $na }}
              </span>
              <span class="muted2">
                · {{ .JSON.String "next_game.date" }} {{ .JSON.String "next_game.time" }}
              </span>
            </li>
          {{ else }}
            <li class="muted">No upcoming matches found.</li>
          {{ end }}
        </ul>
      </div>

      <hr style="margin: .5rem 0 1rem;" />
      <a class="button" href="https://www.flashscore.se/lag/modo-hc/nwkwmtM7/resultat/"
         target="_blank">Open Flashscore</a>
    </div>
```

---

## 🏗️ Development

After making code changes, push a version tag to trigger the GitHub Actions build:

```bash
git tag v1.x.x
git push --tags
```

GitHub Actions builds and pushes the image to `ghcr.io/gshimself/hockey-api` with both a version tag and `latest`. Update the image tag in your k8s manifest and apply it on your cluster to test.

There is no local test setup — deploy to k8s to verify changes.

---

## 🤝 Contributing

Pull requests and issues are welcome!
Ideas for improvements include:

* Better parser for irregular Swehockey formats
* Multi-team support (`/team/{tag}`)
* Caching layer
* Tests
* Logo provider fallbacks
* Automatic schedule discovery

---

## 📄 License

MIT License – you are free to use, modify and distribute.

---
