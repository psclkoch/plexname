# plexname

CLI tool to create [Plex](https://www.plex.tv/)-compatible folder structures for movies and TV shows.

Search by title, confirm, done — plexname creates properly named folders with the correct metadata tags so Plex can match them automatically.

## What it does

```
$ plexname inception
  🔍 Inception (2010) — Movie — starring Leonardo DiCaprio, Joseph Gordon-Levitt
     Correct? (Enter/n):
  ✅ tt1375666 confirmed.
✅ Created: Inception (2010) {imdb-tt1375666}

$ plexname breaking bad
  🔍 Breaking Bad (2008) — TV Show — starring Bryan Cranston, Aaron Paul
     Correct? (Enter/n):
  📺 Create seasons (TMDB: 5, Enter = accept):
  ✅ tmdb-1396 confirmed.
✅ Created: Breaking Bad (2008) {tmdb-1396}
   ✅ Season 01
   ✅ Season 02
   ✅ Season 03
   ✅ Season 04
   ✅ Season 05
```

**Movies** get folders named `Title (Year) {imdb-ttXXXXXXX}` — the [Plex naming convention](https://support.plex.tv/articles/naming-and-organizing-your-movie-media-files/) for automatic matching via IMDb.

**TV shows** get folders named `Title (Year) {tmdb-XXXXX}` with `Season 01`, `Season 02`, ... subfolders — the [Plex naming convention](https://support.plex.tv/articles/naming-and-organizing-your-tv-files/) for series.

## Installation

**Requirements:** Python 3.9+ and [pipx](https://pipx.pypa.io/) (recommended) or pip.

```bash
# Clone the repository
git clone https://github.com/psclkoch/plexname.git
cd plexname

# Install with pipx (recommended)
pipx install -e .

# Or with pip
pip install -e .
```

On first run, plexname will walk you through the setup:

```
$ plexname
Not configured yet. Starting setup...

==================================================
  🎬 plexname — First-time setup
==================================================

1) OMDb API Key (for movie lookups via IMDb ID)
   Get a free key: https://www.omdbapi.com/apikey.aspx
   → Choose 'FREE', enter your email, key arrives by mail.
   API Key: ________

2) TMDB Read Access Token (for title search, TV shows, cast)
   Create account: https://www.themoviedb.org/signup
   Get token: https://www.themoviedb.org/settings/api
   → Copy the long 'API Read Access Token' (not the short API key).
   Token: ________

3) Plex movie folder (root of your movie library)
   Example: /Volumes/NAS/Movies or /mnt/media/movies
   Path: ________

4) Plex TV show folder (root of your series library)
   Example: /Volumes/NAS/TV or /mnt/media/tv
   Path: ________

✅ Configuration saved: ~/.config/plexname/config.json
```

Your configuration is stored in `~/.config/plexname/config.json`. Run `plexname setup` at any time to change it.

## Usage

```bash
plexname                        # Interactive mode — enter titles one by one
plexname <title>                # Direct search (e.g. plexname the matrix)
plexname -n <title>             # Dry run — show what would be created
plexname -o /path <title>       # Override target path for this run
plexname -f movies.txt          # Batch mode — process IMDb URLs from a file
plexname setup                  # (Re)configure API keys and paths
plexname help                   # Show detailed help
```

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
plexname -f movies.txt
```

Successfully processed entries are automatically removed from the file (a `.bak` backup is created).

## How it works

1. **Title search** uses TMDB's `/search/multi` endpoint — one search returns both movies and TV shows
2. **Movies** are tagged with their IMDb ID (`{imdb-ttXXXXXXX}`), fetched via OMDb or TMDB
3. **TV shows** are tagged with their TMDB ID (`{tmdb-XXXXX}`) and get Season subfolders
4. Folders are created directly in your configured Plex library paths

## API keys

plexname uses two free APIs:

- **[OMDb API](https://www.omdbapi.com/)** — for movie lookups by IMDb ID. Free tier: 1,000 requests/day.
- **[TMDB API](https://www.themoviedb.org/documentation/api)** — for title search, TV show data, and cast info. Free for non-commercial use.

## License

MIT
