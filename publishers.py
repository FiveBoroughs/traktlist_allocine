"""
Publishing targets for an Allocine list.

The scraper produces platform-neutral :class:`ListEntry` records; each publisher
knows how to push those to one destination (a JSON file an *arr can subscribe
to, a TMDb list, a Trakt list). Adding a destination means adding a Publisher,
not touching the scraping or matching code.
"""

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests
from rich.console import Console

console = Console()

TMDB_API = 'https://api.themoviedb.org'


class PublishError(RuntimeError):
    """A destination's API refused a request or could not be reached."""


@dataclass
class ListEntry:
    """One title, identified by whatever external IDs we managed to resolve."""

    title: str
    year: Optional[str] = None
    media_type: str = 'movie'  # 'movie' or 'series'
    allocine_id: Optional[str] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    trakt_id: Optional[int] = None
    poster_url: Optional[str] = None

    def has_any_id(self) -> bool:
        return bool(self.imdb_id or self.tmdb_id or self.trakt_id)


@dataclass
class PublishResult:
    """What a publisher managed to do, so the CLI can report per-target."""

    target: str
    published: int = 0
    skipped: int = 0
    location: Optional[str] = None
    error: Optional[str] = None
    skipped_titles: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


def slugify(text: str) -> str:
    """Turns a list title into a filename-safe slug."""
    import unicodedata

    normalized = unicodedata.normalize('NFKD', text)
    ascii_text = ''.join(c for c in normalized if not unicodedata.combining(c))
    slug = re.sub(r'[^a-z0-9]+', '-', ascii_text.lower()).strip('-')
    return slug or 'allocine-list'


def tmdb_request(token: str, method: str, path: str, **kwargs) -> Dict:
    """Calls the TMDb API with a bearer token, raising PublishError on failure."""
    try:
        response = requests.request(
            method,
            f'{TMDB_API}{path}',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json;charset=utf-8'},
            timeout=30,
            **kwargs,
        )
    except requests.exceptions.RequestException as e:
        raise PublishError(f'TMDb {method} {path} failed: {e}') from e

    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.ok:
        message = body.get('status_message') or response.text[:200]
        # Validation failures only say why in `errors`.
        if body.get('errors'):
            message = f"{message} {'; '.join(map(str, body['errors']))}"
        raise PublishError(f'TMDb {method} {path} returned HTTP {response.status_code}: {message}')
    return body


class Publisher(ABC):
    """A destination an Allocine list can be published to."""

    #: Human-readable name used in CLI output
    name: str = 'publisher'

    @abstractmethod
    def required_id(self) -> str:
        """Name of the ListEntry field this target needs to identify a title."""

    @abstractmethod
    def published_count(self, list_key: str, list_name: str) -> int:
        """How many titles the destination currently holds, 0 if the list doesn't exist."""

    @abstractmethod
    def publish(self, list_key: str, list_name: str, description: str,
                entries: List[ListEntry]) -> PublishResult:
        """Pushes entries to the destination, replacing whatever was there."""

    def usable_entries(self, entries: List[ListEntry]):
        """Splits entries into those this target can identify and those it can't."""
        id_field = self.required_id()
        usable = [e for e in entries if getattr(e, id_field, None)]
        unusable = [e for e in entries if not getattr(e, id_field, None)]
        return usable, unusable


class JsonPublisher(Publisher):
    """
    Writes a JSON file that Radarr can subscribe to as a "StevenLu Custom" list.

    Radarr identifies titles by ``imdb_id``; ``title`` and ``poster_url`` are
    cosmetic and extra keys are ignored, so the file carries the Allocine ID and
    year too - useful for debugging a bad match without re-scraping.
    """

    name = 'json'

    def __init__(self, output_dir: str):
        self.output_dir = output_dir

    def required_id(self) -> str:
        return 'imdb_id'

    def published_count(self, list_key: str, list_name: str) -> int:
        path = os.path.join(self.output_dir, f'{list_key}.json')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return len(json.load(f))
        except (OSError, ValueError, TypeError):
            return 0

    def publish(self, list_key: str, list_name: str, description: str,
                entries: List[ListEntry]) -> PublishResult:
        usable, unusable = self.usable_entries(entries)
        result = PublishResult(
            target=self.name,
            published=len(usable),
            skipped=len(unusable),
            skipped_titles=[e.title for e in unusable],
        )

        payload = []
        for entry in usable:
            item: Dict = {'title': entry.title, 'imdb_id': entry.imdb_id}
            if entry.poster_url:
                item['poster_url'] = entry.poster_url
            # Ignored by Radarr, kept for traceability back to the source.
            item['allocine_id'] = entry.allocine_id
            item['year'] = entry.year
            payload.append(item)

        path = os.path.join(self.output_dir, f'{list_key}.json')
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as e:
            result.error = str(e)
            return result

        result.location = path
        return result


