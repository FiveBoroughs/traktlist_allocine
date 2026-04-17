import trakt
from trakt.users import User
import requests
import json
import os
import time
from rich.console import Console
from typing import Optional, List, Dict
from types import SimpleNamespace

console = Console()

class TraktClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        trakt.CLIENT_ID = client_id
        trakt.CLIENT_SECRET = client_secret
        trakt.core.AUTH_METHOD = trakt.core.DEVICE_AUTH
        trakt.core.session.headers['User-Agent'] = 'pytrakt/4.2.2'

        # Set config path to current directory to ensure persistence visibility
        self.config_path = os.path.join(os.getcwd(), ".pytrakt.json")
        trakt.core.CONFIG_PATH = self.config_path
        
        self.authenticated_username = None

    def authenticate(self):
        """
        Ensures the client is authenticated. Tries to use stored credentials first.
        Only triggers interactive auth if necessary.
        """
        console.print("[bold yellow]Checking authentication status...[/bold yellow]")
        
        try:
            me = User('me')
            self.authenticated_username = me.username
            console.print(f"[bold green]Authenticated as '{self.authenticated_username}' using stored credentials.[/bold green]")
            return 
        except Exception:
            console.print("[bold yellow]Stored credentials missing or invalid. Starting device authentication...[/bold yellow]")

        try:
            trakt.init(client_id=self.client_id, client_secret=self.client_secret, store=True)
            self.authenticated_username = User('me').username
            console.print("[bold green]Trakt.tv authentication successful![/bold green]")
            
        except Exception as e:
            console.print(f"[bold red]Trakt.tv authentication failed: {e}[/bold red]")
            raise

    def _get_access_token(self) -> Optional[str]:
        """
        Attempts to retrieve the access token from PyTrakt's config file.
        """
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    config = json.load(f)
                return config.get('OAUTH_TOKEN')
            except:
                pass
        return None

    def create_trakt_list_raw(self, list_name: str, description: str, privacy: str) -> Optional[SimpleNamespace]:
        """
        Creates a new Trakt.tv list using a direct API call with requests.
        Returns a SimpleNamespace object with list details if successful.
        """
        if not self.authenticated_username:
            self.authenticate()

        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Access token not found. Please re-authenticate.")

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
            'trakt-api-version': '2',
            'trakt-api-key': self.client_id
        }
        
        data = {
            'name': list_name,
            'description': description,
            'privacy': privacy,
            'display_numbers': False,
            'allow_comments': True
        }

        # Use 'me' instead of username to avoid formatting issues
        url = f"https://api.trakt.tv/users/me/lists"
        console.print(f"[bold yellow]Creating list '{list_name}' on Traakt.tv via direct API...[/bold yellow]")

        retries = 5
        for attempt in range(retries):
            try:
                response = requests.post(url, headers=headers, data=json.dumps(data))
                response.raise_for_status()
                list_data = response.json()
                console.print(f"[bold green]List '{list_name}' created successfully via direct API! (Slug: {list_data['ids']['slug']})[/bold green]")

                return SimpleNamespace(
                    trakt_id=list_data['ids']['trakt'],
                    name=list_data['name'],
                    slug=list_data['ids']['slug'],
                    username=self.authenticated_username
                )
            except requests.exceptions.RequestException as e:
                status = e.response.status_code if e.response is not None else None
                if status == 429:
                    wait_time = int(e.response.headers.get('Retry-After', 10)) + 1
                    console.print(f"[bold yellow]Rate limit (429) creating list. Waiting {wait_time}s before retry {attempt + 1}/{retries}...[/bold yellow]")
                    time.sleep(wait_time)
                elif status == 420:
                    wait_time = 60 * (attempt + 1)
                    console.print(f"[bold yellow]Account limit (420) creating list. Waiting {wait_time}s before retry {attempt + 1}/{retries}...[/bold yellow]")
                    time.sleep(wait_time)
                else:
                    console.print(f"[bold red]Failed to create list '{list_name}' via direct API: {e}[/bold red]")
                    if e.response is not None:
                        console.print(f"[bold red]Response: {e.response.text}[/bold red]")
                    raise
        raise Exception(f"Failed to create list after {retries} retries due to rate limiting.")

    def update_list_metadata(self, user_list: SimpleNamespace, new_name: str = None, new_description: str = None):
        """
        Updates the name and/or description of an existing Trakt list.
        """
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Access token not found. Please re-authenticate.")

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
            'trakt-api-version': '2',
            'trakt-api-key': self.client_id
        }

        data = {}
        if new_name is not None:
            data['name'] = new_name
        if new_description is not None:
            data['description'] = new_description

        if not data:
            return  # Nothing to update

        url = f"https://api.trakt.tv/users/me/lists/{user_list.trakt_id}"

        try:
            response = requests.put(url, headers=headers, data=json.dumps(data))
            response.raise_for_status()
            if new_name and new_description:
                console.print(f"[bold green]List updated: '{new_name}' with new description[/bold green]")
            elif new_name:
                console.print(f"[bold green]List name updated to '{new_name}'[/bold green]")
            elif new_description:
                console.print(f"[bold green]List description updated[/bold green]")
            # Update the user_list object
            if new_name:
                user_list.name = new_name
        except requests.exceptions.RequestException as e:
            console.print(f"[bold yellow]Warning: Could not update list metadata: {e}[/bold yellow]")
            # Non-fatal - continue with sync

    def get_list_items(self, user_list: SimpleNamespace) -> List[Dict]:
        """
        Fetches the current items in the Trakt list.
        Returns a list of dictionaries with 'trakt_id' keys.
        """
        console.print(f"[bold yellow]Fetching existing items from list '{user_list.name}'...[/bold yellow]")
        
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Access token not found. Please re-authenticate.")

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
            'trakt-api-version': '2',
            'trakt-api-key': self.client_id
        }

        # URL to get list items
        url = f"https://api.trakt.tv/users/me/lists/{user_list.trakt_id}/items/movies"
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            items = response.json()
            return items
        except requests.exceptions.RequestException as e:
            console.print(f"[bold red]Failed to fetch list items for '{user_list.name}': {e}[/bold red]")
            if e.response is not None:
                console.print(f"[bold red]Response: {e.response.text}[/bold red]")
            return []

    def add_movies_to_list(self, user_list: SimpleNamespace, movies: list):
        """
        Adds movies to the specified list.
        :param user_list: A SimpleNamespace object with list details (trakt_id, name).
        :param movies: A list of trakt.movies.Movie objects.
        """
        console.print(f"[bold yellow]Adding {len(movies)} movies to list '{user_list.name}'...[/bold yellow]")
        
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Access token not found. Please re-authenticate.")

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
            'trakt-api-version': '2',
            'trakt-api-key': self.client_id
        }
        
        # Separate movies and shows
        import trakt.tv
        movies_to_add_data = []
        shows_to_add_data = []

        for idx, item in enumerate(movies):
            tid = None
            if hasattr(item, 'trakt'):
                 tid = item.trakt
            elif hasattr(item, 'ids'):
                ids = item.ids
                if 'ids' in ids:
                    ids = ids['ids']

                if 'trakt' in ids:
                    tid = ids['trakt']
            elif hasattr(item, 'trakt_id'):
                tid = item.trakt_id

            if tid:
                item_data = {
                    'ids': {'trakt': tid},
                    'rank': idx + 1  # 1-based rank for Trakt API
                }

                # Detect if it's a TV show or movie
                if isinstance(item, trakt.tv.TVShow):
                    shows_to_add_data.append(item_data)
                else:
                    movies_to_add_data.append(item_data)

        # Chunk all items into batches of 2 to avoid hitting API limits
        # Trakt has strict rate limits: 1 POST request per second
        chunk_size = 2
        total_items_data = []

        # Combine movies and shows with their types
        for item in movies_to_add_data:
            total_items_data.append(('movie', item))
        for item in shows_to_add_data:
            total_items_data.append(('show', item))

        total_added_movies = 0
        total_added_shows = 0
        total_existing_movies = 0
        total_existing_shows = 0


        for i in range(0, len(total_items_data), chunk_size):
            chunk_items = total_items_data[i:i + chunk_size]

            # Separate chunk into movies and shows
            chunk_movies = [item[1] for item in chunk_items if item[0] == 'movie']
            chunk_shows = [item[1] for item in chunk_items if item[0] == 'show']

            data = {}
            if chunk_movies:
                data['movies'] = chunk_movies
            if chunk_shows:
                data['shows'] = chunk_shows

            url = f"https://api.trakt.tv/users/me/lists/{user_list.trakt_id}/items"
            console.print(f"[bold yellow]Adding batch {i//chunk_size + 1}/{(len(total_items_data) + chunk_size - 1)//chunk_size}: {len(chunk_items)} items to list '{user_list.name}'...[/bold yellow]")

            retries = 5
            for attempt in range(retries):
                try:
                    response = requests.post(url, headers=headers, data=json.dumps(data))
                    response.raise_for_status()
                    add_items_data = response.json()
                    added_movies = add_items_data['added'].get('movies', 0)
                    added_shows = add_items_data['added'].get('shows', 0)
                    existing_movies = add_items_data['existing'].get('movies', 0)
                    existing_shows = add_items_data['existing'].get('shows', 0)
                    total_added_movies += added_movies
                    total_added_shows += added_shows
                    total_existing_movies += existing_movies
                    total_existing_shows += existing_shows
                    console.print(f"[bold green]Batch success! Added: {added_movies + added_shows}, Existing: {existing_movies + existing_shows}[/bold green]")
                    time.sleep(1.5)  # POST limit is 1 call/second
                    break # Success, exit retry loop
                except requests.exceptions.RequestException as e:
                    if e.response is not None and e.response.status_code in (420, 429):
                        wait_time = int(e.response.headers.get('Retry-After', 10)) + 1
                        console.print(f"[bold yellow]Rate limit hit ({e.response.status_code}). Waiting {wait_time}s before retry {attempt + 1}/{retries}...[/bold yellow]")
                        time.sleep(wait_time)
                    else:
                        console.print(f"[bold red]Failed to add batch to list '{user_list.name}' via direct API: {e}[/bold red]")
                        if e.response is not None:
                            console.print(f"[bold red]Response: {e.response.text}[/bold red]")
                        raise
            else:
                # If we exhausted retries without break
                console.print(f"[bold red]Failed to add batch after {retries} retries due to rate limiting.[/bold red]")
                raise Exception("Rate limit exceeded and retries exhausted.")
        
        total_added = total_added_movies + total_added_shows
        total_existing = total_existing_movies + total_existing_shows
        console.print(f"[bold green]Finished adding items! Total Added: {total_added} (Movies: {total_added_movies}, Shows: {total_added_shows}), Total Existing: {total_existing} (Movies: {total_existing_movies}, Shows: {total_existing_shows})[/bold green]")

    def remove_movies_from_list(self, user_list: SimpleNamespace, movies_to_remove: list):
        """
        Removes movies from the specified list.
        :param user_list: A SimpleNamespace object with list details (trakt_id, name).
        :param movies_to_remove: A list of dicts or objects with 'trakt_id' or 'ids'.
        """
        if not movies_to_remove:
            return

        console.print(f"[bold yellow]Removing {len(movies_to_remove)} movies from list '{user_list.name}'...[/bold yellow]")
        
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Access token not found. Please re-authenticate.")

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
            'trakt-api-version': '2',
            'trakt-api-key': self.client_id
        }
        
        # Prepare data for removal
        movies_data = []
        for m in movies_to_remove:
            tid = None
            if isinstance(m, dict):
                if 'movie' in m and 'ids' in m['movie']: # Structure returned by get_list_items
                    tid = m['movie']['ids']['trakt']
                elif 'ids' in m:
                    tid = m['ids']['trakt']
                elif 'trakt_id' in m:
                    tid = m['trakt_id']
            elif hasattr(m, 'trakt'):
                tid = m.trakt
            elif hasattr(m, 'ids'):
                 if 'trakt' in m.ids:
                     tid = m.ids['trakt']
            
            if tid:
                movies_data.append({'ids': {'trakt': tid}})

        # Chunk the movies into batches of 5
        chunk_size = 5
        total_removed = 0
        
        for i in range(0, len(movies_data), chunk_size):
            chunk = movies_data[i:i + chunk_size]
            data = {
                'movies': chunk
            }

            url = f"https://api.trakt.tv/users/me/lists/{user_list.trakt_id}/items/remove"
            console.print(f"[bold yellow]Removing batch of {len(chunk)} movies from list '{user_list.name}'...[/bold yellow]")

            try:
                response = requests.post(url, headers=headers, data=json.dumps(data))
                response.raise_for_status()
                remove_data = response.json()
                removed = remove_data['deleted']['movies']
                total_removed += removed
                console.print(f"[bold green]Batch removal success! Removed: {removed}[/bold green]")
                time.sleep(1) 
            except requests.exceptions.RequestException as e:
                console.print(f"[bold red]Failed to remove batch from list '{user_list.name}': {e}[/bold red]")
                if e.response is not None:
                    console.print(f"[bold red]Response: {e.response.text}[/bold red]")
                raise
        
        console.print(f"[bold green]Finished removing movies! Total Removed: {total_removed}[/bold green]")