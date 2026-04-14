"""
Create Plex- or Jellyfin-compatible folders from IMDb links / TMDB.

Reads a text file with IMDb URLs (or tt-IDs), queries the OMDb API for
title and year, and creates media-server-compatible folders.

Plex preset:
    MovieTitle (Year) {imdb-ttXXXXXXX}
    ShowTitle (Year) {tmdb-XXXXX}

Jellyfin preset (IDs configurable):
    MovieTitle (Year) [imdbid-ttXXXXXXX]   or [tmdbid-XXXXX]
    ShowTitle (Year) [tmdbid-XXXXX]        or [imdbid-ttXXXXXXX]

Usage:
    medianame                        # Interactive mode (enter titles)
    medianame <title>                # Direct search
    medianame -n <title>             # Dry run (show only, no changes)
    medianame -i <title>             # Interactive: dry run + confirmation
    medianame -o /path/to/movies     # Override target path (movies + series)
    medianame -f movies.txt          # Use a different input file
    medianame -p                     # Prompt for titles (skip input file)
    medianame --preset jellyfin ...  # Override naming preset for this run
    medianame scan [<path>]          # Scan a folder and move/copy raw media
                                     # into named library folders. After a
                                     # successful move, the source folder is
                                     # cleaned up automatically.
    medianame scan --copy <path>     # Scan and copy instead of move
    medianame scan --max-age-days N  # Restrict scan to entries from the last N days
    medianame setup                  # (Re)configure API keys, paths, preset
    # When the input file is empty, prompt mode starts automatically.
"""

import argparse
import os
import re
import shutil
import time

import requests


# --- Scan feature constants ---

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".sub", ".idx", ".vtt"}
# Minimum video size for `scan`. Overridden from config at runtime.
MIN_VIDEO_SIZE_MB = 500
MIN_VIDEO_BYTES = MIN_VIDEO_SIZE_MB * 1024 * 1024

# Max folder recursion depth when collecting media files under a scan item.
# Scene releases keep videos at depth 0-1; TV packs at depth 0-2 at most.
SCAN_MAX_DEPTH = 2

# Default --max-age-days for `scan` (0 = no limit). Loaded from config.
SCAN_MAX_AGE_DAYS = 0

# Folders whose name matches one of these (case-insensitive, exact match)
# are skipped entirely during scan. Users can extend this list via the
# `scan_ignore` config field.
DEFAULT_SCAN_IGNORE = {
    "#recycle", "@eadir", ".trash", ".trashes", ".fseventsd",
    ".spotlight-v100", ".ds_store", "lost+found",
    "system volume information", "$recycle.bin", "recycle.bin",
}
SCAN_IGNORE = set(DEFAULT_SCAN_IGNORE)

# Regex: folder names already containing a medianame ID tag are treated as
# existing library entries and never re-scanned.
_LIBRARY_TAG_RE = re.compile(r"\{(?:imdb|tmdb)-[^}]+\}|\[(?:imdbid|tmdbid)-[^\]]+\]")


# --- Configuration (loaded at startup from ~/.config/plexname/config.json) ---

API_KEY = None
TMDB_TOKEN = None
MOVIE_PATH = None
SERIES_PATH = None
INPUT_FILE = "movies.txt"

# Naming configuration (loaded from config.json; defaults = Plex)
NAMING_PRESET = "plex"        # "plex" | "jellyfin"
MOVIE_ID_SOURCE = "imdb"      # "imdb" | "tmdb"  (only used when preset=jellyfin)
SERIES_ID_SOURCE = "tmdb"     # "imdb" | "tmdb"  (only used when preset=jellyfin)
DEFAULT_OPERATION = "move"    # "move" | "copy"  (for `medianame scan`)


MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds between retries

# Cache for fetched data (avoids duplicate API calls)
_movie_cache = {}
_tmdb_cache = {}


