"""
Configuration management for medianame.

Stores API keys and paths in ~/.config/medianame/config.json.
On first run, the user is prompted interactively for all values.

Legacy configs under ~/.config/plexname/config.json are migrated
automatically on first run of v1.1.
"""

import json
import os
import stat

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "medianame")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Legacy path from v1.0 (plexname) — used for one-time migration
LEGACY_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "plexname")
LEGACY_CONFIG_PATH = os.path.join(LEGACY_CONFIG_DIR, "config.json")

REQUIRED_KEYS = ["omdb_api_key", "tmdb_token", "movie_path", "series_path"]
# naming_preset, movie_id_source, series_id_source are optional
# (v1.0 configs default to plex).


def _migrate_legacy_config():
    """
    If a legacy plexname config exists and the new one doesn't, copy it over.
    Runs silently; only prints on actual migration.
    """
    if os.path.exists(CONFIG_PATH) or not os.path.exists(LEGACY_CONFIG_PATH):
        return
    try:
        with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    save_config(cfg)
    print(f"ℹ️ Migrated config from {LEGACY_CONFIG_PATH} → {CONFIG_PATH}")


def load_config():
    """
    Read configuration from ~/.config/medianame/config.json.

    Returns:
        dict with all config values, or None if the file is missing
        or incomplete.
    """
    _migrate_legacy_config()
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    for key in REQUIRED_KEYS:
        if not cfg.get(key):
            return None
    return cfg


def save_config(cfg):
    """Save configuration and set restrictive file permissions."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    # Owner read/write only (file contains API tokens)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)


def run_setup():
    """
    Interactive setup: prompts the user for API keys and paths.

    Returns:
        dict with the complete configuration.
    """
    print("=" * 50)
    print("  🎬 medianame — First-time setup")
    print("=" * 50)

    existing = load_config() or {}

    # 1. OMDb API Key
    print()
    print("1) OMDb API Key (for movie lookups via IMDb ID)")
    print("   Get a free key: https://www.omdbapi.com/apikey.aspx")
    print("   → Choose 'FREE', enter your email, key arrives by mail.")
    default = existing.get("omdb_api_key", "")
    omdb_key = _prompt_value("   API Key", default)

    # 2. TMDB Token
    print()
    print("2) TMDB Read Access Token (for title search, TV shows, cast)")
    print("   Create account: https://www.themoviedb.org/signup")
    print("   Get token: https://www.themoviedb.org/settings/api")
    print("   → Copy the long 'API Read Access Token' (not the short API key).")
    default = existing.get("tmdb_token", "")
    tmdb_token = _prompt_value("   Token", default)

    # 3. Movie path
    print()
    print("3) Plex movie folder (root of your movie library)")
    print("   Example: /Volumes/NAS/Movies or /mnt/media/movies")
    default = existing.get("movie_path", "")
    movie_path = _prompt_value("   Path", default)

    # 4. Series path
    print()
    print("4) TV show folder (root of your series library)")
    print("   Example: /Volumes/NAS/TV or /mnt/media/tv")
    default = existing.get("series_path", "")
    series_path = _prompt_value("   Path", default)

    # 5. Naming preset
    print()
    print("5) Media server (determines folder naming convention)")
    print("   plex     → \"Title (Year) {imdb-ttXXX}\"  /  \"Title (Year) {tmdb-XXX}\"")
    print("   jellyfin → \"Title (Year) [imdbid-ttXXX]\" / \"Title (Year) [tmdbid-XXX]\"")
    default_preset = existing.get("naming_preset", "plex")
    preset = _prompt_choice("   Preset (plex/jellyfin)", ["plex", "jellyfin"], default_preset)

    # 6. + 7. ID source (only matters for Jellyfin — Plex is fixed)
    if preset == "jellyfin":
        print()
        print("6) Movie ID source")
        print("   imdb → use IMDb IDs for movies (default, recommended)")
        print("   tmdb → use TMDB IDs for movies")
        default_movie_source = existing.get("movie_id_source", "imdb")
        movie_id_source = _prompt_choice("   Movie IDs (imdb/tmdb)",
                                          ["imdb", "tmdb"], default_movie_source)

        print()
        print("7) TV show ID source")
        print("   tmdb → use TMDB IDs for TV shows (default, recommended)")
        print("   imdb → use IMDb IDs for TV shows")
        default_series_source = existing.get("series_id_source", "tmdb")
        series_id_source = _prompt_choice("   TV show IDs (imdb/tmdb)",
                                           ["imdb", "tmdb"], default_series_source)
    else:
        # Plex: fixed conventions
        movie_id_source = "imdb"
        series_id_source = "tmdb"

    # 8. Default operation for `medianame scan`
    print()
    print("8) Default operation for `medianame scan`")
    print("   move → move files into the library folders (source is emptied)")
    print("   copy → copy files (source is preserved)")
    default_operation = existing.get("default_operation", "move")
    default_operation = _prompt_choice("   Operation (move/copy)",
                                        ["move", "copy"], default_operation)

    cfg = {
        "omdb_api_key": omdb_key,
        "tmdb_token": tmdb_token,
        "movie_path": movie_path,
        "series_path": series_path,
        "naming_preset": preset,
        "movie_id_source": movie_id_source,
        "series_id_source": series_id_source,
        "default_operation": default_operation,
    }

    save_config(cfg)
    print()
    print(f"✅ Configuration saved: {CONFIG_PATH}")
    print("   Reconfigure anytime: medianame setup")
    print()
    return cfg


def get_config():
    """
    Load configuration. Starts setup if not yet configured.

    Returns:
        dict with all config values.
    """
    cfg = load_config()
    if cfg is not None:
        return cfg
    print("Not configured yet. Starting setup...\n")
    return run_setup()


def _prompt_value(label, default=""):
    """Prompt for a value, showing the existing value as a default if available."""
    if default:
        preview = default if len(default) <= 30 else default[:20] + "..." + default[-7:]
        entry = input(f"{label} [{preview}]: ").strip()
        return entry if entry else default
    else:
        while True:
            entry = input(f"{label}: ").strip()
            if entry:
                return entry
            print("   Input required.")


def _prompt_choice(label, choices, default):
    """Prompt for a choice from a fixed list. Empty input → default."""
    while True:
        entry = input(f"{label} [{default}]: ").strip().lower()
        if not entry:
            return default
        if entry in choices:
            return entry
        print(f"   Please enter one of: {', '.join(choices)}")