class TmdbPublisher(Publisher):
    """
    Keeps a public TMDb list in sync, for Radarr's "TMDb List" import.

    TMDb hosts the list for free, so subscribers only need its numeric ID and
    nobody has to run a web server. The list is found on the account by name
    and created on first publish; later runs only add and remove what changed.
    """

    name = 'tmdb'

    def __init__(self, access_token: str, account_id: str):
        self.access_token = access_token
        self.account_id = account_id

    def required_id(self) -> str:
        return 'tmdb_id'

    def _call(self, method: str, path: str, **kwargs) -> Dict:
        return tmdb_request(self.access_token, method, path, **kwargs)

    def _paged(self, path: str):
        """Yields every result across the pages of a paginated TMDb endpoint."""
        page, total_pages = 1, 1
        while page <= total_pages:
            body = self._call('GET', path, params={'page': page})
            yield from body.get('results', [])
            total_pages = body.get('total_pages') or 1
            page += 1

    def find_list(self, list_name: str) -> Optional[Dict]:
        for tmdb_list in self._paged(f'/4/account/{self.account_id}/lists'):
            if tmdb_list.get('name') == list_name:
                return tmdb_list
        return None

    def published_count(self, list_key: str, list_name: str) -> int:
        tmdb_list = self.find_list(list_name)
        return tmdb_list.get('number_of_items', 0) if tmdb_list else 0

    def publish(self, list_key: str, list_name: str, description: str,
                entries: List[ListEntry]) -> PublishResult:
        usable, unusable = self.usable_entries(entries)
        result = PublishResult(
            target=self.name,
            skipped=len(unusable),
            skipped_titles=[e.title for e in unusable],
        )

        # Two Allocine entries can resolve to the same film; keep Allocine's order.
        wanted: Dict[int, str] = {}
        for entry in usable:
            wanted.setdefault(entry.tmdb_id, entry.title)

        try:
            tmdb_list = self.find_list(list_name)
            if tmdb_list:
                list_id = tmdb_list['id']
                current = {item['id'] for item in self._paged(f'/4/list/{list_id}')
                           if item.get('media_type') == 'movie'}
            else:
                # Radarr reads the list without a user token, so it must be public.
                list_id = self._call('POST', '/4/list', json={
                    'name': list_name,
                    'description': description,
                    'iso_639_1': 'fr',
                    'iso_3166_1': 'FR',
                    'public': True,
                })['id']
                current = set()

            to_add = [tmdb_id for tmdb_id in wanted if tmdb_id not in current]
            to_remove = [tmdb_id for tmdb_id in current if tmdb_id not in wanted]

            rejected: List[int] = []
            if to_add:
                body = self._call('POST', f'/4/list/{list_id}/items', json={
                    'items': [{'media_type': 'movie', 'media_id': tmdb_id} for tmdb_id in to_add],
                })
                rejected = [r.get('media_id') for r in body.get('results', []) if not r.get('success')]
            # Removing after adding means a run that dies halfway leaves stale
            # titles on the list rather than missing ones.
            if to_remove:
                self._call('DELETE', f'/4/list/{list_id}/items', json={
                    'items': [{'media_type': 'movie', 'media_id': tmdb_id} for tmdb_id in to_remove],
                })
        except PublishError as e:
            result.error = str(e)
            return result

        console.print(f"[dim]TMDb list {list_id}: +{len(to_add) - len(rejected)} -{len(to_remove)}[/dim]")
        result.published = len(wanted) - len(rejected)
        result.skipped += len(rejected)
        result.skipped_titles += [f"{wanted.get(tmdb_id, tmdb_id)} (TMDb rejected ID {tmdb_id})" for tmdb_id in rejected]
        result.location = f'https://www.themoviedb.org/list/{list_id}'
        return result


def write_index(output_dir: str, lists: List[Dict]) -> str:
    """
    Upserts entries into the index of published JSON lists.

    Not consumed by Radarr - it exists so subscribers have one URL to look at
    to discover what lists are available and when they were last built. Entries
    are merged by slug so publishing one list does not drop the others.
    """
    path = os.path.join(output_dir, 'index.json')
    os.makedirs(output_dir, exist_ok=True)

    existing: List[Dict] = []
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                existing = loaded
        except (OSError, ValueError):
            console.print(f"[bold yellow]Warning: could not read {path}, rebuilding it.[/bold yellow]")

    by_slug = {entry.get('slug'): entry for entry in existing if isinstance(entry, dict)}
    for entry in lists:
        by_slug[entry.get('slug')] = entry

    merged = sorted(by_slug.values(), key=lambda e: e.get('slug') or '')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return path
