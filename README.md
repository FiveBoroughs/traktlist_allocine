# traktlist_allocine

Scrapes movie lists from [Allocine.fr](https://www.allocine.fr) and syncs them to [Trakt.tv](https://trakt.tv) lists. Matches movies via Wikidata/IMDb lookups, original title, and French title search with director verification.

## Setup

### Trakt API credentials

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
TRAKT_CLIENT_ID=your_client_id
TRAKT_CLIENT_SECRET=your_client_secret
```

### Run with Docker

```bash
docker compose run --build --rm traktlist
```

On first run you'll be prompted to authenticate with Trakt via device code. The OAuth token is persisted in `.pytrakt.json`.

### Cron

```bash
docker compose -f /path/to/docker-compose.yml run --build --rm traktlist >> /path/to/sync.log 2>&1
```

## Usage

```bash
# Movies currently in theaters
python main.py sync-to-trakt https://www.allocine.fr/film/aucinema/ --max-movies 25

# Most anticipated upcoming movies
python main.py sync-to-trakt https://www.allocine.fr/film/attendus/ --max-movies 25
```

## How matching works

For each movie scraped from Allocine, the tool tries to find a match on Trakt:

1. Wikidata/IMDb: looks up the Allocine ID on Wikidata to get the IMDb ID, then searches Trakt
2. Original title: searches Trakt using the movie's original (non-French) title
3. French title: falls back to searching Trakt with the French title

Each match is verified by comparing directors and release year (±1 year).
