"""
Create Plex- or Jellyfin-compatible folders from IMDb links / TMDB.

Reads a text file with IMDb URLs (or tt-IDs) and resolves each via the
TMDB API to title and year, then creates media-server-compatible folders.

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
    medianame scan --no-publish      # Disable auto-publish for this run
    medianame publish [<path>]       # Move tag-named folders from the working
                                     # paths into the configured Plex/Jellyfin
                                     # library (setup steps 11/12).
    medianame setup                  # (Re)configure API token, paths, preset
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


# --- Configuration (loaded at startup from ~/.config/medianame/config.json) ---

TMDB_TOKEN = None
MOVIE_PATH = None
SERIES_PATH = None
INPUT_FILE = "movies.txt"

# Naming configuration (loaded from config.json; defaults = Plex)
NAMING_PRESET = "plex"        # "plex" | "jellyfin"
MOVIE_ID_SOURCE = "imdb"      # "imdb" | "tmdb"  (only used when preset=jellyfin)
SERIES_ID_SOURCE = "tmdb"     # "imdb" | "tmdb"  (only used when preset=jellyfin)
DEFAULT_OPERATION = "move"    # "move" | "copy"  (for `medianame scan`)


# Cache for fetched data (avoids duplicate API calls)
_movie_cache = {}
_tmdb_cache = {}


def get_movie_data(imdb_id):
    """
    Fetch movie data for an IMDb ID using TMDB.

    Resolves IMDb → TMDB via `/find`, then fetches full details via
    `/movie/{id}`. Returns a dict in the historical shape expected by
    the rest of the module:

        {"Response": "True"|"False",
         "Title": str, "Year": str, "Actors": str, "imdbID": str,
         "Error": str (only on failure)}

    Results are cached in `_movie_cache` (keyed by IMDb ID).
    """
    if imdb_id in _movie_cache:
        return _movie_cache[imdb_id]
    try:
        tmdb_id = get_tmdb_id_from_imdb(imdb_id, "movie")
    except Exception as e:
        return {"Response": "False", "Error": f"TMDB find failed: {e}"}
    if not tmdb_id:
        return {"Response": "False",
                "Error": f"No TMDB match for IMDb ID {imdb_id}"}
    details = get_tmdb_details(tmdb_id, "movie")
    if not details or details.get("Response") != "True":
        return {"Response": "False",
                "Error": f"TMDB details lookup failed for {imdb_id}"}
    # get_tmdb_details() already populates _movie_cache for movies by
    # their IMDb ID, but only when TMDB actually returned one. Make sure
    # the caller-provided id is also cached so repeat lookups are free.
    _movie_cache[imdb_id] = details
    return details


_INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


def _sanitize_title_year(title, year):
    """
    Strip filesystem-invalid characters from a title and collapse
    year ranges (e.g. "1999–2000") to the starting year.

    Returns:
        (clean_title, clean_year) tuple. clean_year falls back to "0000"
        when empty.
    """
    clean_title = _INVALID_PATH_CHARS_RE.sub("", title or "")
    year_str = str(year or "").split("–")[0].split("-")[0].strip()
    clean_year = _INVALID_PATH_CHARS_RE.sub("", year_str) or "0000"
    return clean_title, clean_year


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
            clean_title, clean_year = _sanitize_title_year(
                data["Title"], data.get("Year", ""))

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


def _prompt_unmatched_scan_item(entry_name):
    """
    When a scan item could not be resolved via TMDB, ask the user what
    to do. Returns one of:
        ("ignore", None)     — add entry_name to scan_ignore (persisted)
        ("retry", new_title) — re-run the title search with a manual title
        ("skip", None)       — skip this entry for this run only
    """
    print()
    print(f"     🤔 Couldn't resolve: {entry_name}")
    print(f"        [i] Add to ignore list (never scan again)")
    print(f"        [m] Enter title manually")
    print(f"        [s] Skip (this run only)")
    while True:
        try:
            choice = input("        Choice [Enter = skip]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return ("skip", None)
        if choice in ("", "s", "skip"):
            return ("skip", None)
        if choice in ("i", "ignore"):
            return ("ignore", None)
        if choice in ("m", "manual", "t", "title"):
            try:
                title = input("        Title: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return ("skip", None)
            if not title:
                return ("skip", None)
            return ("retry", title)


def _add_to_scan_ignore(name):
    """
    Persist `name` to the config's scan_ignore list and update the
    in-memory SCAN_IGNORE set so subsequent items in the same run are
    also filtered.
    """
    global SCAN_IGNORE
    import config as _config
    cfg = _config.load_config() or {}
    extras = list(cfg.get("scan_ignore") or [])
    # Deduplicate case-insensitively
    if not any(e.strip().lower() == name.strip().lower() for e in extras):
        extras.append(name)
        cfg["scan_ignore"] = extras
        _config.save_config(cfg)
    SCAN_IGNORE = SCAN_IGNORE | {name.strip().lower()}
    print(f"        ✅ Added to ignore list: {name}")


def _resolve_scan_item(item, preset):
    """
    Resolve one scan item to a folder name + target library path by
    asking TMDB (using the title/year guessit extracted, with interactive
    confirmation via search_by_title).

    If the TMDB lookup fails or the user rejects all suggestions, offer
    three follow-up options: add the entry name to the ignore list,
    try again with a manually entered title, or skip this run only.

    Returns:
        dict with folder_name, target_path, seasons (int|None), or None on skip.
    """
    parsed = item["parsed"]
    title = parsed.get("title")

    if not title:
        print(f"  ⚠️ Could not parse title from: {item['name']}")
        # Still offer the fallback menu — the user might know better.
        action, payload = _prompt_unmatched_scan_item(item["name"])
        if action == "ignore":
            _add_to_scan_ignore(item["name"])
            return None
        if action != "retry":
            return None
        title = payload
        # Drop the (missing) year hint on manual retry
        parsed = dict(parsed, year=None)

    # Pass year as a *hint* for re-ranking, not as part of the query string.
    # TMDB's /search/multi treats extra words as title keywords, so
    # "Boyz n the Hood 1991" returns zero results.
    result = search_by_title(title, year_hint=parsed.get("year"))
    while not result:
        action, payload = _prompt_unmatched_scan_item(item["name"])
        if action == "ignore":
            _add_to_scan_ignore(item["name"])
            return None
        if action == "skip":
            return None
        # retry with manual title
        title = payload
        result = search_by_title(title, year_hint=None)
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

    clean_title, clean_year = _sanitize_title_year(
        data["Title"], data.get("Year", ""))

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
        dict with counts: moved, copied, skipped, failed, cleaned,
        and a set "created_folders" with target paths that ended up
        populated (used for the optional publish step).
    """
    counts = {"moved": 0, "copied": 0, "skipped": 0, "failed": 0,
              "cleaned": 0, "created_folders": set()}
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
                counts["created_folders"].add(entry["target_path"])
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
                 max_age_days=0, publish_mode="auto"):
    """
    High-level entry point for `medianame scan`.

    Args:
        source_path: Folder to scan. If None, prompt the user.
        operation: "move" or "copy". If None, use configured default.
        preset_override: Naming preset override for this run.
        max_age_days: If > 0, only consider top-level entries modified
                      within the last N days.
        publish_mode: "auto" (default — publish when library paths are
                      configured), "force" (always attempt publish),
                      or "off" (never publish).
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

    # Determine whether the optional publish step will run, and — if so —
    # build its plan up front so we can show everything in one preview
    # and ask for a single confirmation.
    will_publish = (
        publish_mode != "off"
        and (MOVIE_LIBRARY_PATH or SERIES_LIBRARY_PATH)
    )
    publish_preview = []
    if will_publish:
        publish_preview = _predict_publish_plan(plan)

    # Show final plan(s) and confirm once
    _print_scan_plan(plan, op)
    if publish_preview:
        _print_publish_plan(publish_preview)

    if publish_preview:
        prompt = (f"\nProceed: {op} {len(plan)} item(s) + publish "
                  f"{len(publish_preview)} to library? "
                  f"[Enter = yes, n = cancel]: ")
    elif op == "move":
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

    # Optional publish step: move the freshly created library folders into
    # the configured Plex/Jellyfin library paths. If we already showed the
    # publish preview in the combined confirmation above, skip the second
    # confirmation here.
    if publish_mode == "off":
        return
    created = counts.get("created_folders") or set()
    if publish_mode == "auto":
        if not (MOVIE_LIBRARY_PATH or SERIES_LIBRARY_PATH):
            return
        _publish_after_scan(created, operation="move",
                            skip_confirm=bool(publish_preview))
    elif publish_mode == "force":
        if not (MOVIE_LIBRARY_PATH or SERIES_LIBRARY_PATH):
            print("ℹ️ --publish requested but no library paths configured.")
            return
        _publish_after_scan(created, operation="move",
                            skip_confirm=bool(publish_preview))


# ===========================================================================
# Publish feature — move finished, tag-named folders from the working area
# (MOVIE_PATH / SERIES_PATH) into the actual Plex/Jellyfin library
# (MOVIE_LIBRARY_PATH / SERIES_LIBRARY_PATH). Optional — the target paths
# are only configured when the user wants this step automated.
# ===========================================================================

MOVIE_LIBRARY_PATH = None
SERIES_LIBRARY_PATH = None

# Folder names (case-insensitive) to skip during `namecheck`. Populated by
# `_load_config`; mutated by `_add_to_namecheck_ignore` when the user picks
# "[i] ignore permanently" in the interactive remediation flow.
NAMECHECK_IGNORE = set()

# Files below this size skip the progress indicator for cross-FS copies.
_PROGRESS_MIN_BYTES = 100 * 1024 * 1024  # 100 MB
# Block size used by the progress-aware copy.
_COPY_BLOCK_BYTES = 4 * 1024 * 1024  # 4 MB

_SEASON_DIR_RE = re.compile(r"^Season\s+(\d{1,3})$", re.IGNORECASE)
# Matches folder names with our title/year prefix (with or without tag)
_TITLE_YEAR_RE = re.compile(r"^(?P<base>.+?\(\d{4}\))(?:\s+[\{\[].+[\}\]])?\s*$")
# Matches a trailing year in parentheses: "Inception (2010)" → year 2010
_TRAILING_YEAR_RE = re.compile(r"\s*\((?P<year>\d{4})\)\s*$")


def _fmt_size(num_bytes):
    """Human-readable file size: 123.4 MB / 4.5 GB."""
    if num_bytes is None:
        return "?"
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{num_bytes} B"


def _fmt_mtime(ts):
    """Format an mtime timestamp as YYYY-MM-DD."""
    if ts is None:
        return "?"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (ValueError, OSError):
        return "?"


def _file_fingerprint(path):
    """Return (size, mtime) or (None, None) if the file is gone."""
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return None, None


def _list_video_files(folder):
    """
    Return a list of (path, size, mtime) for video files in `folder`
    (non-recursive). Used for conflict display.
    """
    out = []
    try:
        for name in sorted(os.listdir(folder)):
            ext = os.path.splitext(name)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            full = os.path.join(folder, name)
            size, mtime = _file_fingerprint(full)
            out.append((full, size, mtime))
    except OSError:
        return []
    return out


def _copy_with_progress(src, dst):
    """
    Copy `src` to `dst` with a one-line progress indicator for files
    >= _PROGRESS_MIN_BYTES. Preserves metadata via shutil.copystat after
    the byte copy. For smaller files, falls back to shutil.copy2.
    """
    try:
        size = os.path.getsize(src)
    except OSError:
        size = 0
    if size < _PROGRESS_MIN_BYTES:
        shutil.copy2(src, dst)
        return
    label = os.path.basename(src)
    done = 0
    last_pct = -1
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(_COPY_BLOCK_BYTES)
            if not chunk:
                break
            fout.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / size)
            if pct != last_pct:
                last_pct = pct
                print(f"\r      Copying {label} … "
                      f"{_fmt_size(done)} / {_fmt_size(size)} ({pct}%)",
                      end="", flush=True)
    shutil.copystat(src, dst, follow_symlinks=False)
    print()


def _move_or_copy_file(src, dst, operation="move"):
    """
    Move or copy a single file, using progress-aware copy for large files
    on cross-filesystem moves. Returns True on success, False on error.
    """
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if operation == "copy":
            _copy_with_progress(src, dst)
            return True
        # move: try os.rename (same FS, instant); fall back to copy+delete
        try:
            os.rename(src, dst)
            return True
        except OSError:
            _copy_with_progress(src, dst)
            os.remove(src)
            return True
    except OSError as e:
        print(f"      ❌ {e}")
        return False


def _move_folder(src, dst):
    """
    Move a whole folder to `dst`. If dst already exists, fail — callers
    should handle merges explicitly.
    """
    if os.path.exists(dst):
        raise OSError(f"Destination already exists: {dst}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass
    # Cross-FS: walk + copy + remove
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target_root, exist_ok=True)
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(target_root, f)
            _copy_with_progress(s, d)
    shutil.rmtree(src)


def _split_title_year(folder_name):
    """
    Split a folder name into (bare_title, year_or_None), stripping any
    trailing ID tag ({...} / [...]) and parenthesised year.

    Examples:
        "Inception (2010) {imdb-tt1375666}" → ("Inception", 2010)
        "Inception (2010)"                   → ("Inception", 2010)
        "Send Help"                          → ("Send Help", None)
    """
    # Strip trailing tag (possibly with leading whitespace)
    name = re.sub(r"\s+[\{\[][^\}\]]+[\}\]]\s*$", "", folder_name).strip()
    m = _TRAILING_YEAR_RE.search(name)
    if m:
        year = int(m.group("year"))
        bare = name[:m.start()].strip()
    else:
        year = None
        bare = name.strip()
    return (bare, year)


def _find_library_match(library_root, folder_name):
    """
    Look for a folder in `library_root` that matches `folder_name`.

    Match order:
      1. Exact name → ("exact", existing_name)
      2. Same bare title, matching year (or one side missing year)
         → ("rename", existing_name)
      3. Otherwise → (None, None)

    Rule (2) handles the real-world cases:
      - "Send Help"                     ↔  "Send Help (2026) {imdb-tt…}"
      - "Inception (2010)"              ↔  "Inception (2010) {imdb-tt…}"
      - "Inception (2010) {imdb-ttX}"   ↔  "Inception (2010) {imdb-ttY}"
    A mismatch in year (different movies sharing a title) is NOT matched.
    """
    if not library_root or not os.path.isdir(library_root):
        return (None, None)
    try:
        entries = os.listdir(library_root)
    except OSError:
        return (None, None)
    if folder_name in entries:
        return ("exact", folder_name)
    new_title, new_year = _split_title_year(folder_name)
    if not new_title:
        return (None, None)
    # Case-insensitive title match; year must agree (or one side empty)
    for existing in entries:
        if existing == folder_name:
            continue
        ex_title, ex_year = _split_title_year(existing)
        if ex_title.lower() != new_title.lower():
            continue
        if new_year is None or ex_year is None or new_year == ex_year:
            return ("rename", existing)
    return (None, None)


def _scan_for_publishable_items(staging_root):
    """
    Return a list of {"name", "source", "media_type"} for every tag-named
    folder directly under `staging_root`. `media_type` is "tv" if the folder
    contains a `Season NN/` subdir, otherwise "movie".
    """
    items = []
    if not os.path.isdir(staging_root):
        return items
    for entry in sorted(os.listdir(staging_root)):
        full = os.path.join(staging_root, entry)
        if not os.path.isdir(full):
            continue
        if not _is_library_folder(entry):
            continue
        media_type = "movie"
        try:
            for sub in os.listdir(full):
                if os.path.isdir(os.path.join(full, sub)) and _SEASON_DIR_RE.match(sub):
                    media_type = "tv"
                    break
        except OSError:
            continue
        items.append({"name": entry, "source": full, "media_type": media_type})
    return items


def build_publish_plan(staging_roots):
    """
    Build a publish plan from one or more staging roots.

    `staging_roots` is a list of (staging_path, library_path, media_type)
    tuples. Media type is used as a fallback when the folder content is
    ambiguous (e.g. a show without season folders yet).

    Returns a list of plan dicts:
        {
            "source": staging_path_to_folder,
            "folder_name": original_tag_name,
            "media_type": "movie" | "tv",
            "library_root": target_library_root,
            "match": "new" | "exact" | "rename",
            "existing_name": str | None,  # when match != "new"
        }
    """
    plan = []
    for staging, library, default_type in staging_roots:
        if not staging or not library:
            continue
        for item in _scan_for_publishable_items(staging):
            media_type = item["media_type"] or default_type
            kind, existing = _find_library_match(library, item["name"])
            plan.append({
                "source": item["source"],
                "folder_name": item["name"],
                "media_type": media_type,
                "library_root": library,
                "match": kind or "new",
                "existing_name": existing,
            })
    return plan


def _print_publish_plan(plan):
    """Render the publish plan for the confirmation prompt."""
    divider = "=" * 60
    print()
    print(divider)
    print(f"Publish plan — {len(plan)} item(s):")
    print(divider)
    for idx, entry in enumerate(plan, 1):
        print()
        type_label = "TV Show" if entry["media_type"] == "tv" else "Movie"
        print(f"[{idx}] {entry['folder_name']} ({type_label})")
        print(f"    From:   {entry['source']}")
        dest = os.path.join(entry["library_root"], entry["folder_name"])
        if entry["match"] == "new":
            print(f"    To:     {dest}")
            print(f"    Status: New — move as-is")
        elif entry["match"] == "exact":
            print(f"    To:     {dest}")
            print(f"    Status: Merge into existing folder (conflicts")
            print(f"            resolved per file during execution)")
        elif entry["match"] == "rename":
            existing_path = os.path.join(entry["library_root"], entry["existing_name"])
            print(f"    To:     {dest}")
            print(f"    ⚠️ Similar folder found in library:")
            print(f"       {existing_path}")
            print(f"    Status: Prompt at execution time — rename + merge")
    print()
    print(divider)


# --- Conflict resolution -----------------------------------------------------

def _prompt_file_conflict(src, dst):
    """
    Ask the user what to do when a file with the same name already exists
    in the target. Shows size + mtime for both.

    Returns: "replace", "skip", "keep_both", "abort".
    """
    s_size, s_mtime = _file_fingerprint(src)
    d_size, d_mtime = _file_fingerprint(dst)
    print(f"    ⚠️ File exists: {os.path.basename(dst)}")
    print(f"       Existing: {_fmt_size(d_size)}, modified {_fmt_mtime(d_mtime)}")
    print(f"       New:      {_fmt_size(s_size)}, modified {_fmt_mtime(s_mtime)}")
    while True:
        try:
            answer = input("       [r]eplace / [s]kip / [b]oth / [a]bort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "abort"
        if answer in ("r", "replace"):
            return "replace"
        if answer in ("s", "skip"):
            return "skip"
        if answer in ("b", "both", "k"):
            return "keep_both"
        if answer in ("a", "abort"):
            return "abort"


def _prompt_foreign_file(src, existing_videos):
    """
    Ask the user what to do when the new file is a video with a *different*
    name from existing videos in the target (user policy: one movie per
    folder, so still ask).

    Returns: "replace_all", "skip", "keep_both", "abort".
    """
    s_size, s_mtime = _file_fingerprint(src)
    print(f"    ⚠️ Target already contains video(s):")
    for p, size, mtime in existing_videos:
        print(f"       • {os.path.basename(p)} "
              f"({_fmt_size(size)}, {_fmt_mtime(mtime)})")
    print(f"       New file: {os.path.basename(src)} "
          f"({_fmt_size(s_size)}, {_fmt_mtime(s_mtime)})")
    while True:
        try:
            answer = input("       [r]eplace existing / [s]kip new / "
                           "[b]oth / [a]bort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "abort"
        if answer in ("r", "replace"):
            return "replace_all"
        if answer in ("s", "skip"):
            return "skip"
        if answer in ("b", "both", "k"):
            return "keep_both"
        if answer in ("a", "abort"):
            return "abort"


def _prompt_rename_merge(source_folder, existing_folder, new_name):
    """
    When the library has a similarly-named folder without the right tag.
    Offer the 4 options the user specified.

    Returns: "keep_old", "keep_new", "keep_all", "skip", "abort".
    """
    existing_videos = _list_video_files(existing_folder)
    source_videos = _list_video_files(source_folder)
    print()
    print(f"    ⚠️ Similar library folder found:")
    print(f"       Existing: {os.path.basename(existing_folder)}")
    for p, size, mtime in existing_videos:
        print(f"          • {os.path.basename(p)} "
              f"({_fmt_size(size)}, {_fmt_mtime(mtime)})")
    if not existing_videos:
        print(f"          (no top-level videos)")
    print(f"       New:      {new_name}")
    for p, size, mtime in source_videos:
        print(f"          • {os.path.basename(p)} "
              f"({_fmt_size(size)}, {_fmt_mtime(mtime)})")
    if not source_videos:
        print(f"          (no top-level videos)")
    print()
    print("       [1] Rename to new, keep old files (discard new)")
    print("       [2] Rename to new, replace with new files (discard old)")
    print("       [3] Rename to new, keep all files")
    print("       [4] Skip (leave both as-is)")
    while True:
        try:
            answer = input("       Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "abort"
        if answer == "1":
            return "keep_old"
        if answer == "2":
            return "keep_new"
        if answer == "3":
            return "keep_all"
        if answer == "4":
            return "skip"


def _prompt_episode_schema_conflict(src_name, existing_names):
    """
    Warn when the incoming episode file uses a different naming pattern
    than the episodes already in the season folder.

    Returns: "keep_as_is", "skip", "abort".
    """
    print(f"    ⚠️ Naming scheme differs from existing episodes:")
    print(f"       New:      {src_name}")
    print(f"       Existing examples:")
    for n in existing_names[:3]:
        print(f"          • {n}")
    while True:
        try:
            answer = input("       [k]eep new name / [s]kip / [a]bort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "abort"
        if answer in ("k", "keep", ""):
            return "keep_as_is"
        if answer in ("s", "skip"):
            return "skip"
        if answer in ("a", "abort"):
            return "abort"


def _episode_schema_signature(name):
    """
    Crude signature of an episode filename's naming scheme.
    Replaces digits with "#" and strips the extension, so
    "Show - S01E02 - Title.mkv" → "Show - S##E## - Title"
    """
    base = os.path.splitext(name)[0]
    return re.sub(r"\d", "#", base)


# --- Plan execution ----------------------------------------------------------

def _merge_files(src_dir, dst_dir, operation, cleanup_tracker):
    """
    Merge the contents of src_dir into dst_dir file by file.

    For each file:
      - identical fingerprint (name + size) → skip silently
      - videos, different name than existing videos → _prompt_foreign_file
      - same name, different fingerprint → _prompt_file_conflict
      - new name → just move/copy

    Returns "ok", "skip" (some files skipped, source not empty), or "abort".
    """
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError as e:
        print(f"    ❌ Cannot create {dst_dir}: {e}")
        return "abort"

    had_conflict_skip = False

    try:
        entries = sorted(os.listdir(src_dir))
    except OSError as e:
        print(f"    ❌ Cannot read {src_dir}: {e}")
        return "abort"

    for name in entries:
        src = os.path.join(src_dir, name)
        if os.path.isdir(src):
            # Nested dirs (Season NN/, subs/, …) → recurse
            result = _merge_files(src, os.path.join(dst_dir, name),
                                  operation, cleanup_tracker)
            if result == "abort":
                return "abort"
            if result == "skip":
                had_conflict_skip = True
            continue
        dst = os.path.join(dst_dir, name)
        s_size, _ = _file_fingerprint(src)

        # Check: same filename already exists?
        if os.path.exists(dst):
            d_size, _ = _file_fingerprint(dst)
            if s_size == d_size:
                # Identical → skip silently, remove src on move
                print(f"    ⏭  Identical, skipped: {name}")
                if operation == "move":
                    try:
                        os.remove(src)
                        cleanup_tracker["removed"] += 1
                    except OSError:
                        pass
                continue
            decision = _prompt_file_conflict(src, dst)
            if decision == "abort":
                return "abort"
            if decision == "skip":
                had_conflict_skip = True
                continue
            if decision == "replace":
                try:
                    os.remove(dst)
                except OSError as e:
                    print(f"    ❌ Cannot overwrite {dst}: {e}")
                    had_conflict_skip = True
                    continue
                ok = _move_or_copy_file(src, dst, operation)
                if not ok:
                    had_conflict_skip = True
                    continue
                print(f"    ✅ Replaced: {name}")
                continue
            if decision == "keep_both":
                new_dst = _unique_path(dst)
                ok = _move_or_copy_file(src, new_dst, operation)
                if not ok:
                    had_conflict_skip = True
                    continue
                print(f"    ✅ Kept both: {os.path.basename(new_dst)}")
                continue

        # No same-name conflict. If it's a video, check for foreign videos.
        # Skip this check inside Season NN/ folders, where multiple distinct
        # episode filenames are expected.
        ext = os.path.splitext(name)[1].lower()
        in_season = bool(_SEASON_DIR_RE.match(os.path.basename(dst_dir)))
        if ext in VIDEO_EXTENSIONS and not in_season:
            existing_videos = _list_video_files(dst_dir)
            if existing_videos:
                decision = _prompt_foreign_file(src, existing_videos)
                if decision == "abort":
                    return "abort"
                if decision == "skip":
                    had_conflict_skip = True
                    continue
                if decision == "replace_all":
                    for p, _, _ in existing_videos:
                        try:
                            os.remove(p)
                        except OSError as e:
                            print(f"    ⚠️ Could not remove {p}: {e}")
                    ok = _move_or_copy_file(src, dst, operation)
                    if not ok:
                        had_conflict_skip = True
                        continue
                    print(f"    ✅ Replaced library video(s) with: {name}")
                    continue
                # keep_both = fallthrough to normal move

        # Episode schema check (only inside Season NN folders)
        if (ext in VIDEO_EXTENSIONS
                and _SEASON_DIR_RE.match(os.path.basename(dst_dir))):
            existing_eps = [n for n in os.listdir(dst_dir)
                            if os.path.splitext(n)[1].lower() in VIDEO_EXTENSIONS
                            and os.path.isfile(os.path.join(dst_dir, n))]
            if existing_eps:
                sig_new = _episode_schema_signature(name)
                sigs_existing = {_episode_schema_signature(n) for n in existing_eps}
                if sig_new not in sigs_existing:
                    decision = _prompt_episode_schema_conflict(name, existing_eps)
                    if decision == "abort":
                        return "abort"
                    if decision == "skip":
                        had_conflict_skip = True
                        continue
                    # keep_as_is → fall through

        ok = _move_or_copy_file(src, dst, operation)
        if not ok:
            had_conflict_skip = True
            continue
        print(f"    ✅ {os.path.basename(dst)}")

    return "skip" if had_conflict_skip else "ok"


def _unique_path(path):
    """
    Return a path that doesn't exist yet by appending ' (1)', ' (2)', … to
    the filename stem. Used for the keep-both case.
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(1, 1000):
        candidate = f"{stem} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem} (dup){ext}"


