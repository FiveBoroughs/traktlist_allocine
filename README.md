# traktlist_allocine

Scrapes movie lists from [Allocine.fr](https://www.allocine.fr) and publishes them as lists that Radarr can subscribe to, so French \*arr users can follow the Allocine box office without adding films by hand.

Three publishing channels:

| Channel | Radarr import list | Needs | Status |
| --- | --- | --- | --- |
| TMDb list | TMDb List | free TMDb account | primary |
| JSON list | StevenLu Custom | a web server to serve `lists/` | primary |
| Trakt list | Trakt List | Trakt OAuth app (VIP-only since August 2026) | optional |

Titles are identified by IMDb/TMDb IDs resolved from Wikidata, so the TMDb and JSON channels keep working when Trakt does not.

## Publishing

```bash
# JSON only (default)
python main.py publish https://www.allocine.fr/film/aucinema/ --max-movies 25 --output-dir lists

# JSON and TMDb
python main.py publish https://www.allocine.fr/film/aucinema/ --to json --to tmdb
```

### TMDb list

On first publish a public list named `Allocine - <list title>` is created on your TMDb account; later runs find it by name and only add or remove the titles that changed. Don't rename it on TMDb, or the next run will create a new one.

Titles need a TMDb ID. Wikidata supplies most of them; for the rest the TMDb ID is looked up from the IMDb ID.

In Radarr: Settings → Import Lists → Add → **TMDb List**, and enter the list's numeric ID (the number in `https://www.themoviedb.org/list/<id>`, printed at the end of each run).

### JSON list

This writes `lists/<slug>.json` plus an `index.json` manifest. Serve the `lists/` directory with any web server; `docker-compose.yml` mounts it to `./lists` for that purpose.

In Radarr: Settings → Import Lists → Add → **StevenLu Custom**, URL `https://your-host/lists/films-a-l-affiche.json`. Radarr's plain "Custom Lists" type matches on TMDb IDs and will not read this file.

Radarr identifies titles by `imdb_id`; the `title`, `year`, and `allocine_id` fields are there for humans debugging a bad entry.

### Safety guards

Each channel is checked separately, counting only the titles it can publish (an IMDb ID for JSON, a TMDb ID for TMDb):

- if no title has the ID that channel needs, nothing is written
- if fewer than half as many titles are publishable as are currently published, that channel is skipped (override with `--force`)

A skipped channel leaves its list untouched and makes the run exit non-zero, but the other channel still publishes. A Wikidata outage leaves every list untouched.

## Setup

### TMDb credentials

1. Create a free account at https://www.themoviedb.org and request an API key under https://www.themoviedb.org/settings/api
2. Put the **API Read Access Token** (the long one, not the API key) in `.env` as `TMDB_API_TOKEN`
3. Run `python main.py tmdb-login` (or `docker compose run --rm traktlist python main.py tmdb-login`), open the printed URL, approve, press Enter
4. Add the printed `TMDB_ACCESS_TOKEN` and `TMDB_ACCOUNT_ID` lines to `.env`

With `TMDB_ACCESS_TOKEN` set, `sync.sh` publishes to TMDb as well as JSON.

### Trakt API credentials (optional)

Trakt made API application creation VIP-only in August 2026 and removed some
existing applications on free accounts, so this channel needs a paid Trakt VIP
account. Without credentials in `.env`, `sync.sh` skips the Trakt step.

1. Go to https://trakt.tv/oauth/applications/new
2. Fill in a name (e.g. `allocine-sync`)
3. Set the redirect URI to `urn:ietf:wg:oauth:2.0:oob`
4. Save and copy the Client ID and Client Secret

### Configure

```bash
cp .env.example .env
```

Add your credentials to `.env`:

```
TMDB_API_TOKEN=your_api_read_access_token
TMDB_ACCESS_TOKEN=printed_by_tmdb_login
TMDB_ACCOUNT_ID=printed_by_tmdb_login
TRAKT_CLIENT_ID=your_client_id
TRAKT_CLIENT_SECRET=your_client_secret
```

### Run with Docker

Every push to `main` builds `ghcr.io/fiveboroughs/traktlist_allocine:latest`. To run it you only need `docker-compose.yml`, `.env` and (for Trakt) `.pytrakt.json` in one directory:

```bash
docker compose pull && docker compose run --rm traktlist
```

To run local code changes instead, build from the checkout: `docker compose run --build --rm traktlist`.

If Trakt is configured, on first run you'll be prompted to authenticate with Trakt via device code. The OAuth token is persisted in `.pytrakt.json`.

### Cron

```bash
docker compose -f /path/to/docker-compose.yml pull && docker compose -f /path/to/docker-compose.yml run --rm traktlist >> /path/to/sync.log 2>&1
```

## Usage

```bash
# Movies currently in theaters
python main.py publish https://www.allocine.fr/film/aucinema/ --to json --to tmdb

# Most anticipated upcoming movies, to Trakt
python main.py sync-to-trakt https://www.allocine.fr/film/attendus/ --max-movies 25
```

## How Trakt matching works

For each movie scraped from Allocine, `sync-to-trakt` tries to find a match on Trakt:

1. Wikidata/IMDb: looks up the Allocine ID on Wikidata to get the IMDb ID, then searches Trakt
2. Original title: searches Trakt using the movie's original (non-French) title
3. French title: falls back to searching Trakt with the French title

Each match is verified by comparing directors and release year (±1 year).
