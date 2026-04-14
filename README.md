# medianame

[![Tests](https://github.com/psclkoch/medianame/actions/workflows/tests.yml/badge.svg)](https://github.com/psclkoch/medianame/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

CLI tool to create [Plex](https://www.plex.tv/)- or [Jellyfin](https://jellyfin.org/)-compatible folder structures for movies and TV shows.

Search by title, confirm, done — medianame creates properly named folders with the correct metadata tags so your media server can match them automatically.

## What it does

```
$ medianame inception
  🔍 Inception (2010) — Movie — starring Leonardo DiCaprio, Joseph Gordon-Levitt
     Correct? (Enter/n):
  ✅ tt1375666 confirmed.
✅ Created: Inception (2010) {imdb-tt1375666}

$ medianame breaking bad
  🔍 Breaking Bad (2008) — TV Show — starring Bryan Cranston, Aaron Paul
     Correct? (Enter/n):
  📺 Seasons to create (TMDB: 5, Enter = accept):
  ✅ tmdb-1396 confirmed.
✅ Created: Breaking Bad (2008) {tmdb-1396}
   ✅ Season 01
   ✅ Season 02
   ✅ Season 03
   ✅ Season 04
   ✅ Season 05
```

### Naming conventions

medianame supports two naming presets:

**Plex** — [naming docs](https://support.plex.tv/articles/naming-and-organizing-your-movie-media-files/):
- Movies: `Title (Year) {imdb-ttXXXXXXX}`
- TV shows: `Title (Year) {tmdb-XXXXX}` with `Season 01`, `Season 02`, … subfolders

**Jellyfin** — [naming docs](https://jellyfin.org/docs/general/server/media/movies):
- Movies: `Title (Year) [imdbid-ttXXXXXXX]` or `[tmdbid-XXXXX]`
- TV shows: `Title (Year) [tmdbid-XXXXX]` or `[imdbid-ttXXXXXXX]`

Unlike Plex, Jellyfin lets you choose either IMDb or TMDB IDs for both media types — configurable during setup.

## Installation

**Requirements:** Python 3.9+ and [pipx](https://pipx.pypa.io/) (recommended) or pip.

```bash
# Clone the repository
git clone https://github.com/psclkoch/medianame.git
cd medianame

# Install with pipx (recommended)
pipx install -e .

# Or with pip
pip install -e .
```

On first run, medianame will walk you through the setup:

```
$ medianame
Not configured yet. Starting setup...

==================================================
  🎬 medianame — First-time setup
==================================================

ℹ️ Press Enter at any prompt with a default in [brackets] to accept it.

1) OMDb API Key (for movie lookups via IMDb ID)
2) TMDB Read Access Token (for title search, TV shows, cast)
3) Movie working folder (library root, or a staging area — see step 12)
4) TV show working folder (library root, or a staging area — see step 13)
5) Media server (plex / jellyfin)
6) Movie ID source        (Jellyfin only — imdb / tmdb)
7) TV show ID source      (Jellyfin only — imdb / tmdb)
8) Default operation for `scan` (move / copy)
9) Minimum video size for `scan` (in MB, default 500)
10) Extra scan ignores (comma-separated; defaults cover #recycle, @eaDir, …)
11) Default max age (days) for `scan` (0 = no limit)
12) Movie library folder — optional Plex/Jellyfin target for `publish`
13) TV show library folder — optional Plex/Jellyfin target for `publish`

✅ Configuration saved: ~/.config/medianame/config.json
```

Your configuration is stored in `~/.config/medianame/config.json`. Run `medianame setup` at any time to change it — existing values are shown in brackets and kept by pressing Enter (nothing needs to be re-typed).

## Usage

```bash
medianame                        # Interactive mode — enter titles one by one
medianame <title>                # Direct search (e.g. medianame the matrix)
medianame -n <title>             # Dry run — show what would be created
medianame -o /path <title>       # Override target path for this run
medianame -f movies.txt          # Batch mode — process IMDb URLs from a file
medianame --preset jellyfin ...  # Override naming preset for this run
medianame scan [<path>]          # Scan a folder and move raw media into named folders
medianame scan --copy <path>     # Same, but copy instead of move
medianame scan --max-age-days 7  # Only scan entries modified in the last 7 days
medianame scan --no-publish      # Scan without the auto-publish step this run
medianame publish [<path>]       # Move finished folders into your Plex/Jellyfin library
medianame setup                  # (Re)configure API keys, paths, preset
medianame help                   # Show detailed help
```

### Scan mode (v1.2.0+)

If you already have raw downloads like `Movie.2011.2160p.BluRay.x265-SceneGROUP.mkv` or folders like `TV.Show.S01.1080p.REMUX-SceneGROUP/`, scan mode parses the release names, looks each title up on TMDB with confirmation, creates the correctly-named library folder, and moves (or copies) the relevant media files into it.

```bash
medianame scan                         # Interactive: pick movie / series / custom path
medianame scan ~/Downloads             # Scan a specific folder
medianame scan --copy <path>           # Keep the source (default is move)
medianame scan --max-age-days 7 <path> # Only look at entries modified in the last 7 days
```

**When scan can't resolve an entry** (e.g. a `Download Station` folder or a one-off mystery filename), you get a three-way prompt before it's skipped:

- `[i]` Add to ignore list — the name is persisted to your config's `scan_ignore` and skipped on every future scan, no extra setup required.
- `[m]` Enter title manually — useful when the parsed release name is wrong and you want to tell medianame the real title.
- `[s]` Skip (this run only) — default when you just press Enter.

What gets picked up:

- **Videos** (`.mkv .mp4 .avi .m4v .mov`) — only files above the configured minimum size are kept, so samples, trailers, and extras are filtered out. The threshold defaults to 500 MB and can be changed in `medianame setup` (step 9).
- **All subtitle files** (`.srt .ass .sub .idx .vtt`) — every language is preserved
- Filenames are kept as-is; only the enclosing folder gets renamed
- TV episodes land in `Season NN/` subfolders based on the parsed season number
- If a destination file already exists, you're prompted per conflict: skip / overwrite / abort

After a successful **move**, the original source folder is deleted along with any leftovers inside it (NFOs, screenshots, sample subfolders, …). The upfront confirmation prompt covers both the move and the cleanup in one step. When the operation is **copy**, the source is always preserved.

What is **automatically skipped** so scans on large library volumes stay fast:

- Folders that already carry a medianame ID tag (`{imdb-…}`, `{tmdb-…}`, `[imdbid-…]`, `[tmdbid-…]`) — they're assumed to be processed
- OS / NAS metadata: `#recycle`, `@eaDir`, `.Trash`, `lost+found`, `System Volume Information`, `$RECYCLE.BIN`, `.DS_Store`, …
- Anything you add as an extra ignore (setup step 10)
- Subfolders deeper than 2 levels inside a scan item (scene releases never nest deeper)
- Entries older than the configured max age, when set (setup step 11 or `--max-age-days N`)

The default operation (`move` or `copy`) is set during `medianame setup` (step 8) and can be overridden per run with `--copy` / `--move`. The default max-age is step 11 and can be overridden with `--max-age-days`.

> **Note on title matching.** TMDB search is driven only by the parsed title; the parsed year is used to re-rank results (preferring the matching year, then ±1). Appending the year to the query string would confuse TMDB's multi-search and return zero hits for otherwise obvious titles.

### Publish to library (v1.3.0+)

If you keep a **separate working area** (where medianame creates folders) from your **Plex/Jellyfin library root** (what Plex actually indexes), `publish` automates the last step: moving the finished, tag-named folders into the library.

Configure it during `medianame setup`:

- **Step 12** — Movie library folder (e.g. `/Volumes/Plex/Movies`)
- **Step 13** — TV show library folder (e.g. `/Volumes/Plex/TV`)

Leave empty to disable. When at least one of them is set, every successful `medianame scan` runs an interactive publish step at the end (suppress with `--no-publish`, force with `--publish`). Or run it manually:

```bash
medianame publish               # Publish everything from both staging folders
medianame publish ~/Downloads   # Publish tag-named folders from a specific path
```

Only folders carrying a medianame ID tag (`{imdb-…}`, `{tmdb-…}`, `[imdbid-…]`, `[tmdbid-…]`) are considered — untagged content in the staging area is left alone.

**Conflict handling.** Three cases trigger a prompt during publish:

- **Same-name file in target folder.** Shown side-by-side with filename, size, and modified date. Options: `[r]` replace, `[s]` skip, `[b]` keep both (new file gets ` (1)` suffix), `[a]` abort.
- **Different filename, but target already has a video.** Same comparison display. `[r]` replace all existing videos with the new one, `[s]` skip the new file, `[b]` keep both, `[a]` abort.
- **Similar library folder with a different ID tag** (or no tag). Four explicit options:
  - `[1]` Rename library folder to new, **keep old files** (discard new)
  - `[2]` Rename library folder to new, **replace with new files** (discard old)
  - `[3]` Rename library folder to new, **keep all files**
  - `[4]` Skip (leave both as-is)

**TV series specifics.** When a show already exists in the library:
- New seasons are added without asking.
- Existing seasons are merged episode-by-episode with the same same-name conflict prompt.
- If the new episode uses a different naming scheme than existing episodes in the season (e.g. `Show - S04E05.mkv` vs. `Show.S04E05.mkv`), you're warned and can keep the new name, skip, or abort.

**Progress indicator.** Cross-filesystem copies of files ≥ 100 MB show a per-file progress line (`Copying foo.mkv … 4.2 / 45.1 GB (9%)`). Same-filesystem moves are instant renames and skip the indicator.

**Staging cleanup.** After a successful publish, the staging folder is removed if empty. If some files stayed behind (skipped conflicts, leftover `.nfo` files, samples) you get a summary listing and a prompt whether to delete anyway.

### Input formats

In interactive mode, you can enter:

| Input | Result |
|---|---|
| `breaking bad` | Title search (movies + TV via TMDB) |
| `https://www.imdb.com/title/tt1375666/` | Direct IMDb URL → movie |
| `tt1375666` | IMDb ID → movie |
| `https://www.themoviedb.org/tv/1396-breaking-bad` | TMDB URL → TV show |

### Batch mode

Create a text file with one IMDb URL or `tt`-ID per line:

```
https://www.imdb.com/title/tt0133093/
https://www.imdb.com/title/tt1375666/
tt0167260
```

Then run:

```bash
medianame -f movies.txt
```

Successfully processed entries are automatically removed from the file (a `.bak` backup is created).

## How it works

1. **Title search** uses TMDB's `/search/multi` endpoint — one search returns both movies and TV shows
2. **Movies** are tagged with the preferred ID format (`{imdb-...}` / `[imdbid-...]` / `[tmdbid-...]`)
3. **TV shows** are tagged with the preferred ID format and get Season subfolders
4. When Jellyfin + TMDB movie IDs are requested from an IMDb input, `/find` resolves IMDb → TMDB
5. When Jellyfin + IMDb TV IDs are requested, `external_ids` on TMDB provides the IMDb ID
6. Folders are created directly in your configured library paths

## Upgrading

### From an earlier medianame release

```bash
cd ~/Desktop/IMDB   # or wherever your clone lives
git pull
pipx install -e . --force     # --force picks up new dependencies
```

New config fields introduced in later versions fall back to sensible defaults — your existing config continues to work. Run `medianame setup` to adjust them interactively. Recent additions:

- v1.2.0 — `default_operation`, `min_video_size_mb`
- v1.2.1 — `scan_ignore` (extra ignores on top of built-in defaults)
- v1.2.2 — `scan_max_age_days`
- v1.3.0 — `movie_library_path`, `series_library_path` (optional publish targets)

### From plexname (v1.0)

medianame v1.1 is the renamed successor of plexname v1.0. If you had v1.0 installed:

```bash
pipx uninstall plexname
cd ~/Desktop/IMDB
git pull
pipx install -e .
```

Your existing `~/.config/plexname/config.json` is **automatically migrated** to `~/.config/medianame/config.json` on first run. Plex naming is the default, so your existing folders continue to work without changes.

## API keys

medianame uses two free APIs:

- **[OMDb API](https://www.omdbapi.com/)** — for movie lookups by IMDb ID. Free tier: 1,000 requests/day.
- **[TMDB API](https://www.themoviedb.org/documentation/api)** — for title search, TV show data, and cast info. Free for non-commercial use.

## Dependencies

Runtime dependencies (installed automatically by `pip` / `pipx`):

- **[requests](https://pypi.org/project/requests/)** — HTTP client for the OMDb and TMDB APIs (Apache 2.0)
- **[guessit](https://pypi.org/project/guessit/)** — parser for scene-style release filenames, used by `medianame scan` (LGPL-3.0; pure Python)

## License

MIT
