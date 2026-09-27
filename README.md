# traktlist_allocine

Scrapes movie lists from [Allocine.fr](https://www.allocine.fr) and publishes them as lists that Radarr can subscribe to, so French \*arr users can follow the Allocine box office without adding films by hand.

Four publishing channels:

| Channel | Radarr import list | Needs | Status |
| --- | --- | --- | --- |
| MDBList list | Custom Lists | free MDBList account | primary, searchable |
| TMDb list | TMDb List | free TMDb account | primary |
| JSON list | StevenLu Custom | a web server to serve `lists/` | primary |
| Trakt list | Trakt List | Trakt OAuth app (VIP-only since August 2026) | optional |

Titles are identified by IMDb/TMDb IDs resolved from Wikidata, so the MDBList, TMDb and JSON channels keep working when Trakt does not.

Published lists:

| Allocine list | MDBList (Radarr Custom Lists URL) | TMDb List ID |
| --- | --- | --- |
| Films à l'affiche | https://mdblist.com/lists/allocine/allocine-films-a-laffiche | 8699897 |
| Films à venir les plus consultés | https://mdblist.com/lists/allocine/allocine-films-a-venir-les-plus-consultes | 8699898 |

## Publishing

```bash
# JSON only (default)
python main.py publish https://www.allocine.fr/film/aucinema/ --max-movies 25 --output-dir lists

# JSON, TMDb and MDBList
python main.py publish https://www.allocine.fr/film/aucinema/ --to json --to tmdb --to mdblist
```

### MDBList and TMDb lists

On first publish a public list named `Allocine - <list title>` is created on the account; later runs find it by name and only add or remove the titles that changed. Don't rename it, or the next run will create a new one.

Titles need a TMDb ID. Wikidata supplies most of them; for the rest the TMDb ID is looked up from the IMDb ID (needs `TMDB_API_TOKEN`).

MDBList lists show up in MDBList's list search, so other users can find them. TMDb has no list search: share the ID or URL.

In Radarr:

- MDBList: Settings → Import Lists → Add → **Custom Lists**, List URL `https://mdblist.com/lists/<user>/<slug>` (printed at the end of each run)
- TMDb: Settings → Import Lists → Add → **TMDb List**, and enter the list's numeric ID (the number in `https://www.themoviedb.org/list/<id>`, printed at the end of each run)

### JSON list

This writes `lists/<slug>.json` plus an `index.json` manifest. Serve the `lists/` directory with any web server; `docker-compose.yml` mounts it to `./lists` for that purpose.

In Radarr: Settings → Import Lists → Add → **StevenLu Custom**, URL `https://your-host/lists/films-a-l-affiche.json`. Radarr's plain "Custom Lists" type matches on TMDb IDs and will not read this file.

Radarr identifies titles by `imdb_id`; the `title`, `year`, and `allocine_id` fields are there for humans debugging a bad entry.

### Safety guards

Each channel is checked separately, counting only the titles it can publish (an IMDb ID for JSON, a TMDb ID for TMDb and MDBList):

- if no title has the ID that channel needs, nothing is written
- if fewer than half as many titles are publishable as are currently published, that channel is skipped (override with `--force`)

A skipped channel leaves its list untouched and makes the run exit non-zero, but the other channels still publish. A Wikidata outage leaves every list untouched.

## Setup

### TMDb credentials

1. Create a free account at https://www.themoviedb.org and request an API key under https://www.themoviedb.org/settings/api
2. Put the **API Read Access Token** (the long one, not the API key) in `.env` as `TMDB_API_TOKEN`
3. Run `python main.py tmdb-login` (or `docker compose run --rm traktlist python main.py tmdb-login`), open the printed URL, approve, press Enter
4. Add the printed `TMDB_ACCESS_TOKEN` and `TMDB_ACCOUNT_ID` lines to `.env`

With `TMDB_ACCESS_TOKEN` set, `sync.sh` publishes to TMDb as well as JSON.

### MDBList credentials

1. Log in at https://mdblist.com and copy the API key from https://mdblist.com/preferences
2. Put it in `.env` as `MDBLIST_API_KEY`

With `MDBLIST_API_KEY` set, `sync.sh` publishes to MDBList too. The free tier allows 4 static lists and 1,000 API requests a day; a nightly run of both lists uses about a dozen.

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
MDBLIST_API_KEY=your_mdblist_api_key
TRAKT_CLIENT_ID=your_client_id
TRAKT_CLIENT_SECRET=your_client_secret
```

### Run continuously with Docker

Every push to `main` builds `ghcr.io/fiveboroughs/traktlist_allocine:latest`. The container stays running but idle between daily runs; `SYNC_TIME` defaults to `00:00` and the Compose file sets `TZ=Europe/Paris`. It waits until the next scheduled time on startup (it does not replay a missed run). A failed sync is logged and the next day's run is still scheduled.

```bash
docker compose pull
docker compose up -d --no-build
docker compose logs -f traktlist
```

To use local code changes, run `docker compose up -d --build`. To trigger one sync immediately without waiting for midnight, use `docker compose run --rm traktlist bash sync.sh`.

### TrueNAS Custom App

In **Apps → Discover Apps → Custom App → Install via YAML**, use `truenas-compose.yml`. It refers to `/mnt/Octopus/appConfigs/traktlist_allocine/.env`, `.pytrakt.json` and `lists/`; change these absolute paths for another NAS. The app must stay **Running** for TrueNAS to manage container-image updates. When a new GHCR image is available, update/redeploy the app in TrueNAS; `pull_policy: always` fetches the current `latest` tag. No TrueNAS cron job is needed.

The scheduled time and timezone can be changed with `SYNC_TIME: "HH:MM"` and `TZ` in the app YAML. Logs are available in the TrueNAS app UI. If Trakt is configured, its first device authentication requires an interactive one-off run; the OAuth token persists in `.pytrakt.json`.

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
