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
from typing import Any, Dict, List, Optional, Set

import requests
from rich.console import Console

console = Console()

TMDB_API = 'https://api.themoviedb.org'
MDBLIST_API = 'https://api.mdblist.com'


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


def _api_request(service: str, method: str, base: str, path: str, secret: str = '', **kwargs) -> Any:
    """
    Calls a JSON API, raising PublishError with the service's reason on failure.

    ``secret`` is scrubbed from error text: a key sent as a query parameter
    ends up in requests' connection error messages, and those reach cron mail.
    """
    try:
        response = requests.request(method, f'{base}{path}', timeout=30, **kwargs)
    except requests.exceptions.RequestException as e:
        reason = str(e).replace(secret, '***') if secret else e
        raise PublishError(f'{service} {method} {path} failed: {reason}') from None

    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.ok:
        details = body if isinstance(body, dict) else {}
        message = details.get('status_message') or details.get('error') or details.get('detail') or response.text[:200]
        # TMDb validation failures only say why in `errors`.
        if details.get('errors'):
            message = f"{message} {'; '.join(map(str, details['errors']))}"
        raise PublishError(f'{service} {method} {path} returned HTTP {response.status_code}: {message}')
    return body


def tmdb_request(token: str, method: str, path: str, **kwargs) -> Dict:
    """Calls the TMDb API with a bearer token, raising PublishError on failure."""
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json;charset=utf-8'}
    return _api_request('TMDb', method, TMDB_API, path, headers=headers, **kwargs)


def mdblist_request(api_key: str, method: str, path: str, params: Optional[Dict] = None, **kwargs) -> Any:
    """Calls the MDBList API with an API key, raising PublishError on failure."""
    return _api_request('MDBList', method, MDBLIST_API, path, secret=api_key,
                        params={**(params or {}), 'apikey': api_key}, **kwargs)


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


class MdblistPublisher(Publisher):
    """
    Keeps a public MDBList static list in sync, for Radarr's "Custom Lists" import.

    Unlike TMDb, MDBList lists are searchable, so other *arr users can find
    them. Radarr takes the list's page URL. Same lifecycle as TmdbPublisher:
    found by name, created on first publish, then only diffs are sent.
    """

    name = 'mdblist'

    def __init__(self, api_key: str):
        self.api_key = api_key

    def required_id(self) -> str:
        return 'tmdb_id'

    def _call(self, method: str, path: str, **kwargs) -> Any:
        return mdblist_request(self.api_key, method, path, **kwargs)

    def find_list(self, list_name: str) -> Optional[Dict]:
        for mdb_list in self._call('GET', '/lists/user'):
            if mdb_list.get('name') == list_name:
                return mdb_list
        return None

    def published_count(self, list_key: str, list_name: str) -> int:
        mdb_list = self.find_list(list_name)
        return mdb_list.get('items', 0) if mdb_list else 0

    def _movie_ids(self, list_id: int) -> Set[int]:
        ids: Set[int] = set()
        params: Dict = {'mediatype': 'movie', 'limit': 1000}
        while True:
            body = self._call('GET', f'/lists/{list_id}/items', params=params)
            ids.update(m['ids']['tmdb'] for m in body.get('movies', []) if m.get('ids', {}).get('tmdb'))
            cursor = (body.get('pagination') or {}).get('next_cursor')
            if not cursor:
                return ids
            params = {**params, 'cursor': cursor}

    def publish(self, list_key: str, list_name: str, description: str,
                entries: List[ListEntry]) -> PublishResult:
        usable, unusable = self.usable_entries(entries)
        result = PublishResult(
            target=self.name,
            skipped=len(unusable),
            skipped_titles=[e.title for e in unusable],
        )

        wanted: Dict[int, ListEntry] = {}
        for entry in usable:
            wanted.setdefault(entry.tmdb_id, entry)

        def payload(tmdb_ids):
            movies = []
            for tmdb_id in tmdb_ids:
                item: Dict = {'tmdb': tmdb_id}
                if tmdb_id in wanted and wanted[tmdb_id].imdb_id:
                    item['imdb'] = wanted[tmdb_id].imdb_id
                movies.append(item)
            return {'movies': movies}

        try:
            mdb_list = self.find_list(list_name)
            if mdb_list:
                list_id = mdb_list['id']
                current = self._movie_ids(list_id)
            else:
                list_id = self._call('POST', '/lists/user/add', json={'name': list_name, 'private': False})['id']
                current = set()

            to_add = [tmdb_id for tmdb_id in wanted if tmdb_id not in current]
            to_remove = [tmdb_id for tmdb_id in current if tmdb_id not in wanted]

            not_found = 0
            if to_add:
                body = self._call('POST', f'/lists/{list_id}/items/add', json=payload(to_add))
                not_found = (body.get('not_found') or {}).get('movies', 0)
            # Add before remove, as in TmdbPublisher.
            if to_remove:
                self._call('POST', f'/lists/{list_id}/items/remove', json=payload(to_remove))

            # The add response only counts misses, so re-read to name them.
            rejected: List[int] = []
            if not_found:
                listed = self._movie_ids(list_id)
                rejected = [tmdb_id for tmdb_id in wanted if tmdb_id not in listed]
            # Returned as a one-element array, despite the schema.
            info = self._call('GET', f'/lists/{list_id}')[0]
        except PublishError as e:
            result.error = str(e)
            return result

        console.print(f"[dim]MDBList list {list_id}: +{len(to_add) - len(rejected)} -{len(to_remove)}[/dim]")
        result.published = len(wanted) - len(rejected)
        result.skipped += len(rejected)
        result.skipped_titles += [f"{wanted[tmdb_id].title} (MDBList does not know TMDb ID {tmdb_id})" for tmdb_id in rejected]
        result.location = f"https://mdblist.com/lists/{info['user_name']}/{info['slug']}"
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