def get_movie_data(imdb_id):
    """
    Fetch movie data from the OMDb API (with retry on transient errors).

    Args:
        imdb_id: IMDb ID (e.g. tt0133093)

    Returns:
        dict with Title, Year etc. on success, None otherwise.
    """
    if imdb_id in _movie_cache:
        return _movie_cache[imdb_id]
    url = f"https://www.omdbapi.com/?i={imdb_id}&apikey={API_KEY}"
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=10)
            data = response.json()
            if data.get("Response") in ("True", "False"):
                return data
            last_error = data.get("Error", "Unknown API error")
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES:
                print(f"  ⚠️ Attempt {attempt}/{MAX_RETRIES} failed, retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"Error for {imdb_id} after {MAX_RETRIES} attempts: {last_error}")
                return None
    return None


def format_folder_name(title, year, id_type, id_value, preset="plex"):
    """
    Build a media-server-compatible folder name.

    Args:
        title: Title (already sanitized of invalid path chars).
        year: 4-digit year string.
        id_type: "imdb" or "tmdb".
        id_value: ID string (e.g. "tt1375666" or "1396").
        preset: "plex" (uses {}) or "jellyfin" (uses [] and -id- suffix).

    Returns:
        Folder name string, e.g.:
            Plex:     "Inception (2010) {imdb-tt1375666}"
            Jellyfin: "Inception (2010) [imdbid-tt1375666]"
    """
    if preset == "jellyfin":
        tag = f"[{id_type}id-{id_value}]"
    else:
        tag = f"{{{id_type}-{id_value}}}"
    return f"{title} ({year}) {tag}"


def _tmdb_request(endpoint, params=None):
    """
    Make an authenticated TMDB API request.

    Args:
        endpoint: API path (e.g. /search/multi)
        params: Query parameters as dict

    Returns:
        JSON response as dict.
    """
    url = f"https://api.themoviedb.org/3{endpoint}"
    headers = {
        "Authorization": f"Bearer {TMDB_TOKEN}",
        "accept": "application/json",
    }
    response = requests.get(url, headers=headers, params=params or {}, timeout=10)
    return response.json()


def get_tmdb_details(tmdb_id, media_type):
    """
    Fetch detail data (title, year, cast) from TMDB.

    Args:
        tmdb_id: TMDB ID (e.g. 1396)
        media_type: "tv" or "movie"

    Returns:
        dict with Response, Title, Year, Actors (and imdbID for movies).
        None on error.
    """
    cache_key = f"{media_type}-{tmdb_id}"
    if cache_key in _tmdb_cache:
        return _tmdb_cache[cache_key]

    endpoint = f"/{'tv' if media_type == 'tv' else 'movie'}/{tmdb_id}"
    try:
        # external_ids gives us the IMDb ID for TV shows (Jellyfin needs it)
        data = _tmdb_request(endpoint, {"append_to_response": "credits,external_ids"})
    except Exception as e:
        print(f"  ❌ TMDB error: {e}")
        return None

    if "id" not in data:
        return None

    cast = data.get("credits", {}).get("cast", [])
    actors = ", ".join(c["name"] for c in cast[:2]) if cast else "N/A"

    if media_type == "tv":
        # For TV shows, TMDB returns the IMDb ID under external_ids
        imdb_id = data.get("external_ids", {}).get("imdb_id", "") or ""
        result = {
            "Response": "True",
            "Title": data.get("name", ""),
            "Year": (data.get("first_air_date") or "")[:4],
            "Actors": actors,
            "Seasons": data.get("number_of_seasons"),
            "imdbID": imdb_id,
        }
    else:
        result = {
            "Response": "True",
            "Title": data.get("title", ""),
            "Year": (data.get("release_date") or "")[:4],
            "imdbID": data.get("imdb_id", ""),
            "Actors": actors,
        }
        # Populate movie cache so get_movie_data() finds it
        if result.get("imdbID"):
            _movie_cache[result["imdbID"]] = result

    _tmdb_cache[cache_key] = result
    return result


def get_tmdb_id_from_imdb(imdb_id, media_type):
    """
    Look up the TMDB ID for a given IMDb ID via TMDB's /find endpoint.

    Args:
        imdb_id: IMDb ID (e.g. "tt1375666").
        media_type: "movie" or "tv".

    Returns:
        TMDB ID as string, or None if not found.
    """
    try:
        data = _tmdb_request(f"/find/{imdb_id}", {"external_source": "imdb_id"})
    except Exception as e:
        print(f"  ❌ TMDB find error: {e}")
        return None
    key = "movie_results" if media_type == "movie" else "tv_results"
    results = data.get(key, [])
    if results:
        return str(results[0]["id"])
    return None


def remove_processed_links(processed_ids, input_file):
    """
    Remove processed IMDb links from the input file.
    Comment lines (#) and blank lines are preserved.
    Creates a backup (.bak) before overwriting.
    """
    if not processed_ids or not os.path.exists(input_file):
        return
    with open(input_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        match = re.search(r"tt\d+", line)
        if match and match.group() in processed_ids:
            continue  # Remove this line
        new_lines.append(line)
    backup_path = input_file + ".bak"
    shutil.copy2(input_file, backup_path)
    with open(input_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"📦 Backup saved: {backup_path}")
    print(f"🗑️ {len(processed_ids)} IMDb link(s) removed from {input_file}.")


def _can_write_to(path):
    """Check if the target path is writable."""
    if not os.path.exists(path):
        return False
    return os.access(path, os.W_OK)


def _prompt_seasons(known_seasons=None):
    """
    Ask the user how many seasons to create.

    Args:
        known_seasons: Known season count from TMDB (used as suggestion).

    Returns:
        Number of seasons (int), at least 1.
    """
    if known_seasons:
        prompt = f"  📺 Seasons to create (TMDB: {known_seasons}, Enter = accept): "
    else:
        prompt = "  📺 How many seasons to create? "
    try:
        user_input = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return known_seasons or 1
    if not user_input and known_seasons:
        return known_seasons
    try:
        n = int(user_input)
        return max(1, n)
    except ValueError:
        if known_seasons:
            return known_seasons
        return 1


def search_by_title(title, year_hint=None):
    """
    Search for a movie or TV show by title via the TMDB API.

    Stage 1: Best match from /search/multi with confirmation
             (title, year, type, cast).
    Stage 2: On rejection, numbered list of top 5 results.

    Args:
        title: Search term (movie or show title).
        year_hint: If given (int), results matching this year are ranked
                   first (exact match, then ±1), before falling back to
                   TMDB's own popularity order.

    Returns:
        Tuple (id, media_type, seasons) on success:
            - Movie: ("tt1375666", "movie", None)
            - TV:    ("1396", "tv", 5)
        None on error or cancellation.
    """
    try:
        data = _tmdb_request("/search/multi", {"query": title})
    except Exception as e:
        print(f"  ❌ Search error: {e}")
        return None

    results = [r for r in data.get("results", [])
               if r.get("media_type") in ("movie", "tv")]

    if not results:
        print(f"  ❌ No results for \"{title}\".")
        return None

    if year_hint:
        def _year_of(r):
            date = r.get("release_date") or r.get("first_air_date") or ""
            try:
                return int(date[:4])
            except (ValueError, TypeError):
                return None
        exact, close, rest = [], [], []
        for r in results:
            y = _year_of(r)
            if y == year_hint:
                exact.append(r)
            elif y is not None and abs(y - year_hint) == 1:
                close.append(r)
            else:
                rest.append(r)
        results = exact + close + rest

    # Stage 1: Best match with details
    best = results[0]
    tmdb_id = str(best["id"])
    media_type = best["media_type"]

    details = get_tmdb_details(tmdb_id, media_type)
    if details and details.get("Response") == "True":
        type_label = "TV Show" if media_type == "tv" else "Movie"
        actors = details.get("Actors", "N/A")
        print(f"  🔍 {details['Title']} ({details['Year']}) — {type_label} — starring {actors}")
        try:
            answer = input("     Correct? (Enter/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if answer in ("", "j", "ja", "y", "yes"):
            if media_type == "tv":
                seasons = _prompt_seasons(details.get("Seasons"))
                print(f"  ✅ tmdb-{tmdb_id} confirmed.")
                return (tmdb_id, "tv", seasons)
            else:
                imdb_id = details.get("imdbID", "")
                if not imdb_id:
                    print("  ❌ No IMDb ID found for this movie.")
                    return None
                print(f"  ✅ {imdb_id} confirmed.")
                return (imdb_id, "movie", None)

    # Stage 2: List search
    display_results = results[:5]
    print(f"  Results for \"{title}\":")
    for i, r in enumerate(display_results, 1):
        type_label = "TV Show" if r["media_type"] == "tv" else "Movie"
        if r["media_type"] == "tv":
            name = r.get("name", "?")
            date = r.get("first_air_date", "")
        else:
            name = r.get("title", "?")
            date = r.get("release_date", "")
        year = date[:4] if date else "?"
        print(f"    [{i}] {name} ({year}) — {type_label}")

    try:
        choice = input("  Pick a number (or empty = skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not choice:
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(display_results):
            chosen = display_results[idx]
            tmdb_id = str(chosen["id"])
            media_type = chosen["media_type"]
            details = get_tmdb_details(tmdb_id, media_type)
            if details and details.get("Response") == "True":
                if media_type == "tv":
                    seasons = _prompt_seasons(details.get("Seasons"))
                    print(f"  ✅ tmdb-{tmdb_id} confirmed.")
                    return (tmdb_id, "tv", seasons)
                else:
                    imdb_id = details.get("imdbID", "")
                    if imdb_id:
                        print(f"  ✅ {imdb_id} confirmed.")
                        return (imdb_id, "movie", None)
                    print("  ❌ No IMDb ID found for this movie.")
        else:
            print("  Invalid selection.")
    except ValueError:
        print("  Invalid input.")
    return None


def _prompt_for_links():
    """
    Interactively ask the user for IMDb URLs, TMDB URLs, or titles.
    Empty input ends the prompt.

    Returns:
        List of tuples (id, media_type, seasons):
            - Movie: (imdb_id, "movie", None)
            - TV:    (tmdb_id, "tv", 5)
    """
    print("Enter IMDb URL, TMDB URL, or title (empty line to start processing):")
    seen = set()
    entries = []
    while True:
        try:
            user_input = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            break

        # Detect TMDB URL: themoviedb.org/tv/1396-...
        tmdb_match = re.search(r"themoviedb\.org/tv/(\d+)", user_input)
        if tmdb_match:
            tmdb_id = tmdb_match.group(1)
            key = f"tmdb-{tmdb_id}"
            if key not in seen:
                seen.add(key)
                details = get_tmdb_details(tmdb_id, "tv")
                known_seasons = details.get("Seasons") if details else None
                seasons = _prompt_seasons(known_seasons)
                entries.append((tmdb_id, "tv", seasons))
            continue

        # Detect IMDb URL or tt-ID
        imdb_match = re.search(r"tt\d+", user_input)
        if imdb_match:
            imdb_id = imdb_match.group()
            if imdb_id not in seen:
                seen.add(imdb_id)
                entries.append((imdb_id, "movie", None))
            continue

        # Treat input as title search (movie or TV show)
        result = search_by_title(user_input)
        if result:
            entry_id, media_type, seasons = result
            key = entry_id if media_type == "movie" else f"tmdb-{entry_id}"
            if key not in seen:
                seen.add(key)
                entries.append(result)
                break  # Proceed to processing
    return entries


def _resolve_naming(media_type, preset):
    """
    Decide which ID type to tag a folder with, given the naming preset.

    Plex: always imdb for movies, always tmdb for series.
    Jellyfin: user-configurable per media type.

    Returns:
        "imdb" or "tmdb".
    """
    if preset == "plex":
        return "imdb" if media_type == "movie" else "tmdb"
    # Jellyfin
    if media_type == "movie":
        return MOVIE_ID_SOURCE
    return SERIES_ID_SOURCE


def _resolve_id_value(entry_id, media_type, want_id_type, data):
    """
    Return the ID value in the wanted format (imdb or tmdb).

    `entry_id` is what we already have:
      - movie flow: IMDb ID (e.g. tt1375666)
      - tv flow:    TMDB ID (e.g. 1396)

    `data` is the result from get_movie_data() or get_tmdb_details(),
    which may carry the "other" ID we need.
    """
    if media_type == "movie":
        # entry_id is an IMDb ID
        if want_id_type == "imdb":
            return entry_id
        # Need a TMDB ID for this movie
        tmdb_id = None
        if data and data.get("tmdbID"):
            tmdb_id = data["tmdbID"]
        if not tmdb_id:
            tmdb_id = get_tmdb_id_from_imdb(entry_id, "movie")
        return tmdb_id
    # TV: entry_id is a TMDB ID
    if want_id_type == "tmdb":
        return entry_id
    # Need an IMDb ID for this TV show
    return (data or {}).get("imdbID") or None


def process_list(dry_run=False, interactive=False, output_path=None, input_file=None,
                 prompt_mode=False, direct_title=None, preset_override=None):
    """
    Read the input file (or prompt interactively), deduplicate IDs,
    and create media-server-compatible folders for movies and TV shows.

    Args:
        dry_run: If True, show planned actions without creating folders.
        interactive: If True, show dry run then ask for confirmation.
        output_path: Override the default target path (applies to movies and series).
        input_file: Override the default input file.
        prompt_mode: If True, skip the input file and prompt interactively.
        direct_title: If set, search for this title directly.
        preset_override: If set ("plex" or "jellyfin"), override the configured
                         naming preset for this run only.
    """
    movie_path = output_path if output_path is not None else MOVIE_PATH
    series_path = output_path if output_path is not None else SERIES_PATH
    file_path = input_file if input_file is not None else INPUT_FILE
    preset = preset_override or NAMING_PRESET
    use_from_file = True

    # Check target path (only in file mode without dry run / interactive)
    if not dry_run and not interactive and not prompt_mode:
        if not os.path.exists(movie_path):
            print(f"❌ ABORT: Path '{movie_path}' not found. NAS connected?")
            return
        if os.path.exists(movie_path) and not _can_write_to(movie_path):
            print(f"❌ ABORT: No write permissions for '{movie_path}'.")
            return

    valid_count = 0
    if direct_title:
        # Direct search: title was passed as argument
        result = search_by_title(direct_title)
        if result:
            entries = [result]
        else:
            entries = []
        use_from_file = False
    elif prompt_mode:
        entries = _prompt_for_links()
        use_from_file = False
    else:
        if not os.path.exists(file_path):
            print(f"File {file_path} not found!")
            return

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        seen = set()
        entries = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.search(r"tt\d+", line)
            if not match:
                print(f"Invalid URL skipped: {line}")
                continue
            valid_count += 1
            imdb_id = match.group()
            if imdb_id not in seen:
                seen.add(imdb_id)
                entries.append((imdb_id, "movie", None))

        if not entries:
            entries = _prompt_for_links()
            use_from_file = False

    if not entries:
        print("Nothing to process.")
        return

    if use_from_file and valid_count > len(entries):
        print(f"ℹ️ {valid_count - len(entries)} duplicate(s) skipped → {len(entries)} unique entries")

    mode = "Dry run" if (dry_run or interactive) else "Processing"
    print(f"{mode}: {len(entries)} entries...")

    to_create = []  # For interactive: (folder_name, full_path, seasons) tuples
    successfully_processed = set()  # IMDb IDs for file cleanup
    count_created = 0
    count_existing = 0
    count_failed = 0

    for idx, (entry_id, media_type, seasons) in enumerate(entries, 1):
        if len(entries) > 1:
            print(f"  [{idx}/{len(entries)}] ", end="")

        # Fetch data and determine target path
        if media_type == "tv":
            data = get_tmdb_details(entry_id, "tv")
            target_path = series_path
        else:
            data = get_movie_data(entry_id)
            target_path = movie_path

        if data and data.get("Response") == "True":
            title = data["Title"]
            year = data["Year"]

            # Year range like "1999–2000" → use first year only
            year = str(year).split("–")[0].split("-")[0].strip()

            # Folder name: remove invalid characters (/:*?"<>|\ etc.)
            clean_title = re.sub(r'[<>:"/\\|?*]', '', title)
            clean_year = re.sub(r'[<>:"/\\|?*]', '', year) or "0000"

            # Decide ID tag based on preset + media type
            want_id_type = _resolve_naming(media_type, preset)
            id_value = _resolve_id_value(entry_id, media_type, want_id_type, data)
            if not id_value:
                print(f"❌ Could not resolve {want_id_type} ID for {entry_id} — skipping")
                count_failed += 1
                continue
            folder_name = format_folder_name(clean_title, clean_year,
                                             want_id_type, id_value, preset)

            full_path = os.path.join(target_path, folder_name)
            exists = os.path.exists(full_path)

            if media_type == "movie":
                successfully_processed.add(entry_id)

            if dry_run or interactive:
                status = "would create" if not exists else "already exists"
                print(f"📋 {status}: {folder_name}")
                if seasons:
                    for s in range(1, seasons + 1):
                        print(f"   📋 Season {s:02d}")
                if not exists:
                    to_create.append((folder_name, full_path, seasons))
                    count_created += 1
                else:
                    count_existing += 1
            elif not exists:
                if not os.path.exists(target_path):
                    print(f"❌ Path '{target_path}' not found. NAS connected?")
                    count_failed += 1
                    continue
                os.makedirs(full_path)
                print(f"✅ Created: {folder_name}")
                if seasons:
                    for s in range(1, seasons + 1):
                        season_path = os.path.join(full_path, f"Season {s:02d}")
                        os.makedirs(season_path)
                        print(f"   ✅ Season {s:02d}")
                count_created += 1
            else:
                print(f"ℹ️ Already exists: {folder_name}")
                count_existing += 1
        else:
            print(f"❌ Error for ID {entry_id}: {data.get('Error') if data else 'No response'}")
            count_failed += 1

        # Rate limiting
        time.sleep(0.2)

    # Summary
    summary_parts = []
    if count_created:
        summary_parts.append(f"{count_created} created")
    if count_existing:
        summary_parts.append(f"{count_existing} already existed")
    if count_failed:
        summary_parts.append(f"{count_failed} failed")
    if summary_parts:
        print(f"\n📊 Summary: {', '.join(summary_parts)}")

    # Normal mode: remove processed links from input file
    if use_from_file and not dry_run and not interactive and successfully_processed:
        remove_processed_links(successfully_processed, file_path)

    # Interactive: create folders after confirmation (no repeated API calls)
    if interactive and to_create:
        paths_needed = set(os.path.dirname(fp) for _, fp, _ in to_create)
        for p in paths_needed:
            if not os.path.exists(p):
                print(f"❌ ABORT: Path '{p}' not found. NAS connected?")
                return
            if not _can_write_to(p):
                print(f"❌ ABORT: No write permissions for '{p}'.")
                return
        answer = input(f"\nCreate {len(to_create)} folder(s)? "
                       f"[Enter = yes, n = cancel]: ").strip().lower()
        if answer in ("", "j", "ja", "y", "yes"):
            for folder_name, full_path, seasons in to_create:
                os.makedirs(full_path)
                print(f"✅ Created: {folder_name}")
                if seasons:
                    for s in range(1, seasons + 1):
                        season_path = os.path.join(full_path, f"Season {s:02d}")
                        os.makedirs(season_path)
                        print(f"   ✅ Season {s:02d}")
            if use_from_file and successfully_processed:
                remove_processed_links(successfully_processed, file_path)
        else:
            print("Cancelled.")


# ===========================================================================
# Scan feature — detect raw media in a source folder and move/copy into the
# properly-named library folders.
# ===========================================================================


def parse_release_name(name):
    """
    Parse a scene-release filename or folder name with guessit.

    Args:
        name: Filename (with or without extension) or folder name.

    Returns:
        dict with keys: title (str|None), year (int|None),
        type ("movie" | "tv" | None), season (int|None).
    """
    from guessit import guessit  # lazy import: only needed for `scan`
    info = guessit(name)
    raw_type = info.get("type")
    if raw_type == "episode":
        media_type = "tv"
    elif raw_type == "movie":
        media_type = "movie"
    else:
        media_type = None
    return {
        "title": info.get("title"),
        "year": info.get("year"),
        "type": media_type,
        "season": info.get("season"),
    }


def _classify_media_file(path):
    """
    Return "video", "subtitle", or None for the given file path.

    Videos must be >= MIN_VIDEO_BYTES. Files containing "sample" in their
    name are always ignored.
    """
    name = os.path.basename(path).lower()
    if "sample" in name:
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        try:
            if os.path.getsize(path) >= MIN_VIDEO_BYTES:
                return "video"
        except OSError:
            return None
        return None
    if ext in SUBTITLE_EXTENSIONS:
        return "subtitle"
    return None


def _collect_media_files(path, max_depth=None):
    """
    Collect relevant media files from a path.

    If `path` is a single file, classify it. If it's a directory, walk
    recursively up to `max_depth` levels deep (default SCAN_MAX_DEPTH),
    skipping hidden, "sample", and SCAN_IGNORE subdirectories.

    Returns:
        list of (absolute_path, kind) tuples, where kind is "video" or "subtitle".
    """
    if max_depth is None:
        max_depth = SCAN_MAX_DEPTH
    results = []
    if os.path.isfile(path):
        kind = _classify_media_file(path)
        if kind:
            results.append((path, kind))
        return results
    if not os.path.isdir(path):
        return results
    base_depth = os.path.abspath(path).rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(path):
        current_depth = os.path.abspath(root).rstrip(os.sep).count(os.sep) - base_depth
        if current_depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = sorted(
                d for d in dirs
                if not d.startswith(".")
                and "sample" not in d.lower()
                and d.lower() not in SCAN_IGNORE
            )
        for fname in sorted(files):
            if fname.startswith("."):
                continue
            full = os.path.join(root, fname)
            kind = _classify_media_file(full)
            if kind:
                results.append((full, kind))
    return results


def _is_library_folder(name):
    """True if a folder name already carries a medianame ID tag."""
    return bool(_LIBRARY_TAG_RE.search(name))


def _should_skip_scan_entry(name):
    """
    Decide whether to skip a top-level entry by name alone
    (no filesystem access).
    """
    if name.startswith("."):
        return True
    if name.lower() in SCAN_IGNORE:
        return True
    if _is_library_folder(name):
        return True
    return False


def scan_source(source_path, max_age_days=0):
    """
    Scan `source_path` for media items.

    Each top-level entry (file or folder) is treated as one item. Items
    without any qualifying media files are skipped, as are:
      - hidden entries
      - entries matching SCAN_IGNORE
      - folders that already look like medianame library folders
      - entries older than `max_age_days` (mtime-based), if > 0

    Returns:
        list of dicts with keys: source (path), name (basename),
        parsed (dict from parse_release_name), media_files (list of (path, kind)).
    """
    if not os.path.isdir(source_path):
        print(f"❌ Scan source not found: {source_path}")
        return []
    cutoff = None
    if max_age_days and max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
    items = []
    skipped_ignored = 0
    skipped_library = 0
    skipped_old = 0
    for entry in sorted(os.listdir(source_path)):
        if entry.startswith("."):
            continue
        if entry.lower() in SCAN_IGNORE:
            skipped_ignored += 1
            continue
        if _is_library_folder(entry):
            skipped_library += 1
            continue
        full = os.path.join(source_path, entry)
        if cutoff is not None:
            try:
                if os.path.getmtime(full) < cutoff:
                    skipped_old += 1
                    continue
            except OSError:
                continue
        media_files = _collect_media_files(full)
        if not media_files:
            continue
        parsed = parse_release_name(entry)
        items.append({
            "source": full,
            "name": entry,
            "parsed": parsed,
            "media_files": media_files,
        })
    skip_notes = []
    if skipped_library:
        skip_notes.append(f"{skipped_library} already-named")
    if skipped_ignored:
        skip_notes.append(f"{skipped_ignored} on ignore list")
    if skipped_old:
        skip_notes.append(f"{skipped_old} older than {max_age_days}d")
    if skip_notes:
        print(f"ℹ️ Skipped: {', '.join(skip_notes)}")
    return items


def _choose_scan_source():
    """
    Ask the user which folder to scan.

    Options:
        [1] Configured movie folder
        [2] Configured TV show folder
        [3] Custom path (prompted interactively)

    Returns:
        Path string, or None if the user cancelled.
    """
    print("Which folder should I scan?")
    print(f"  [1] Movie folder:    {MOVIE_PATH}")
    print(f"  [2] TV show folder:  {SERIES_PATH}")
    print( "  [3] Custom path (enter manually)")
    try:
        choice = input("Pick 1, 2, or 3 (empty = cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice == "1":
        return MOVIE_PATH
    if choice == "2":
        return SERIES_PATH
    if choice == "3":
        try:
            raw = input("   Path: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None
        path = os.path.expanduser(raw)
        if not os.path.isdir(path):
            print(f"❌ Not a directory: {path}")
            return None
        return path
    return None


def _resolve_scan_item(item, preset):
    """
    Resolve one scan item to a folder name + target library path by
    asking TMDB (using the title/year guessit extracted, with interactive
    confirmation via search_by_title).

    Returns:
        dict with folder_name, target_path, seasons (int|None), or None on skip.
    """
    parsed = item["parsed"]
    title = parsed.get("title")
    if not title:
        print(f"  ⚠️ Could not parse title from: {item['name']}")
        return None

    # Pass year as a *hint* for re-ranking, not as part of the query string.
    # TMDB's /search/multi treats extra words as title keywords, so
    # "Boyz n the Hood 1991" returns zero results.
    result = search_by_title(title, year_hint=parsed.get("year"))
    if not result:
        return None
    entry_id, media_type, seasons = result

    # Fetch details for folder naming
    if media_type == "tv":
        data = get_tmdb_details(entry_id, "tv")
        target_root = SERIES_PATH
    else:
        data = get_movie_data(entry_id)
        target_root = MOVIE_PATH
    if not data or data.get("Response") != "True":
        print(f"  ❌ Could not fetch details for {entry_id}")
        return None

    clean_title = re.sub(r'[<>:"/\\|?*]', '', data["Title"])
    year = str(data.get("Year", "")).split("–")[0].split("-")[0].strip()
    clean_year = re.sub(r'[<>:"/\\|?*]', '', year) or "0000"

    want_id_type = _resolve_naming(media_type, preset)
    id_value = _resolve_id_value(entry_id, media_type, want_id_type, data)
    if not id_value:
        print(f"  ❌ Could not resolve {want_id_type} ID")
        return None

    folder_name = format_folder_name(clean_title, clean_year,
                                     want_id_type, id_value, preset)
    full_path = os.path.join(target_root, folder_name)
    # For TV: honour parsed season if present; else _prompt_seasons already ran
    return {
        "folder_name": folder_name,
        "target_path": full_path,
        "media_type": media_type,
        "seasons": seasons,
        "parsed_season": parsed.get("season"),
    }


def build_scan_plan(items, preset):
    """
    For each scanned item, ask TMDB + user to resolve the correct target
    folder. Returns a list of plan entries:

        {
          "source_name": str,
          "target_path": str,   # full path of the folder to create
          "folder_name": str,
          "media_type": "movie" | "tv",
          "seasons": int | None,
          "parsed_season": int | None,
          "media_files": [(src, kind), ...],
        }
    """
    plan = []
    for idx, item in enumerate(items, 1):
        print(f"\n[{idx}/{len(items)}] {item['name']}")
        resolved = _resolve_scan_item(item, preset)
        if not resolved:
            print("  ⏭️  Skipped.")
            continue
        plan.append({
            "source": item["source"],
            "source_name": item["name"],
            "target_path": resolved["target_path"],
            "folder_name": resolved["folder_name"],
            "media_type": resolved["media_type"],
            "seasons": resolved["seasons"],
            "parsed_season": resolved["parsed_season"],
            "media_files": item["media_files"],
        })
    return plan


def _destination_for(plan_entry, src_file):
    """
    Decide the destination path for a single source file based on its
    plan entry. Keeps the original filename.

    Movies → directly in the movie folder.
    TV     → inside Season NN subfolder. The season number comes from
             the parsed release name; fallback = 1.
    """
    filename = os.path.basename(src_file)
    if plan_entry["media_type"] == "tv":
        season = plan_entry["parsed_season"] or 1
        season_dir = os.path.join(plan_entry["target_path"],
                                   f"Season {int(season):02d}")
        return os.path.join(season_dir, filename)
    return os.path.join(plan_entry["target_path"], filename)


def _resolve_conflict(dest_path):
    """
    Ask the user what to do when a destination file already exists.

    Returns: "skip", "overwrite", or "abort".
    """
    print(f"  ⚠️  Already exists: {dest_path}")
    while True:
        try:
            answer = input("     [s]kip / [o]verwrite / [a]bort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "abort"
        if answer in ("s", "skip"):
            return "skip"
        if answer in ("o", "overwrite"):
            return "overwrite"
        if answer in ("a", "abort"):
            return "abort"


def execute_scan_plan(plan, operation="move"):
    """
    Carry out the plan: create folders, move/copy media files.

    Args:
        plan: output of build_scan_plan().
        operation: "move" or "copy".

    Returns:
        dict with counts: moved, copied, skipped, failed.
    """
    counts = {"moved": 0, "copied": 0, "skipped": 0, "failed": 0,
              "cleaned": 0}
    op_fn = shutil.move if operation == "move" else shutil.copy2
    verb_past = "Moved" if operation == "move" else "Copied"
    counter_key = "moved" if operation == "move" else "copied"

    for entry in plan:
        print(f"\n→ {entry['folder_name']}")
        # Create target folder (and Season NN for TV)
        try:
            os.makedirs(entry["target_path"], exist_ok=True)
        except OSError as e:
            print(f"  ❌ Could not create {entry['target_path']}: {e}")
            counts["failed"] += len(entry["media_files"])
            continue

        entry_ok = True   # True only if every media file moved without error/skip
        for src, kind in entry["media_files"]:
            dest = _destination_for(entry, src)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                decision = _resolve_conflict(dest)
                if decision == "abort":
                    print("  ⛔ Aborted by user.")
                    return counts
                if decision == "skip":
                    counts["skipped"] += 1
                    entry_ok = False
                    continue
                # overwrite
                try:
                    os.remove(dest)
                except OSError as e:
                    print(f"  ❌ Could not remove existing {dest}: {e}")
                    counts["failed"] += 1
                    entry_ok = False
                    continue
            try:
                op_fn(src, dest)
                print(f"  ✅ {verb_past}: {os.path.basename(src)}")
                counts[counter_key] += 1
            except OSError as e:
                print(f"  ❌ Failed {os.path.basename(src)}: {e}")
                counts["failed"] += 1
                entry_ok = False

        # After a successful MOVE of all qualifying files, delete the now-
        # residual source folder (samples, NFOs, screenshots, …). Never
        # touch the source for copy. Only applies when source is a folder
        # (single-file sources are fully moved by the loop above).
        source = entry.get("source")
        if (operation == "move" and entry_ok
                and source and os.path.isdir(source)):
            try:
                shutil.rmtree(source)
                print(f"  🧹 Removed source folder: {entry['source_name']}")
                counts["cleaned"] += 1
            except OSError as e:
                print(f"  ⚠️ Could not remove source folder {source}: {e}")

    return counts


def _print_scan_plan(plan, operation):
    """
    Render a human-readable summary of the scan plan.

    For each item, print the source (file or folder), the target folder,
    the list of files being moved/copied, and — for move — an explicit
    note that the source folder will be deleted afterwards.
    """
    verb = "Move" if operation == "move" else "Copy"
    divider = "=" * 60
    print()
    print(divider)
    print(f"Plan ({operation}) — {len(plan)} item(s):")
    print(divider)
    for idx, entry in enumerate(plan, 1):
        print()
        print(f"[{idx}] {entry['folder_name']}")
        source = entry.get("source")
        source_is_dir = bool(source and os.path.isdir(source))
        label = "Source folder:" if source_is_dir else "Source file:"
        print(f"    {label}")
        print(f"      {source or entry['source_name']}")
        print(f"    Target folder:")
        print(f"      {entry['target_path']}")
        # Group by subdirectory under target (e.g. Season 01 for TV)
        file_count = len(entry["media_files"])
        print(f"    {verb} {file_count} file(s):")
        seen_subdirs = set()
        for src, kind in entry["media_files"]:
            dest = _destination_for(entry, src)
            rel = os.path.relpath(dest, entry["target_path"])
            subdir = os.path.dirname(rel)
            if subdir and subdir not in seen_subdirs:
                seen_subdirs.add(subdir)
                # subtle hint; per-file lines below will include it anyway
            print(f"      • {rel}  ({kind})")
        if operation == "move" and source_is_dir:
            print(f"    Cleanup: source folder above (incl. any leftover")
            print(f"             files inside, e.g. .nfo / samples) will be")
            print(f"             deleted after the move.")
        elif operation == "move" and not source_is_dir:
            print(f"    Cleanup: source is a single file — fully moved, no")
            print(f"             folder to clean up.")
    print()
    print(divider)


def process_scan(source_path=None, operation=None, preset_override=None,
                 max_age_days=0):
    """
    High-level entry point for `medianame scan`.

    Args:
        source_path: Folder to scan. If None, prompt the user.
        operation: "move" or "copy". If None, use configured default.
        preset_override: Naming preset override for this run.
        max_age_days: If > 0, only consider top-level entries modified
                      within the last N days.
    """
    if source_path is None:
        source_path = _choose_scan_source()
        if not source_path:
            print("Cancelled.")
            return
    if not os.path.isdir(source_path):
        print(f"❌ Not a directory: {source_path}")
        return

    op = operation or DEFAULT_OPERATION
    preset = preset_override or NAMING_PRESET

    print(f"🔍 Scanning: {source_path}")
    if max_age_days and max_age_days > 0:
        print(f"   (only entries modified in the last {max_age_days} day(s))")
    items = scan_source(source_path, max_age_days=max_age_days)
    if not items:
        print("Nothing to process (no qualifying media files found).")
        return
    print(f"Found {len(items)} item(s) with media files.")

    plan = build_scan_plan(items, preset)
    if not plan:
        print("\nNothing to do.")
        return

    # Show final plan and confirm
    _print_scan_plan(plan, op)

    if op == "move":
        prompt = (f"\nProceed: move {len(plan)} item(s) and delete the "
                  f"source folder(s)? [Enter = yes, n = cancel]: ")
    else:
        prompt = (f"\nProceed: copy {len(plan)} item(s)? "
                  f"[Enter = yes, n = cancel]: ")
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = "n"
    # Enter (empty) = yes; "n" / "no" = cancel; anything else also cancels
    # to avoid misclicks.
    if answer not in ("", "y", "yes", "j", "ja"):
        print("Cancelled.")
        return

    counts = execute_scan_plan(plan, operation=op)

    # Summary
    parts = []
    if counts["moved"]:
        parts.append(f"{counts['moved']} moved")
    if counts["copied"]:
        parts.append(f"{counts['copied']} copied")
    if counts.get("cleaned"):
        parts.append(f"{counts['cleaned']} source folder(s) removed")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    if parts:
        print(f"\n📊 Summary: {', '.join(parts)}")


def _show_help():
    """Display detailed help text."""
    movie_path = MOVIE_PATH or "<not configured>"
    series_path = SERIES_PATH or "<not configured>"
    preset = NAMING_PRESET or "plex"
    # Examples reflect the currently configured preset
    if preset == "jellyfin":
        movie_example = "Inception (2010) [imdbid-tt1375666]"
        series_example = "Breaking Bad (2008) [tmdbid-1396]"
    else:
        movie_example = "Inception (2010) {imdb-tt1375666}"
        series_example = "Breaking Bad (2008) {tmdb-1396}"
    lines = [
        "🎬 medianame — Create Plex/Jellyfin-compatible folders from IMDb/TMDB",
        "",
        "Usage:",
        "  medianame                      Interactive mode (enter titles)",
        "  medianame <title>              Direct search (e.g. medianame breaking bad)",
        "  medianame -n <title>           Dry run: show what would be created",
        "  medianame -i <title>           Interactive: show first, then confirm",
        "  medianame -f movies.txt        Process IMDb URLs from file",
        "  medianame -o /path <title>     Override target path (movies + series)",
        "  medianame --preset jellyfin .. Override naming preset for this run",
        "  medianame scan [<path>]        Scan a folder for raw media and",
        "                                 move/copy into named library folders.",
        "                                 After a successful move, the original",
        "                                 source folder (incl. samples, NFOs,",
        "                                 screenshots) is removed automatically.",
        "  medianame scan --copy <path>   Scan and copy (preserve source)",
        "  medianame scan --max-age-days N",
        "                                 Scan: only entries modified in the last N days",
        "  medianame setup                (Re)configure API keys, paths, preset",
        "  medianame help                 Show this help",
        "",
        "Input formats:",
        "  breaking bad                  Title search (movies + TV via TMDB)",
        "  https://imdb.com/title/tt...  IMDb URL → movie",
        "  tt1375666                     IMDb ID → movie",
        "  https://themoviedb.org/tv/... TMDB URL → TV show",
        "",
        f"Active preset: {preset}",
        "",
        "Folder format:",
        f"  Movies:  {movie_example}      → {movie_path}",
        f"  Series:  {series_example}         → {series_path}",
        "           └── Season 01, Season 02, ...",
    ]
    print("\n".join(lines))


def _load_config():
    """Load configuration and set module globals."""
    global API_KEY, TMDB_TOKEN, MOVIE_PATH, SERIES_PATH
    global NAMING_PRESET, MOVIE_ID_SOURCE, SERIES_ID_SOURCE, DEFAULT_OPERATION
    global MIN_VIDEO_SIZE_MB, MIN_VIDEO_BYTES, SCAN_IGNORE
    import config
    cfg = config.get_config()
    API_KEY = cfg["omdb_api_key"]
    TMDB_TOKEN = cfg["tmdb_token"]
    MOVIE_PATH = cfg["movie_path"]
    SERIES_PATH = cfg["series_path"]
    # Naming fields are optional for backwards compatibility with v1.0 configs
    NAMING_PRESET = cfg.get("naming_preset", "plex")
    MOVIE_ID_SOURCE = cfg.get("movie_id_source", "imdb")
    SERIES_ID_SOURCE = cfg.get("series_id_source", "tmdb")
    DEFAULT_OPERATION = cfg.get("default_operation", "move")
    MIN_VIDEO_SIZE_MB = int(cfg.get("min_video_size_mb", 500))
    MIN_VIDEO_BYTES = MIN_VIDEO_SIZE_MB * 1024 * 1024
    # Merge user extras onto the built-in defaults
    extras = cfg.get("scan_ignore", []) or []
    SCAN_IGNORE = set(DEFAULT_SCAN_IGNORE) | {str(e).strip().lower()
                                               for e in extras if str(e).strip()}
    global SCAN_MAX_AGE_DAYS
    SCAN_MAX_AGE_DAYS = int(cfg.get("scan_max_age_days", 0) or 0)


def main():
    parser = argparse.ArgumentParser(
        prog="medianame",
        description="Create Plex/Jellyfin-compatible folders from IMDb/TMDB",
    )
    parser.add_argument("title", nargs="*", help="Movie or TV show title (starts direct search)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Show what would be created (no changes)")
    parser.add_argument("-i", "--interactive", action="store_true", help="Dry run, then confirm: create folders? (y/n)")
    parser.add_argument("-o", "--output", metavar="PATH", help="Override target path for folders")
    parser.add_argument("-f", "--file", metavar="FILE", dest="input_file", help="Alternative input file (default: movies.txt)")
    parser.add_argument("-p", "--prompt", action="store_true", help="Prompt for movies/series (skip input file)")
    parser.add_argument("--preset", choices=["plex", "jellyfin"], help="Override naming preset for this run")
    parser.add_argument("--copy", action="store_true", help="Scan only: copy instead of move")
    parser.add_argument("--move", action="store_true", help="Scan only: move instead of copy")
    parser.add_argument("--max-age-days", type=int, default=None, metavar="N",
                        help="Scan only: skip entries older than N days "
                             "(default: value from config, 0 = no limit)")
    args = parser.parse_args()

    # Handle setup and help before loading config
    if args.title and args.title[0].lower() == "setup":
        import config
        config.run_setup()
        return

    # `medianame scan [<path>]`
    if args.title and args.title[0].lower() == "scan":
        _load_config()
        scan_path = " ".join(args.title[1:]) if len(args.title) > 1 else None
        if args.copy and args.move:
            print("❌ --copy and --move are mutually exclusive.")
            return
        operation = "copy" if args.copy else ("move" if args.move else None)
        # CLI flag overrides config; if absent, fall back to configured default.
        max_age = args.max_age_days if args.max_age_days is not None \
            else SCAN_MAX_AGE_DAYS
        process_scan(source_path=scan_path, operation=operation,
                     preset_override=args.preset,
                     max_age_days=max_age)
        return

    if args.title and args.title[0].lower() == "help":
        # Load config if available, but don't force setup
        import config
        cfg = config.load_config()
        if cfg:
            global MOVIE_PATH, SERIES_PATH, NAMING_PRESET
            MOVIE_PATH = cfg["movie_path"]
            SERIES_PATH = cfg["series_path"]
            NAMING_PRESET = cfg.get("naming_preset", "plex")
        _show_help()
        return

    # Load configuration (triggers setup on first run)
    _load_config()

    if args.title:
        # Direct search: "medianame breaking bad" → join title words
        title = " ".join(args.title)
        process_list(dry_run=args.dry_run, interactive=args.interactive,
                     output_path=args.output, input_file=args.input_file,
                     prompt_mode=True, direct_title=title, preset_override=args.preset)
    elif args.input_file:
        # File mode: only when -f is explicitly given
        process_list(dry_run=args.dry_run, interactive=args.interactive,
                     output_path=args.output, input_file=args.input_file,
                     preset_override=args.preset)
    else:
        # No arguments or -p → interactive prompt
        process_list(dry_run=args.dry_run, interactive=args.interactive,
                     output_path=args.output, prompt_mode=True,
                     preset_override=args.preset)


if __name__ == "__main__":
    main()