def _cleanup_staging(folder):
    """
    Remove `folder` if empty. If not empty, print a summary of what's left
    and ask whether to delete anyway.
    """
    if not os.path.isdir(folder):
        return
    remaining = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            full = os.path.join(root, f)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            remaining.append((os.path.relpath(full, folder), size))
    if not remaining:
        try:
            shutil.rmtree(folder)
            print(f"  🧹 Removed empty staging folder: {os.path.basename(folder)}")
        except OSError as e:
            print(f"  ⚠️ Could not remove staging folder: {e}")
        return
    print(f"  ℹ️ Staging folder not empty — {len(remaining)} file(s) remaining:")
    for rel, size in remaining[:10]:
        print(f"     • {rel} ({_fmt_size(size)})")
    if len(remaining) > 10:
        print(f"     … and {len(remaining) - 10} more")
    try:
        answer = input("     Delete staging folder anyway? "
                       "[Enter = yes, n = keep]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = "n"
    if answer in ("", "y", "yes", "j", "ja"):
        try:
            shutil.rmtree(folder)
            print(f"  🧹 Removed: {os.path.basename(folder)}")
        except OSError as e:
            print(f"  ⚠️ Could not remove: {e}")
    else:
        print(f"  ℹ️ Kept: {folder}")


def execute_publish_plan(plan, operation="move"):
    """
    Execute a publish plan. Returns a counts dict.
    """
    counts = {"moved": 0, "merged": 0, "renamed": 0,
              "skipped": 0, "conflicts": 0}
    for entry in plan:
        src = entry["source"]
        name = entry["folder_name"]
        library = entry["library_root"]
        match = entry["match"]
        dst = os.path.join(library, name)
        print(f"\n📤 {name}")

        if match == "new":
            # Simple case: just move/rename the whole folder.
            try:
                _move_folder(src, dst)
                print(f"  ✅ Published: {dst}")
                counts["moved"] += 1
            except OSError as e:
                print(f"  ❌ Failed: {e}")
                counts["skipped"] += 1
            continue

        if match == "rename":
            existing_folder = os.path.join(library, entry["existing_name"])
            decision = _prompt_rename_merge(src, existing_folder, name)
            if decision == "abort":
                print("  ⛔ Aborted by user.")
                return counts
            if decision == "skip":
                print(f"  ⏭  Skipped: {name}")
                counts["skipped"] += 1
                continue
            if decision == "keep_old":
                # Rename existing folder, discard new files
                try:
                    os.rename(existing_folder, dst)
                    shutil.rmtree(src)
                    print(f"  ✅ Renamed existing → {name}, discarded new files")
                    counts["renamed"] += 1
                except OSError as e:
                    print(f"  ❌ Failed: {e}")
                    counts["skipped"] += 1
                continue
            if decision == "keep_new":
                # Remove existing, move new in with new name
                try:
                    shutil.rmtree(existing_folder)
                    _move_folder(src, dst)
                    print(f"  ✅ Replaced existing with new content ({name})")
                    counts["renamed"] += 1
                except OSError as e:
                    print(f"  ❌ Failed: {e}")
                    counts["skipped"] += 1
                continue
            if decision == "keep_all":
                # Rename existing, then merge new files in
                try:
                    os.rename(existing_folder, dst)
                except OSError as e:
                    print(f"  ❌ Rename failed: {e}")
                    counts["skipped"] += 1
                    continue
                cleanup_tracker = {"removed": 0}
                result = _merge_files(src, dst, operation, cleanup_tracker)
                if result == "abort":
                    print("  ⛔ Aborted by user.")
                    return counts
                counts["renamed"] += 1
                if result == "skip":
                    counts["conflicts"] += 1
                if operation == "move":
                    _cleanup_staging(src)
                continue

        if match == "exact":
            cleanup_tracker = {"removed": 0}
            result = _merge_files(src, dst, operation, cleanup_tracker)
            if result == "abort":
                print("  ⛔ Aborted by user.")
                return counts
            counts["merged"] += 1
            if result == "skip":
                counts["conflicts"] += 1
            if operation == "move":
                _cleanup_staging(src)

    return counts


def process_publish(source_path=None, operation="move"):
    """
    High-level entry point for `medianame publish`.

    If `source_path` is given, treat it as a staging root and infer media
    type (movie vs. series) from which library path best matches. If it
    matches neither, fall back to both library paths by folder content.
    Without `source_path`, publish both configured staging roots (MOVIE_PATH,
    SERIES_PATH) into their matching libraries.
    """
    if not MOVIE_LIBRARY_PATH and not SERIES_LIBRARY_PATH:
        print("❌ Publish is not configured.")
        print("   Run `medianame setup` and fill in steps 12 and/or 13.")
        return

    staging_roots = []
    if source_path:
        # Map the given staging to whichever library fits.
        # If the staging is one of the configured ones, pair accordingly;
        # otherwise, try both.
        if source_path == MOVIE_PATH and MOVIE_LIBRARY_PATH:
            staging_roots.append((source_path, MOVIE_LIBRARY_PATH, "movie"))
        elif source_path == SERIES_PATH and SERIES_LIBRARY_PATH:
            staging_roots.append((source_path, SERIES_LIBRARY_PATH, "tv"))
        else:
            # Ambiguous: build a per-item plan using both libraries.
            # We'll let each item's content-based media_type pick the lib.
            if MOVIE_LIBRARY_PATH:
                staging_roots.append((source_path, MOVIE_LIBRARY_PATH, "movie"))
            if SERIES_LIBRARY_PATH:
                staging_roots.append((source_path, SERIES_LIBRARY_PATH, "tv"))
    else:
        if MOVIE_PATH and MOVIE_LIBRARY_PATH:
            staging_roots.append((MOVIE_PATH, MOVIE_LIBRARY_PATH, "movie"))
        if SERIES_PATH and SERIES_LIBRARY_PATH:
            staging_roots.append((SERIES_PATH, SERIES_LIBRARY_PATH, "tv"))

    if not staging_roots:
        print("❌ No staging root / library pair is configured.")
        return

    raw_plan = build_publish_plan(staging_roots)
    # When the same staging folder is paired with both libraries (custom
    # source path), deduplicate: keep the entry whose declared media_type
    # matches the folder's content-inferred media_type.
    plan = _dedupe_publish_plan(raw_plan)

    if not plan:
        print("Nothing to publish (no tagged folders found).")
        return

    _print_publish_plan(plan)
    try:
        answer = input(f"\nProceed: publish {len(plan)} item(s)? "
                       f"[Enter = yes, n = cancel]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = "n"
    if answer not in ("", "y", "yes", "j", "ja"):
        print("Cancelled.")
        return

    counts = execute_publish_plan(plan, operation=operation)
    parts = []
    if counts["moved"]:
        parts.append(f"{counts['moved']} published")
    if counts["merged"]:
        parts.append(f"{counts['merged']} merged")
    if counts["renamed"]:
        parts.append(f"{counts['renamed']} renamed")
    if counts["conflicts"]:
        parts.append(f"{counts['conflicts']} with unresolved conflicts")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped")
    if parts:
        print(f"\n📊 Publish summary: {', '.join(parts)}")


def _dedupe_publish_plan(plan):
    """
    When the same source folder was paired with multiple libraries (custom
    path), keep the entry whose declared media_type matches the folder's
    content. If no match found, keep the first.
    """
    by_source = {}
    for entry in plan:
        key = entry["source"]
        if key not in by_source:
            by_source[key] = entry
            continue
        # Two entries for the same source — pick the one whose library
        # expects the same media type as the folder content suggests.
        # The folder's content-based type lives on each plan entry from
        # _scan_for_publishable_items via build_publish_plan: here both
        # entries share the same content-inferred media_type, so pick the
        # library whose default media type matches.
        existing = by_source[key]
        # Prefer the entry where media_type matches the item (which is
        # actually the *same* because scan picks it from content); if tied,
        # keep the first.
        if entry["media_type"] == existing["media_type"]:
            continue
        by_source[key] = entry
    return list(by_source.values())


def _predict_publish_plan(scan_plan):
    """
    Given a scan plan (list of entries with `target_path` + `media_type`),
    predict the publish plan that would run after the scan — by looking
    up each would-be staging folder's name against the configured library.

    Used to show scan + publish in one combined preview.
    """
    items = []
    for entry in scan_plan:
        target = entry["target_path"]
        name = os.path.basename(target)
        parent = os.path.dirname(target)
        media_type = entry.get("media_type", "movie")
        if parent == MOVIE_PATH and MOVIE_LIBRARY_PATH:
            lib = MOVIE_LIBRARY_PATH
        elif parent == SERIES_PATH and SERIES_LIBRARY_PATH:
            lib = SERIES_LIBRARY_PATH
        else:
            continue
        if not _is_library_folder(name):
            continue
        kind, existing = _find_library_match(lib, name)
        items.append({
            "source": target,
            "folder_name": name,
            "media_type": media_type,
            "library_root": lib,
            "match": kind or "new",
            "existing_name": existing,
        })
    return items


def _publish_after_scan(created_folders, operation="move", skip_confirm=False):
    """
    After a successful scan, publish any tag-named folders we just created.
    `created_folders` is a set of absolute paths. When `skip_confirm` is
    true, the scan+publish combined preview has already been confirmed.
    """
    if not created_folders:
        return
    if not MOVIE_LIBRARY_PATH and not SERIES_LIBRARY_PATH:
        return
    # Only publish folders that match a configured library + still exist.
    plan_items = []
    for folder in sorted(created_folders):
        if not os.path.isdir(folder):
            continue
        parent = os.path.dirname(folder)
        name = os.path.basename(folder)
        if not _is_library_folder(name):
            continue
        # Pair parent (staging root) with the matching library
        if parent == MOVIE_PATH and MOVIE_LIBRARY_PATH:
            lib, default_type = MOVIE_LIBRARY_PATH, "movie"
        elif parent == SERIES_PATH and SERIES_LIBRARY_PATH:
            lib, default_type = SERIES_LIBRARY_PATH, "tv"
        else:
            continue
        media_type = "movie"
        try:
            for sub in os.listdir(folder):
                if (os.path.isdir(os.path.join(folder, sub))
                        and _SEASON_DIR_RE.match(sub)):
                    media_type = "tv"
                    break
        except OSError:
            continue
        kind, existing = _find_library_match(lib, name)
        plan_items.append({
            "source": folder,
            "folder_name": name,
            "media_type": media_type or default_type,
            "library_root": lib,
            "match": kind or "new",
            "existing_name": existing,
        })
    if not plan_items:
        return

    print()
    print("📤 Publishing to library...")
    if not skip_confirm:
        _print_publish_plan(plan_items)
        try:
            answer = input(f"\nProceed: publish {len(plan_items)} item(s) "
                           f"to library? "
                           f"[Enter = yes, n = skip publish]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"
        if answer not in ("", "y", "yes", "j", "ja"):
            print("Publish skipped.")
            return
    counts = execute_publish_plan(plan_items, operation=operation)
    parts = []
    if counts["moved"]:
        parts.append(f"{counts['moved']} published")
    if counts["merged"]:
        parts.append(f"{counts['merged']} merged")
    if counts["renamed"]:
        parts.append(f"{counts['renamed']} renamed")
    if counts["conflicts"]:
        parts.append(f"{counts['conflicts']} with conflicts")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped")
    if parts:
        print(f"\n📊 Publish summary: {', '.join(parts)}")


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
        "  medianame scan --no-publish    Scan: skip publish step this run",
        "  medianame publish [<path>]     Move tag-named folders into the",
        "                                 configured library (setup 11/12).",
        "  medianame namecheck [<path>]   Audit the library (defaults to the",
        "                                 library folders from setup 11/12;",
        "                                 falls back to the working folders).",
        "                                 Flags missing ID tags, incomplete",
        "                                 TV seasons, orphan subtitles, and",
        "                                 duplicate IDs. Offers interactive",
        "                                 fix / ignore-permanently / skip.",
        "  medianame healthcheck          Verify setup: config, TMDB token,",
        "                                 paths, dependencies.",
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
    global TMDB_TOKEN, MOVIE_PATH, SERIES_PATH
    global NAMING_PRESET, MOVIE_ID_SOURCE, SERIES_ID_SOURCE, DEFAULT_OPERATION
    global MIN_VIDEO_SIZE_MB, MIN_VIDEO_BYTES, SCAN_IGNORE
    global MOVIE_LIBRARY_PATH, SERIES_LIBRARY_PATH
    import config
    cfg = config.get_config()
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
    # Optional library paths (empty string = disabled)
    MOVIE_LIBRARY_PATH = cfg.get("movie_library_path") or None
    SERIES_LIBRARY_PATH = cfg.get("series_library_path") or None
    # Folders that namecheck should skip permanently (user-curated).
    global NAMECHECK_IGNORE
    NAMECHECK_IGNORE = {str(e).strip().lower()
                         for e in (cfg.get("namecheck_ignore") or [])
                         if str(e).strip()}


# ===========================================================================
# namecheck — read-only audit of an existing library. Reports folders with
# missing/malformed ID tags, TV seasons with missing episodes (via TMDB),
# orphan subtitle files, and duplicate IDs. Does not modify anything.
# ===========================================================================

_TAG_CONTENT_RE = re.compile(
    r"[{\[](?P<kind>imdbid|tmdbid|imdb|tmdb)-(?P<val>[^}\]]+)[}\]]"
)


def _extract_id_from_tag(folder_name):
    """
    Return ("imdb"|"tmdb", id_value) if `folder_name` carries a medianame
    tag, else (None, None).
    """
    m = _TAG_CONTENT_RE.search(folder_name)
    if not m:
        return (None, None)
    kind = m.group("kind")
    id_type = "imdb" if kind.startswith("imdb") else "tmdb"
    return (id_type, m.group("val"))


def _tmdb_season_episode_counts(tmdb_id):
    """
    Return {season_number: episode_count, …} for a TV show, or None on error.
    Specials (season 0) are excluded.
    """
    try:
        data = _tmdb_request(f"/tv/{tmdb_id}")
    except Exception:
        return None
    seasons = data.get("seasons") or []
    result = {}
    for s in seasons:
        num = s.get("season_number")
        ep = s.get("episode_count")
        if num is None or num == 0 or ep is None:
            continue
        result[int(num)] = int(ep)
    return result


def _count_season_episodes(season_dir):
    """Count video files directly inside a Season NN folder."""
    try:
        entries = os.listdir(season_dir)
    except OSError:
        return 0
    return sum(
        1 for e in entries
        if os.path.isfile(os.path.join(season_dir, e))
        and os.path.splitext(e)[1].lower() in VIDEO_EXTENSIONS
    )


def _find_orphan_subtitles(folder_path):
    """
    Return a list of subtitle filenames (basename only) that have no
    video counterpart in the same folder — i.e. no video file shares
    the subtitle's base stem prefix.
    """
    try:
        entries = os.listdir(folder_path)
    except OSError:
        return []
    videos = [e for e in entries
              if os.path.isfile(os.path.join(folder_path, e))
              and os.path.splitext(e)[1].lower() in VIDEO_EXTENSIONS]
    if not videos:
        # No videos at all → can't judge orphan-ness here (maybe TV root)
        return []
    video_stems = [os.path.splitext(v)[0] for v in videos]
    orphans = []
    for e in entries:
        full = os.path.join(folder_path, e)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(e)[1].lower()
        if ext not in SUBTITLE_EXTENSIONS:
            continue
        sub_stem = os.path.splitext(e)[0]
        # Subtitle "Movie.ger.srt" pairs with "Movie.mkv" — prefix match
        if not any(sub_stem == v or sub_stem.startswith(v + ".")
                   for v in video_stems):
            orphans.append(e)
    return orphans


def _namecheck_folder(folder_path, folder_name, is_tv_root):
    """
    Audit one top-level library folder. Returns a list of finding dicts.
    Each finding: {"kind": str, "detail": str}.
    """
    findings = []
    id_type, id_value = _extract_id_from_tag(folder_name)
    if not id_type:
        findings.append({"kind": "missing-tag",
                         "detail": "no medianame ID tag found"})

    # Orphan subtitles (movie folder: check root; TV: check each season)
    if is_tv_root:
        try:
            subs = [s for s in os.listdir(folder_path)
                    if os.path.isdir(os.path.join(folder_path, s))
                    and _SEASON_DIR_RE.match(s)]
        except OSError:
            subs = []
        # TV: season-completeness check requires TMDB + known tmdb id
        tmdb_counts = None
        if id_type and TMDB_TOKEN:
            tmdb_id = id_value
            if id_type == "imdb":
                try:
                    tmdb_id = get_tmdb_id_from_imdb(id_value, "tv")
                except Exception:
                    tmdb_id = None
            if tmdb_id:
                tmdb_counts = _tmdb_season_episode_counts(tmdb_id)

        for season in sorted(subs):
            season_path = os.path.join(folder_path, season)
            m = _SEASON_DIR_RE.match(season)
            season_num = int(m.group(1)) if m else None
            found = _count_season_episodes(season_path)
            if tmdb_counts is not None and season_num in tmdb_counts:
                expected = tmdb_counts[season_num]
                if found < expected:
                    findings.append({
                        "kind": "incomplete-season",
                        "detail": f"{season}: {found} episode(s), "
                                  f"TMDB reports {expected} "
                                  f"(missing {expected - found})",
                    })
            orphans = _find_orphan_subtitles(season_path)
            for o in orphans:
                findings.append({"kind": "orphan-subtitle",
                                 "detail": f"{season}/{o} (no matching video)"})
    else:
        orphans = _find_orphan_subtitles(folder_path)
        for o in orphans:
            findings.append({"kind": "orphan-subtitle",
                             "detail": f"{o} (no matching video)"})
    return findings


def _iter_namecheck_roots(explicit_path):
    """
    Decide which root folder(s) to scan. Returns a list of
    (path, is_tv) tuples.

    Priority:
      1. Explicit path → auto-detect type from content.
      2. Configured library paths (MOVIE_LIBRARY_PATH / SERIES_LIBRARY_PATH)
         — these are what Plex/Jellyfin actually indexes, so they're the
         right default target for an audit.
      3. Fall back to the working paths (MOVIE_PATH / SERIES_PATH) when no
         library paths are configured (single-folder setups).
    """
    if explicit_path:
        # Heuristic: if any immediate subfolder contains a Season NN/ folder,
        # treat as TV root; else movie root. Cheap and good enough.
        is_tv = False
        try:
            for entry in os.listdir(explicit_path):
                sub = os.path.join(explicit_path, entry)
                if not os.path.isdir(sub):
                    continue
                try:
                    children = os.listdir(sub)
                except OSError:
                    continue
                if any(_SEASON_DIR_RE.match(c) for c in children):
                    is_tv = True
                    break
        except OSError:
            pass
        return [(explicit_path, is_tv)]

    roots = []
    seen = set()
    movie_root = MOVIE_LIBRARY_PATH or MOVIE_PATH
    series_root = SERIES_LIBRARY_PATH or SERIES_PATH
    if movie_root and os.path.isdir(movie_root):
        roots.append((movie_root, False))
        seen.add(os.path.abspath(movie_root))
    if (series_root and os.path.isdir(series_root)
            and os.path.abspath(series_root) not in seen):
        roots.append((series_root, True))
    return roots


def _add_to_namecheck_ignore(name):
    """
    Persist `name` to the config's namecheck_ignore list and update the
    in-memory NAMECHECK_IGNORE set.
    """
    global NAMECHECK_IGNORE
    import config as _config
    cfg = _config.load_config() or {}
    extras = list(cfg.get("namecheck_ignore") or [])
    if not any(e.strip().lower() == name.strip().lower() for e in extras):
        extras.append(name)
        cfg["namecheck_ignore"] = extras
        _config.save_config(cfg)
    NAMECHECK_IGNORE = NAMECHECK_IGNORE | {name.strip().lower()}
    print(f"      ✅ Added to namecheck ignore list: {name}")


def _fix_missing_tag(folder_path, folder_name, root, is_tv):
    """
    Interactive fix for a folder missing its medianame ID tag.
    Searches TMDB using the parsed title/year, confirms with the user,
    then renames the folder in place. Returns True on success.
    """
    title, year = _split_title_year(folder_name)
    if not title:
        print("      ⚠️ Could not parse a title from the folder name.")
        return False
    print(f"      🔍 Searching TMDB for: {title}"
          + (f" ({year})" if year else ""))
    result = search_by_title(title, year_hint=year)
    if not result:
        print("      Skipped.")
        return False
    entry_id, media_type, _ = result
    if media_type == "tv":
        data = get_tmdb_details(entry_id, "tv")
    else:
        data = get_movie_data(entry_id)
    if not data or data.get("Response") != "True":
        print("      ❌ Could not fetch TMDB details.")
        return False

    clean_title, clean_year = _sanitize_title_year(
        data["Title"], data.get("Year", ""))
    want_id_type = _resolve_naming(media_type, NAMING_PRESET)
    id_value = _resolve_id_value(entry_id, media_type, want_id_type, data)
    if not id_value:
        print(f"      ❌ Could not resolve {want_id_type} ID.")
        return False
    new_name = format_folder_name(clean_title, clean_year,
                                   want_id_type, id_value, NAMING_PRESET)
    new_path = os.path.join(root, new_name)

    if os.path.abspath(new_path) == os.path.abspath(folder_path):
        print("      ℹ️ Folder already carries the correct name.")
        return True
    if os.path.exists(new_path):
        print(f"      ⚠️ Target already exists: {new_name} — not renaming.")
        return False
    try:
        answer = input(f"      Rename to: {new_name}? "
                       f"[Enter = yes, n = cancel]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer not in ("", "y", "yes", "j", "ja"):
        print("      Skipped.")
        return False
    try:
        os.rename(folder_path, new_path)
    except OSError as e:
        print(f"      ❌ Rename failed: {e}")
        return False
    print(f"      ✅ Renamed → {new_name}")
    return True


def _fix_orphan_subtitle(folder_path, detail):
    """
    Offer to delete an orphan subtitle file. `detail` looks like
    "Other.ger.srt (no matching video)" or "Season 01/foo.srt (…)".
    """
    # Strip the trailing " (no matching video)" suffix to get the path
    rel = detail.rsplit(" (", 1)[0].strip()
    target = os.path.join(folder_path, rel)
    if not os.path.isfile(target):
        print(f"      ⚠️ File not found: {target}")
        return False
    try:
        answer = input(f"      Delete {rel}? "
                       f"[Enter = yes, n = cancel]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer not in ("", "y", "yes", "j", "ja"):
        print("      Skipped.")
        return False
    try:
        os.remove(target)
    except OSError as e:
        print(f"      ❌ Delete failed: {e}")
        return False
    print(f"      ✅ Deleted {rel}")
    return True


# Findings that expose a [f]ix action and which handler implements it.
_FIXABLE_KINDS = {"missing-tag", "orphan-subtitle"}


def _remediate_finding(root, folder_path, folder_name, is_tv, finding):
    """
    Prompt the user with [f/i/s] options for one finding.

    Returns one of: "fixed", "ignored", "skipped", "abort".
    """
    kind = finding["kind"]
    fixable = kind in _FIXABLE_KINDS
    options = []
    if fixable:
        options.append("[f]ix")
    options.extend(["[i]gnore permanently", "[s]kip this run", "[a]bort"])
    prompt = f"      Action — {' / '.join(options)} (Enter = skip): "
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "abort"
    if answer == "a":
        return "abort"
    if answer == "i":
        _add_to_namecheck_ignore(folder_name)
        return "ignored"
    if answer in ("", "s"):
        return "skipped"
    if answer == "f" and fixable:
        if kind == "missing-tag":
            ok = _fix_missing_tag(folder_path, folder_name, root, is_tv)
        elif kind == "orphan-subtitle":
            ok = _fix_orphan_subtitle(folder_path, finding["detail"])
        else:
            ok = False
        return "fixed" if ok else "skipped"
    print(f"      ⚠️ Unknown option: {answer!r} — treating as skip.")
    return "skipped"


def process_namecheck(path=None, interactive=True):
    """
    Read-only audit of an existing library.

    By default, scans the *library* paths (movie_library_path,
    series_library_path) because those are what Plex/Jellyfin actually
    indexes. Falls back to the working paths when no library is
    configured. `path` overrides everything.

    When `interactive` is True and any findings exist, a remediation
    loop offers per-finding choices: fix / ignore permanently / skip.
    """
    roots = _iter_namecheck_roots(path)
    if not roots:
        print("❌ No folder to scan. Configure movie_library_path / "
              "series_library_path (or movie_path / series_path) "
              "or pass a path explicitly.")
        return

    total_folders = 0
    ignored_folders = 0
    all_reports = []      # list of (root, is_tv, folder_path, folder_name, findings)
    duplicate_ids = {}    # id → [(root, relpath), …]

    for root, is_tv in roots:
        kind_label = "TV" if is_tv else "Movies"
        print(f"🔍 Namecheck: {root}   ({kind_label})")
        try:
            entries = sorted(os.listdir(root))
        except OSError as e:
            print(f"  ❌ Cannot read: {e}")
            continue

        for name in entries:
            full = os.path.join(root, name)
            if not os.path.isdir(full) or name.startswith("."):
                continue
            if name.strip().lower() in NAMECHECK_IGNORE:
                ignored_folders += 1
                continue
            total_folders += 1
            id_type, id_value = _extract_id_from_tag(name)
            if id_type:
                key = f"{id_type}-{id_value}"
                duplicate_ids.setdefault(key, []).append(
                    (root, os.path.relpath(full, root)))
            findings = _namecheck_folder(full, name, is_tv)
            if findings:
                all_reports.append((root, is_tv, full, name, findings))

    # Cross-root duplicate IDs — turn into synthetic findings on the
    # *second and later* folders so remediation can offer "ignore" on each.
    dup_findings = []
    dups = {k: v for k, v in duplicate_ids.items() if len(v) > 1}
    for key, occurrences in sorted(dups.items()):
        peers = ", ".join(p for _, p in occurrences)
        for root, rel in occurrences:
            full = os.path.join(root, rel)
            dup_findings.append((root, None, full, rel,
                                 [{"kind": "duplicate-id",
                                   "detail": f"duplicate ID {key} "
                                             f"(also at: {peers})"}]))

    # Print all findings up-front for a clean overview
    total_findings = 0
    if all_reports or dup_findings:
        print()
    for root, _is_tv, _full, name, findings in all_reports:
        print(f" 📁 {name}")
        for f in findings:
            print(f"    └─ {f['detail']}")
        total_findings += len(findings)
    if dup_findings:
        print()
        print(" ⚠️  Duplicate IDs across folders:")
        for _r, _t, _f, rel, findings in dup_findings:
            print(f"    📁 {rel}")
            for f in findings:
                print(f"       └─ {f['detail']}")
            total_findings += len(findings)

    print()
    suffix = (f" (skipping {ignored_folders} previously-ignored folder(s))"
              if ignored_folders else "")
    if total_findings == 0:
        print(f"✅ {total_folders} folder(s) checked — all clean.{suffix}")
        return
    print(f"⚠️  {total_findings} issue(s) across "
          f"{total_folders} folder(s) checked.{suffix}")

    if not interactive:
        return

    try:
        go = input("\nRemediate interactively? "
                   "[Enter = yes, n = no]: ").strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return
    if go not in ("", "y", "yes", "j", "ja"):
        return

    all_to_process = all_reports + dup_findings
    for root, is_tv, full, name, findings in all_to_process:
        if not os.path.isdir(full):
            # Folder was renamed by a previous fix in this loop
            continue
        if name.strip().lower() in NAMECHECK_IGNORE:
            continue
        print()
        print(f" 📁 {name}")
        aborted = False
        for finding in findings:
            print(f"    └─ {finding['detail']}")
            outcome = _remediate_finding(root, full, name, is_tv, finding)
            if outcome == "abort":
                aborted = True
                break
            if outcome in ("ignored", "fixed"):
                # Further findings on the same folder are moot after
                # a rename or permanent-ignore: break out.
                break
        if aborted:
            print("\nAborted.")
            return


# ===========================================================================
# healthcheck — verify the installation is sane: config readable, TMDB
# token works, paths exist and are writable, dependencies importable.
# ===========================================================================

def _hc_row(status, label, detail=""):
    icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}.get(status, "  ")
    tail = f" — {detail}" if detail else ""
    print(f" {icon} {label}{tail}")


def _hc_check_path(label, path, required=True):
    if not path:
        if required:
            _hc_row("fail", label, "not configured")
            return "fail"
        _hc_row("warn", label, "not configured (optional)")
        return "warn"
    if not os.path.isdir(path):
        _hc_row("fail", label, f"missing: {path}")
        return "fail"
    if not os.access(path, os.W_OK):
        _hc_row("fail", label, f"not writable: {path}")
        return "fail"
    _hc_row("ok", label, path)
    return "ok"


def process_healthcheck():
    """
    Run a series of environment checks and print a verdict. Exits
    without side effects. Intended as a self-test after setup or for
    support diagnostics.
    """
    import config
    print("🩺 medianame healthcheck")
    print()

    statuses = []
    # Config file
    cfg = config.load_config()
    if cfg is None:
        _hc_row("fail", "Config file",
                f"{config.CONFIG_PATH} missing or invalid")
        print()
        print("❌ Cannot proceed — run `medianame setup`.")
        return
    _hc_row("ok", "Config file", config.CONFIG_PATH)
    statuses.append("ok")

    # Prime TMDB token into module global so _tmdb_request() can use it
    global TMDB_TOKEN
    TMDB_TOKEN = cfg.get("tmdb_token")

    # TMDB token: tiny round-trip call
    try:
        data = _tmdb_request("/configuration")
        if data and "images" in data:
            _hc_row("ok", "TMDB token", "API reachable, token accepted")
            statuses.append("ok")
        else:
            _hc_row("fail", "TMDB token",
                    "unexpected response from /configuration")
            statuses.append("fail")
    except Exception as e:
        _hc_row("fail", "TMDB token", f"request failed ({e})")
        statuses.append("fail")

    # Working paths
    statuses.append(_hc_check_path("Movie working folder",
                                    cfg.get("movie_path"), required=True))
    statuses.append(_hc_check_path("TV working folder",
                                    cfg.get("series_path"), required=True))
    # Library paths (optional)
    statuses.append(_hc_check_path("Movie library folder",
                                    cfg.get("movie_library_path") or "",
                                    required=False))
    statuses.append(_hc_check_path("TV library folder",
                                    cfg.get("series_library_path") or "",
                                    required=False))

    # Dependencies
    try:
        import guessit  # noqa: F401
        ver = getattr(guessit, "__version__", "unknown")
        _hc_row("ok", "guessit", f"installed ({ver})")
        statuses.append("ok")
    except ImportError:
        _hc_row("fail", "guessit", "missing — reinstall medianame")
        statuses.append("fail")

    # Legacy OMDb key still in config?
    if cfg.get("omdb_api_key"):
        _hc_row("warn", "Legacy `omdb_api_key`",
                "ignored since v1.4.0 — can be removed from config.json")
        statuses.append("warn")

    fails = statuses.count("fail")
    warns = statuses.count("warn")
    print()
    if fails:
        print(f"❌ {fails} check(s) failed, {warns} warning(s).")
    elif warns:
        print(f"✅ All critical checks passed ({warns} warning(s)).")
    else:
        print(f"✅ All {len(statuses)} checks passed.")


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
    parser.add_argument("--publish", action="store_true",
                        help="Scan: force the publish step (error out if no "
                             "library paths are configured)")
    parser.add_argument("--no-publish", action="store_true",
                        help="Scan: skip the publish step even if library "
                             "paths are configured")
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
        if args.publish and args.no_publish:
            print("❌ --publish and --no-publish are mutually exclusive.")
            return
        operation = "copy" if args.copy else ("move" if args.move else None)
        # CLI flag overrides config; if absent, fall back to configured default.
        max_age = args.max_age_days if args.max_age_days is not None \
            else SCAN_MAX_AGE_DAYS
        publish_mode = "force" if args.publish else (
            "off" if args.no_publish else "auto")
        process_scan(source_path=scan_path, operation=operation,
                     preset_override=args.preset,
                     max_age_days=max_age,
                     publish_mode=publish_mode)
        return

    # `medianame namecheck [<path>]` — read-only library audit
    if args.title and args.title[0].lower() == "namecheck":
        _load_config()
        nc_path = " ".join(args.title[1:]) if len(args.title) > 1 else None
        process_namecheck(path=nc_path)
        return

    # `medianame healthcheck` — environment diagnostics
    if args.title and args.title[0].lower() == "healthcheck":
        # Don't force setup — healthcheck should also help users diagnose
        # a missing or broken config.
        process_healthcheck()
        return

    # `medianame publish [<path>]`
    if args.title and args.title[0].lower() == "publish":
        _load_config()
        publish_path = " ".join(args.title[1:]) if len(args.title) > 1 else None
        process_publish(source_path=publish_path, operation="move")
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
