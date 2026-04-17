import requests
from bs4 import BeautifulSoup
from rich.console import Console
from typing import List, Dict, Optional

console = Console()

def get_imdb_id_from_wikidata(allocine_id: str) -> Optional[str]:
    """
    Queries Wikidata to find the IMDb ID corresponding to an Allocine film ID.
    """
    url = "https://query.wikidata.org/sparql"
    query = f"""
    SELECT ?item ?imdbId WHERE {{
      ?item wdt:P1265 "{allocine_id}".
      OPTIONAL {{ ?item wdt:P345 ?imdbId. }}
    }}
    """
    
    # console.log(f"Querying Wikidata for Allocine ID: {allocine_id}")
    
    try:
        # User-Agent is often required for Wikidata SPARQL to avoid 403
        headers = {'User-Agent': 'TraktListAllocineBot/1.0 (test@example.com)'} 
        response = requests.get(url, params={'query': query, 'format': 'json'}, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        results = data.get('results', {}).get('bindings', [])
        if results:
            for result in results:
                if 'imdbId' in result:
                    return result['imdbId']['value']
    except Exception as e:
        console.log(f"[bold red]Wikidata query failed for {allocine_id}: {e}[/bold red]")
        
    return None

def _get_movie_details_from_page(movie_url: str) -> Dict:
    """
    Fetches an individual Allocine movie page and extracts additional details (actors).
    """
    details = {"actors": []}
    console.log(f"Fetching additional details from movie page: {movie_url}")
    try:
        response = requests.get(movie_url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        console.log(f"[bold red]Error fetching {movie_url} for details: {e}[/bold red]")
        return details
    
    soup = BeautifulSoup(response.text, 'html.parser')

    # Extract Actor Names
    actor_div = soup.find('div', class_='meta-body-item meta-body-actor')
    if actor_div:
        actor_links = actor_div.find_all('a', class_='dark-grey-link')
        for link in actor_links:
            details['actors'].append(link.get_text(strip=True))
    
    return details


def parse_allocine_page(url: str) -> List[Dict]:
    """
    Parses a single Allocine page to extract movie/series titles, Allocine IDs, and release years.
    Returns a list of dicts with a 'content_type' field indicating 'movie' or 'series'.
    """
    console.log(f"Fetching URL: {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for HTTP errors
    except requests.exceptions.RequestException as e:
        console.log(f"[bold red]Error fetching {url}: {e}[/bold red]")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')

    items = []
    item_cards = soup.find_all('div', class_='card entity-card entity-card-list cf')

    for card in item_cards:
        title_link = card.find('a', class_='meta-title-link')
        item_title = title_link.get_text(strip=True) if title_link else None
        item_url_suffix = title_link['href'] if title_link else None

        allocine_id = None
        full_item_url = None
        content_type = None

        # Detect if it's a movie or series
        if item_url_suffix:
            if 'cfilm=' in item_url_suffix:
                full_item_url = requests.compat.urljoin(url, item_url_suffix)
                allocine_id = full_item_url.split('cfilm=')[-1].split('.')[0]
                content_type = 'movie'
            elif 'cserie=' in item_url_suffix:
                full_item_url = requests.compat.urljoin(url, item_url_suffix)
                allocine_id = full_item_url.split('cserie=')[-1].split('.')[0]
                content_type = 'series'

        # Extract release year
        release_year = None
        date_span = card.find('span', class_='date')
        if date_span:
            import re
            match = re.search(r'\b\d{4}\b', date_span.get_text(strip=True))
            if match:
                release_year = match.group(0)

        # Extract Original Title
        original_title = None
        meta_items = card.find_all('div', class_='meta-body-item')
        for item in meta_items:
            light_span = item.find('span', class_='light')
            if light_span and 'Titre original' in light_span.get_text():
                dark_span = item.find('span', class_='dark-grey')
                if dark_span:
                    original_title = dark_span.get_text(strip=True)
                break
        
        # Extract Directors
        directors = []
        direction_div = card.find('div', class_='meta-body-item meta-body-direction')
        if direction_div:
            # Directors are usually links with class 'dark-grey-link' or 'blue-link'
            # or sometimes just text if not linked.
            # They follow the "De" span.
            director_links = direction_div.find_all('a', class_='dark-grey-link')
            for link in director_links:
                directors.append(link.get_text(strip=True))
            
            # Fallback: if no links, try to get text after "De"
            if not directors:
                 text = direction_div.get_text(strip=True)
                 if "De" in text:
                     # "De Director Name" -> "Director Name"
                     directors = [d.strip() for d in text.split("De")[-1].split('|')[0].strip().split(',') if d.strip()]


        # Extract actors/cast from individual page (if needed)
        actors = []
        if full_item_url and content_type == 'movie':
            item_details = _get_movie_details_from_page(full_item_url)
            actors = item_details.get('actors', [])


        if item_title and allocine_id and content_type:
            items.append({
                "title": item_title,
                "allocine_id": allocine_id,
                "release_year": release_year,
                "original_title": original_title,
                "directors": directors,
                "actors": actors,
                "content_type": content_type
            })
    return items

def get_all_movies(base_url: str, max_movies: int = None) -> List[Dict]:
    """
    Fetches movies from Allocine pages until max_movies is reached or no more pages exist.
    If max_movies is None, fetches all available movies.
    """
    all_movies = []
    current_page = 1
    while True:
        # Stop if we've already fetched enough movies
        if max_movies is not None and len(all_movies) >= max_movies:
            break

        page_url = f"{base_url}?page={current_page}" if current_page > 1 else base_url
        movies_on_page = parse_allocine_page(page_url)
        if not movies_on_page:
            break  # No more movies or an error occurred

        all_movies.extend(movies_on_page)
        current_page += 1

    # Trim to exact max_movies if specified
    if max_movies is not None and len(all_movies) > max_movies:
        all_movies = all_movies[:max_movies]

    return all_movies

def get_allocine_list_title(url: str) -> Optional[str]:
    """
    Extracts the main title/heading of the Allocine list page.
    """
    console.log(f"Fetching list title from URL: {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        console.log(f"[bold red]Error fetching {url} for title: {e}[/bold red]")
        return None
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Try to get the main titlebar title first (e.g., "Films à l'affiche")
    titlebar_title_div = soup.find('div', class_='titlebar-title')
    if titlebar_title_div:
        # Check for h1 inside it or get all text
        h1_tag = titlebar_title_div.find('h1')
        if h1_tag:
            return h1_tag.get_text(strip=True)
        else:
            return ' '.join(titlebar_title_div.get_text(separator=' ', strip=True).split())

    # Fallback to the specific subtitle if a main titlebar-title isn't concise
    subtitle_span = soup.find('span', class_='titlebar-subtile-txt')
    if subtitle_span:
        return subtitle_span.get_text(strip=True)
    
    # As a last resort, use the <title> tag
    if soup.title and soup.title.string:
        return soup.title.string.replace(' - AlloCiné', '').strip()
        
    return None