"""
Resolves Allocine IDs to external IDs (IMDb, TMDb) via Wikidata.

This is the platform-neutral half of the pipeline: it turns scraped Allocine
data into :class:`ListEntry` records that any publisher can consume, without
depending on a Trakt account or any other API key.

Wikidata is queried in batches - one SPARQL query per batch of Allocine IDs
rather than one per title, which keeps us well clear of the endpoint's rate
limiting.
"""

import time
from typing import Dict, List, Optional

import requests
from rich.console import Console

from publishers import ListEntry, PublishError, tmdb_request

console = Console()

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "TraktListAllocineBot/1.0 (https://github.com/FiveBoroughs/traktlist_allocine)"

#: Wikidata properties. P1265 is the Allocine *film* ID - series use a separate
#: property and are not reliably resolvable this way.
P_ALLOCINE_FILM = "P1265"
P_IMDB = "P345"
P_TMDB_FILM = "P4947"

#: Allocine IDs per SPARQL query.
BATCH_SIZE = 50


class WikidataUnavailable(RuntimeError):
    """Raised when Wikidata cannot be reached, so callers don't mistake a failed
    lookup for 'this title has no IMDb ID' and publish a gutted list."""


def _query_batch(allocine_ids: List[str]) -> Dict[str, Dict[str, str]]:
    """Runs one batched SPARQL query, retrying on rate limits."""
    values = " ".join(f'"{aid}"' for aid in allocine_ids)
    query = f"""
    SELECT ?allocineId ?imdbId ?tmdbId WHERE {{
      VALUES ?allocineId {{ {values} }}
      ?item wdt:{P_ALLOCINE_FILM} ?allocineId.
      OPTIONAL {{ ?item wdt:{P_IMDB} ?imdbId. }}
      OPTIONAL {{ ?item wdt:{P_TMDB_FILM} ?tmdbId. }}
    }}
    """

    for attempt in range(4):
        try:
            response = requests.get(
                WIKIDATA_ENDPOINT,
                params={'query': query, 'format': 'json'},
                headers={'User-Agent': USER_AGENT},
                timeout=60,
            )
            if response.status_code == 429:
                wait = 5 * 2 ** attempt  # 5, 10, 20, 40 seconds
                console.log(f"[yellow]Wikidata rate limited, retrying in {wait}s...[/yellow]")
                time.sleep(wait)
                continue
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            # Transient failures are common enough here that giving up on the
            # first one silently publishes a list with no IDs resolved.
            wait = 5 * 2 ** attempt
            console.log(f"[yellow]Wikidata query failed ({e}), retrying in {wait}s...[/yellow]")
            time.sleep(wait)
            continue

        resolved: Dict[str, Dict[str, str]] = {}
        for binding in response.json().get('results', {}).get('bindings', []):
            allocine_id = binding.get('allocineId', {}).get('value')
            if not allocine_id:
                continue
            # A title can have several Wikidata statements; keep the first
            # non-empty value seen for each ID.
            entry = resolved.setdefault(allocine_id, {})
            for key, binding_key in (('imdb_id', 'imdbId'), ('tmdb_id', 'tmdbId')):
                value = binding.get(binding_key, {}).get('value')
                if value and not entry.get(key):
                    entry[key] = value
        return resolved

    raise WikidataUnavailable("Wikidata did not answer after 4 attempts")


def resolve_external_ids(allocine_ids: List[str]) -> Dict[str, Dict[str, str]]:
    """Maps Allocine IDs to {'imdb_id': ..., 'tmdb_id': ...} where Wikidata knows them."""
    unique_ids = list(dict.fromkeys(aid for aid in allocine_ids if aid))
    if not unique_ids:
        return {}

    resolved: Dict[str, Dict[str, str]] = {}
    for start in range(0, len(unique_ids), BATCH_SIZE):
        batch = unique_ids[start:start + BATCH_SIZE]
        console.log(f"  Resolving IDs via Wikidata ({start + 1}-{start + len(batch)} of {len(unique_ids)})...")
        resolved.update(_query_batch(batch))

    return resolved


def _to_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def build_entries(scraped_items: List[Dict]) -> List[ListEntry]:
    """
    Turns scraped Allocine dicts into ListEntry records with external IDs.

    Order is preserved and duplicates (same Allocine ID) are dropped, so the
    published list matches the order Allocine presents.
    """
    id_map = resolve_external_ids([item.get('allocine_id') for item in scraped_items])

    entries: List[ListEntry] = []
    seen = set()
    for item in scraped_items:
        allocine_id = item.get('allocine_id')
        if not allocine_id or allocine_id in seen:
            continue
        seen.add(allocine_id)

        ids = id_map.get(allocine_id, {})
        entries.append(ListEntry(
            title=item.get('title', ''),
            year=item.get('release_year'),
            media_type='series' if item.get('content_type') == 'series' else 'movie',
            allocine_id=allocine_id,
            imdb_id=ids.get('imdb_id'),
            tmdb_id=_to_int(ids.get('tmdb_id')),
        ))

    return entries


def fill_tmdb_ids(entries: List[ListEntry], api_token: str) -> int:
    """
    Looks up TMDb IDs by IMDb ID for films Wikidata only half-resolved.

    Wikidata often has the IMDb ID of a recent French release but not yet its
    TMDb ID. Returns how many entries were filled in.
    """
    filled = 0
    for entry in entries:
        if entry.tmdb_id or not entry.imdb_id or entry.media_type != 'movie':
            continue
        try:
            found = tmdb_request(api_token, 'GET', f'/3/find/{entry.imdb_id}',
                                 params={'external_source': 'imdb_id'})
        except PublishError as e:
            console.log(f"[yellow]TMDb lookup failed for '{entry.title}': {e}[/yellow]")
            continue
        movies = found.get('movie_results') or []
        if movies:
            entry.tmdb_id = movies[0].get('id')
            filled += 1
    return filled
