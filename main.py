import os
import unicodedata
import typer
from rich.console import Console
from rich.table import Table
import trakt.movies
import trakt.tv
from trakt.users import User
import trakt.sync
from types import SimpleNamespace
from typing import List, Dict
from datetime import datetime, timezone

from allocine import get_all_movies, get_allocine_list_title, get_imdb_id_from_wikidata
from trakt_client import TraktClient

console = Console()
app = typer.Typer()

TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "")
TRAKT_CLIENT_SECRET = os.environ.get("TRAKT_CLIENT_SECRET", "")

# --- Helper Functions for Matching ---

def _strip_accents(text: str) -> str:
    """Remove diacritical marks from a string for fuzzy comparison."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


_TITLE_STOPWORDS = {'the', 'a', 'an', 'de', 'le', 'la', 'les', 'l', 'un', 'une', 'du'}

def _titles_overlap(query: str, candidate: str) -> bool:
    """True if query and candidate share enough tokens in both directions (prevents wrong-title matches)."""
    q_tokens = set(_strip_accents(query.lower()).split()) - _TITLE_STOPWORDS
    c_tokens = set(_strip_accents(candidate.lower()).split()) - _TITLE_STOPWORDS
    if not q_tokens or not c_tokens:
        return True
    overlap = q_tokens & c_tokens
    return (len(overlap) / len(q_tokens) >= 0.5 and
            len(overlap) / len(c_tokens) >= 0.5)


def _verify_candidate(candidate, allocine_movie_data):
    """
    Verifies a Trakt candidate against Allocine data using Year and Director.
    Returns True if it's a valid match, False otherwise.
    """
    # 1. Year Verification (Strict: +/- 1 year)
    if allocine_movie_data['release_year'] and candidate.year:
        target_year = int(allocine_movie_data['release_year'])
        if abs(candidate.year - target_year) > 1:
            return False
    elif not allocine_movie_data['release_year'] and candidate.year:
        # No year from Allocine - be conservative
        # Reject very old content (likely mismatch for modern series/movies)
        from datetime import datetime
        current_year = datetime.now().year
        if candidate.year < current_year - 5:
            console.log(f"  [yellow]Rejecting '{candidate.title}' ({candidate.year}): Too old without year verification (likely mismatch)[/yellow]")
            return False

    # 2. Director Verification
    allocine_directors = allocine_movie_data.get('directors', [])
    if not allocine_directors:
        # No directors to verify - only accept if year matched or was verified above
        if allocine_movie_data['release_year']:
            return True # Year was verified
        else:
            # No year AND no directors - require exact title match for safety
            allocine_title = _strip_accents(allocine_movie_data.get('title', '').lower().strip())
            candidate_title = _strip_accents(candidate.title.lower().strip()) if hasattr(candidate, 'title') and candidate.title else ''
            # Accept if titles are very similar
            if allocine_title in candidate_title or candidate_title in allocine_title:
                return True
            console.log(f"  [yellow]Rejecting '{candidate.title}': No year/director data and title doesn't match closely[/yellow]")
            return False

    try:
        # Fetch full movie info to get crew
        # Use the candidate directly. Accessing .people will lazy-load the data correctly using its internal IDs.
        full_tm = candidate
        
        # Accessing .people will lazy-load the data
        trakt_directors = []
        if full_tm.people:
            # PyTrakt returns people as a flat list of Person objects
            for p in full_tm.people:
                if p is None:
                    continue
                if hasattr(p, 'job') and p.job and p.job.lower() == 'director':
                    if hasattr(p, 'name') and p.name:
                        trakt_directors.append(p.name.lower())
        
        if not trakt_directors:
            # Trakt has no director info -- require title similarity to avoid false positives
            allocine_title = _strip_accents(allocine_movie_data.get('title', '').lower().strip())
            original_title = _strip_accents(allocine_movie_data.get('original_title', '').lower().strip()) if allocine_movie_data.get('original_title') else ''
            candidate_title = _strip_accents(candidate.title.lower().strip()) if hasattr(candidate, 'title') and candidate.title else ''
            if (allocine_title and (allocine_title in candidate_title or candidate_title in allocine_title)) or \
               (original_title and (original_title in candidate_title or candidate_title in original_title)):
                return True
            console.log(f"  [yellow]Rejecting '{candidate.title}': No director data on Trakt and title doesn't match closely enough[/yellow]")
            return False

        for ad in allocine_directors:
            ad_clean = _strip_accents(ad.lower().strip())
            # Extract last name (usually more reliable than first name/nicknames)
            ad_parts = ad_clean.split()
            ad_last_name = ad_parts[-1] if ad_parts else ad_clean

            for td in trakt_directors:
                td_clean = _strip_accents(td.strip())
                td_parts = td_clean.split()
                td_last_name = td_parts[-1] if td_parts else td_clean

                # Match if: full name substring match OR last names match
                if ad_clean in td_clean or td_clean in ad_clean or ad_last_name == td_last_name:
                    return True
                # Handle reversed name order (East Asian names) and accent edge cases
                ad_tokens = frozenset(ad_clean.replace('-', ' ').split())
                td_tokens = frozenset(td_clean.replace('-', ' ').split())
                if ad_tokens == td_tokens:
                    return True

        # If we checked all and found no match
        console.log(f"  [yellow]Rejecting '{candidate.title}' ({candidate.year}): Director mismatch. Allocine: {allocine_directors}, Trakt: {trakt_directors}[/yellow]")
        return False

    except Exception as e:
        console.log(f"  [yellow]Error verifying director for '{candidate.title}': {e}. Falling back to title match.[/yellow]")
        # Director lookup failed (e.g. PyTrakt parsing error) -- fall back to title similarity
        allocine_title = _strip_accents(allocine_movie_data.get('title', '').lower().strip())
        original_title = _strip_accents(allocine_movie_data.get('original_title', '').lower().strip()) if allocine_movie_data.get('original_title') else ''
        candidate_title = _strip_accents(candidate.title.lower().strip()) if hasattr(candidate, 'title') and candidate.title else ''
        if (allocine_title and (allocine_title in candidate_title or candidate_title in allocine_title)) or \
           (original_title and (original_title in candidate_title or candidate_title in original_title)):
            return True
        return False


def _trakt_search_with_retry(search_fn, *args, **kwargs):
    """Calls a PyTrakt search function, retrying on rate limit errors."""
    import time
    retries = 5
    for attempt in range(retries):
        try:
            return search_fn(*args, **kwargs)
        except Exception as e:
            if 'rate limit' in str(e).lower():
                wait_time = 30 * (attempt + 1)
                console.log(f"  [yellow]Trakt GET rate limit. Waiting {wait_time}s (attempt {attempt + 1}/{retries})...[/yellow]")
                time.sleep(wait_time)
            else:
                raise
    return []


def _search_and_verify(search_query: str, allocine_movie_data: Dict, search_type: str):
    """
    Performs a Trakt search and verifies candidates.
    """
    if not search_query:
        return None

    console.log(f"  Attempting {search_type} search for '{search_query}' (Year: {allocine_movie_data['release_year']})...")
    try:
        results = _trakt_search_with_retry(trakt.movies.Movie.search, search_query, year=allocine_movie_data['release_year'])

        for candidate in results:
            if not _verify_candidate(candidate, allocine_movie_data):
                continue
            candidate_title = getattr(candidate, 'title', '') or ''
            if not _titles_overlap(search_query, candidate_title):
                continue
            return candidate

    except Exception as e:
        console.log(f"  [red]Search error for '{search_query}' ({search_type}): {e}[/red]")

    return None

def _search_and_verify_series(search_query: str, allocine_series_data: Dict, search_type: str):
    """
    Performs a Trakt TV show search and verifies candidates.
    """
    if not search_query:
        return None

    console.log(f"  Attempting {search_type} search for '{search_query}' (Year: {allocine_series_data['release_year']})...")
    try:
        results = _trakt_search_with_retry(trakt.tv.TVShow.search, search_query, year=allocine_series_data['release_year'])

        for candidate in results:
            if not _verify_candidate(candidate, allocine_series_data):
                continue
            candidate_title = getattr(candidate, 'title', '') or ''
            if not _titles_overlap(search_query, candidate_title):
                continue
            return candidate

    except Exception as e:
        console.log(f"  [red]Search error for '{search_query}' ({search_type}): {e}[/red]")

    return None

def _search_by_wikidata(allocine_movie_data: Dict):
    """
    Queries Wikidata for IMDb ID and searches Trakt by that ID.
    """
    console.log(f"  Attempting Wikidata lookup for Allocine ID: {allocine_movie_data['allocine_id']}...")
    imdb_id = get_imdb_id_from_wikidata(allocine_movie_data['allocine_id'])

    if imdb_id:
        console.log(f"  Found IMDb ID {imdb_id}. Searching Trakt...")
        try:
            results = trakt.sync.search_by_id(imdb_id, id_type='imdb')
            if results:
                candidate = results[0]
                # Check if it's a movie or TV show
                if isinstance(candidate, trakt.movies.Movie) or isinstance(candidate, trakt.tv.TVShow):
                    # IMDb ID is a precise identifier — only verify year, not director/title.
                    # Director/title checks are redundant here and fail when Trakt has no crew data.
                    if allocine_movie_data['release_year'] and candidate.year:
                        if abs(candidate.year - int(allocine_movie_data['release_year'])) <= 1:
                            return candidate
                    else:
                        return candidate
        except Exception as e:
            console.log(f"  [red]Trakt search by IMDb ID failed for {imdb_id}: {e}[/red]")
    else:
        console.log(f"  No IMDb ID found on Wikidata for Allocine ID {allocine_movie_data['allocine_id']}.")
    
    return None

# --- Main CLI Commands ---

@app.command()
def authenticate():
    """
    Authenticates with Trakt.tv using device authentication.
    """
    if TRAKT_CLIENT_ID == "YOUR_CLIENT_ID" or TRAKT_CLIENT_SECRET == "YOUR_CLIENT_SECRET":
        console.print("[bold red]Please replace credentials![/bold red]")
        raise typer.Exit(code=1)

    try:
        trakt_client = TraktClient(TRAKT_CLIENT_ID, TRAKT_CLIENT_SECRET)
        trakt_client.authenticate()
        console.print("[bold green]Authentication successful.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Authentication failed: {e}[/bold red]")
        raise typer.Exit(code=1)

@app.command("scrape-allocine")
def scrape_allocine(
    url: str = typer.Argument(..., help="The URL of the Allocine playlist to scrape."),
    max_movies: int = typer.Option(None, "--max-movies", "-m", help="Maximum number of movies to scrape. If not specified, scrapes all available movies."),
):
    """
    Scrapes a movie list from Allocine.fr and displays the results.
    """
    limit_msg = f"up to {max_movies} movies" if max_movies else "all available movies"
    console.print(f"[bold blue]Scraping movies from Allocine URL: {url} ({limit_msg})[/bold blue]")
    movies = get_all_movies(url, max_movies)

    if movies:
        table = Table(title="Scraped Movies from Allocine")
        table.add_column("Title", style="cyan", no_wrap=True)
        table.add_column("Original", style="blue")
        table.add_column("Directors", style="yellow")
        table.add_column("Allocine ID", style="magenta")
        table.add_column("Year", style="green")

        for movie in movies:
            directors = ", ".join(movie.get('directors', []))
            table.add_row(movie["title"], movie["original_title"] or "-", directors, movie["allocine_id"], movie["release_year"] or "N/A")
        
        console.print(table)
    else:
        console.print("[bold yellow]No movies found or an error occurred during scraping.[/bold yellow]")

@app.command("sync-to-trakt")
def sync_to_trakt(
    allocine_url: str = typer.Argument(..., help="The Allocine playlist URL to scrape and sync."),
    max_movies: int = typer.Option(25, "--max-movies", "-m", help="Maximum number of movies to scrape and sync. Defaults to 25."),
    private: bool = typer.Option(False, "--private", "-pr", help="Make the Trakt list private."),
    description: str = typer.Option("Movies synced from Allocine.fr", "--description", "-d", help="Description for the Trakt.tv list."),
):
    """
    Scrapes movies from Allocine.fr and syncs them to a Trakt.tv list.
    """
    trakt_client = TraktClient(TRAKT_CLIENT_ID, TRAKT_CLIENT_SECRET)

    console.print("[bold blue]Attempting to authenticate with Trakt.tv...[/bold blue]")
    try:
        trakt_client.authenticate()
    except Exception:
        console.print("[bold red]Authentication failed.[/bold red]")
        raise typer.Exit(code=1)

    allocine_list_title = get_allocine_list_title(allocine_url)
    if not allocine_list_title:
        console.print("[bold red]Could not extract list title from Allocine URL. Aborting.[/bold red]")
        raise typer.Exit(code=1)

    trakt_list_name = f"Allocine - {allocine_list_title}"

    limit_msg = f"{max_movies} unique matched movies" if max_movies else "all available movies"
    console.print(f"[bold blue]Syncing movies from Allocine URL: {allocine_url} (target: {limit_msg})[/bold blue]")

    # Iteratively fetch pages until we have enough unique matched movies
    matched_pairs = []
    unmatched_movies = []
    seen_trakt_ids = set()
    ordered_unique_trakt_movies = []
    current_page = 1
    total_scraped = 0

    with console.status("[bold green]Fetching and matching movies...[/bold green]", spinner="dots"):
        while True:
            # Check if we have enough unique matches
            if max_movies and len(ordered_unique_trakt_movies) >= max_movies:
                console.log(f"[bold green]Reached target of {max_movies} unique matched movies[/bold green]")
                break

            # Fetch next page
            page_url = f"{allocine_url}?page={current_page}" if current_page > 1 else allocine_url
            from allocine import parse_allocine_page
            page_movies = parse_allocine_page(page_url)

            if not page_movies:
                console.log(f"[bold yellow]No more movies found (page {current_page})[/bold yellow]")
                break

            total_scraped += len(page_movies)
            console.log(f"[bold blue]Page {current_page}: Found {len(page_movies)} movies (total scraped: {total_scraped})[/bold blue]")

            # Match movies from this page
            for movie_data in page_movies:
                # Stop if we have enough
                if max_movies and len(ordered_unique_trakt_movies) >= max_movies:
                    break

                content_type = movie_data.get('content_type', 'movie')
                console.log(f"Processing [{content_type}]: [cyan]{movie_data['title']} ({movie_data['release_year']})[/cyan]")

                best_match = None
                match_method = None

                # Choose search functions based on content type
                if content_type == 'series':
                    # Series-specific matching
                    # 1. Original Title Search + Verification
                    if movie_data.get('original_title') and movie_data['original_title'] != movie_data['title']:
                        best_match = _search_and_verify_series(movie_data['original_title'], movie_data, "Original Title")
                        if best_match: match_method = "Original Title"

                    # 2. Wikidata Fallback (if Original failed)
                    if not best_match:
                        best_match = _search_by_wikidata(movie_data)
                        if best_match: match_method = "Wikidata/IMDb"

                    # 3. Title Search + Verification (Last Resort)
                    if not best_match:
                        best_match = _search_and_verify_series(movie_data['title'], movie_data, "Title")
                        if best_match: match_method = "Title"
                else:
                    # Movie-specific matching
                    # 1. Original Title Search + Verification
                    if movie_data.get('original_title') and movie_data['original_title'] != movie_data['title']:
                        best_match = _search_and_verify(movie_data['original_title'], movie_data, "Original Title")
                        if best_match: match_method = "Original Title"

                    # 2. Wikidata Fallback (if Original failed)
                    if not best_match:
                        best_match = _search_by_wikidata(movie_data)
                        if best_match: match_method = "Wikidata/IMDb"

                    # 3. French Title Search + Verification (Last Resort)
                    if not best_match:
                        best_match = _search_and_verify(movie_data['title'], movie_data, "French Title")
                        if best_match: match_method = "French Title"

                # Result
                if best_match:
                    matched_pairs.append((movie_data, best_match))

                    # Add to unique list if not duplicate
                    if hasattr(best_match, 'trakt'):
                        tid = best_match.trakt
                    elif hasattr(best_match, 'ids') and 'trakt' in best_match.ids:
                        tid = best_match.ids['trakt']
                    else:
                        tid = None

                    if tid and tid not in seen_trakt_ids:
                        seen_trakt_ids.add(tid)
                        ordered_unique_trakt_movies.append(best_match)
                        console.log(f"  Matched ({len(ordered_unique_trakt_movies)}/{max_movies if max_movies else '?'}): [cyan]{movie_data['title']}[/cyan] -> [green]{best_match.title} ({best_match.year})[/green] via [bold]{match_method}[/bold]")
                    else:
                        console.log(f"  Duplicate: [cyan]{movie_data['title']}[/cyan] -> [yellow]{best_match.title}[/yellow]")
                else:
                    console.log(f"[bold red]Unmatched: {movie_data['title']} ({movie_data['release_year']})[/bold red]")
                    unmatched_movies.append(movie_data)

            current_page += 1

    if not ordered_unique_trakt_movies:
        console.print("[bold yellow]No matching movies found. Aborting sync.[/bold yellow]")
        raise typer.Exit()

    console.print(f"[bold green]Collected {len(ordered_unique_trakt_movies)} unique matched movies from {total_scraped} scraped[/bold green]")
    trakt_movie_objects = ordered_unique_trakt_movies

    # Update list name with movie count
    trakt_list_name = f"Allocine - {allocine_list_title} ({len(trakt_movie_objects)})"

    # Sync to List
    existing_list = None
    try:
        me = User('me')
        for lst in me.lists:
            # Match by exact name (including count) to allow multiple lists with different counts
            if lst.name == trakt_list_name:
                existing_list = lst
                break
    except Exception as e:
        console.print(f"[bold red]Warning: Could not fetch existing lists: {e}[/bold red]")

    target_list_obj = None
    
    if existing_list:
        # Safer ID access
        trakt_id = None
        slug = getattr(existing_list, 'slug', None)
        if hasattr(existing_list, 'ids') and existing_list.ids:
            trakt_id = existing_list.ids.get('trakt')
            if not slug: slug = existing_list.ids.get('slug')
        if not trakt_id: trakt_id = slug

        console.print(f"[bold green]Found existing list (ID: {trakt_id}). Syncing...[/bold green]")

        target_list_obj = SimpleNamespace(
            trakt_id=trakt_id,
            name=existing_list.name,
            slug=slug,
            username=me.username
        )

        # Update list metadata (name and description with timestamp)
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        updated_description = f"{description}\n\nLast updated: {timestamp}"

        needs_update = existing_list.name != trakt_list_name
        trakt_client.update_list_metadata(
            target_list_obj,
            new_name=trakt_list_name if needs_update else None,
            new_description=updated_description
        )
    else:
        # Add timestamp to description for new lists
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        updated_description = f"{description}\n\nLast updated: {timestamp}"
        target_list_obj = trakt_client.create_trakt_list_raw(trakt_list_name, description=updated_description, privacy='private' if private else 'public')

    if target_list_obj:
        # Full Sync Logic (Add & Remove)
        current_trakt_items = trakt_client.get_list_items(target_list_obj)
        current_trakt_ids = set()
        for item in current_trakt_items:
            if 'movie' in item and 'ids' in item['movie']:
                current_trakt_ids.add(item['movie']['ids']['trakt'])
        
        scraped_trakt_ids = set(seen_trakt_ids) # Already collected above

        ids_to_remove = current_trakt_ids - scraped_trakt_ids
        movies_to_remove = [item for item in current_trakt_items if item['movie']['ids']['trakt'] in ids_to_remove]
        
        if movies_to_remove:
            trakt_client.remove_movies_from_list(target_list_obj, movies_to_remove)
        else:
            console.print("[bold green]No movies to remove.[/bold green]")

        trakt_client.add_movies_to_list(target_list_obj, trakt_movie_objects)
        
        console.print(f"[bold green]Sync Complete![/bold green]")

        console.print("\n[bold underline]Summary[/bold underline]")
        console.print(f"Scraped:   {total_scraped}")
        console.print(f"Matched:   {len(matched_pairs)}")
        console.print(f"Unique:    {len(trakt_movie_objects)}")
        console.print(f"Removed:   {len(movies_to_remove)}")
        console.print(f"Unmatched: {len(unmatched_movies)}")
        
        if unmatched_movies:
            table = Table(title="Unmatched Movies")
            table.add_column("Title", style="red")
            table.add_column("Allocine ID", style="magenta")
            for m in unmatched_movies:
                table.add_row(m['title'], m['allocine_id'])
            console.print(table)

    else:
        console.print("[bold red]Failed to get/create list.[/bold red]")
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()