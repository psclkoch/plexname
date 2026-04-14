"""
Tests for medianame.py — Plex/Jellyfin folder creation from IMDb links / TMDB.
"""
import os
import re
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import medianame


class TestMovieFix(unittest.TestCase):
    """Test scenarios for medianame.py"""

    def setUp(self):
        """Create temporary directories for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.temp_input_dir = tempfile.mkdtemp()
        self.original_movie_path = medianame.MOVIE_PATH
        self.original_input_file = medianame.INPUT_FILE
        medianame.MOVIE_PATH = self.temp_dir
        # Clear module caches so state doesn't leak between tests
        medianame._movie_cache.clear()
        medianame._tmdb_cache.clear()
        # Safety net: prevent real network calls. Individual tests override
        # this via `with patch("medianame._tmdb_request", ...)` as needed.
        self._tmdb_patcher = patch("medianame._tmdb_request", return_value={"results": []})
        self._tmdb_patcher.start()

    def tearDown(self):
        """Clean up."""
        self._tmdb_patcher.stop()
        medianame.MOVIE_PATH = self.original_movie_path
        medianame.INPUT_FILE = self.original_input_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        shutil.rmtree(self.temp_input_dir, ignore_errors=True)

    def test_imdb_id_extraction(self):
        """tt-number is extracted from various URL formats."""
        test_cases = [
            ("https://www.imdb.com/title/tt0133093/", "tt0133093"),
            ("https://imdb.com/title/tt0133093", "tt0133093"),
            ("https://www.imdb.com/title/tt0133093/reviews", "tt0133093"),
            ("tt0133093", "tt0133093"),
            ("  tt0133093  ", "tt0133093"),
        ]
        for url, expected in test_cases:
            match = re.search(r'tt\d+', url.strip())
            self.assertIsNotNone(match, f"No match for: {url}")
            self.assertEqual(match.group(), expected)

    def test_invalid_url_skipped(self):
        """Invalid URLs are skipped — falls through to prompt mode with empty input."""
        medianame.INPUT_FILE = self._create_input_file([
            "https://www.google.com",
            "no-tt-number",
            "  ",
        ])
        with patch('medianame.get_movie_data', return_value=None):
            with patch('builtins.input', return_value=""):
                medianame.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_valid_movie_creates_folder(self):
        """Valid movie creates a Plex-format folder."""
        medianame.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/"
        ])
        mock_response = {
            "Response": "True",
            "Title": "The Matrix",
            "Year": "1999",
        }
        with patch('medianame.get_movie_data', return_value=mock_response):
            medianame.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])
        self.assertIn("(1999)", folders[0])
        self.assertIn("{imdb-tt0133093}", folders[0])

    def test_special_characters_removed(self):
        """Special characters are removed from folder names."""
        medianame.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/"
        ])
        mock_response = {
            "Response": "True",
            "Title": "Star Wars: Episode IV - A New Hope",
            "Year": "1977",
        }
        with patch('medianame.get_movie_data', return_value=mock_response):
            medianame.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertNotIn(":", folders[0])
        self.assertNotIn("/", folders[0])
        self.assertNotIn("\\", folders[0])

    def test_duplicate_not_recreated(self):
        """Already existing folder is not recreated."""
        medianame.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/",
            "https://www.imdb.com/title/tt0133093/",  # duplicate
        ])
        mock_response = {
            "Response": "True",
            "Title": "The Matrix",
            "Year": "1999",
        }
        with patch('medianame.get_movie_data', return_value=mock_response):
            medianame.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_api_error_handled(self):
        """API errors are handled gracefully."""
        medianame.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt9999999/"
        ])
        mock_response = {"Response": "False", "Error": "Incorrect IMDb ID"}
        with patch('medianame.get_movie_data', return_value=mock_response):
            medianame.process_list()

        self.assertEqual(len(self._get_created_folders()), 0)

    def test_path_not_found_aborts(self):
        """Aborts when target path does not exist."""
        medianame.MOVIE_PATH = "/nonexistent/path/xyz123"
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        with patch('medianame.get_movie_data') as mock_api:
            medianame.process_list()
            mock_api.assert_not_called()

    def test_empty_input_file(self):
        """Empty file → prompt mode; empty input → no API call."""
        medianame.INPUT_FILE = self._create_input_file([])
        with patch('medianame.get_movie_data') as mock_api:
            with patch('builtins.input', return_value=""):
                medianame.process_list()
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_deduplication_single_api_call(self):
        """Duplicate entries in input file → only 1 API call per tt-ID."""
        medianame.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/",
            "tt0133093",
            "https://imdb.com/title/tt0133093/reviews",
        ])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response) as mock_api:
            medianame.process_list()
        self.assertEqual(mock_api.call_count, 1)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_dry_run_creates_nothing(self):
        """Dry run creates no folders."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list(dry_run=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_year_n_a_handling(self):
        """Year 'N/A' from OMDb is handled (no / in path)."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "N/A",
        }
        with patch('medianame.get_movie_data', return_value=mock_response):
            medianame.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("NA", folders[0])
        self.assertNotIn("/", folders[0])

    def test_interactive_confirm_creates_folders(self):
        """Interactive mode with 'j' → folders are created."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="j"):
                medianame.process_list(interactive=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_interactive_decline_creates_nothing(self):
        """Interactive mode with 'n' → no folders created."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="n"):
                medianame.process_list(interactive=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_interactive_all_exist_no_prompt(self):
        """Interactive mode, all folders exist → input() is not called."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        os.makedirs(os.path.join(self.temp_dir, "The Matrix (1999) {imdb-tt0133093}"))
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input") as mock_input:
                medianame.process_list(interactive=True)
                mock_input.assert_not_called()

    def test_interactive_path_missing_on_confirm_aborts(self):
        """Interactive mode, target path missing on confirm → no folder created."""
        medianame.MOVIE_PATH = "/nonexistent/path/xyz789"
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="j"):
                medianame.process_list(interactive=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_year_range_extraction(self):
        """Year range '1999–2000' is reduced to the first year."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "1999–2000",  # en-dash
        }
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("(1999)", folders[0])
        self.assertNotIn("2000", folders[0])

    def test_year_range_ascii_hyphen(self):
        """Year range '1999-2000' (ASCII hyphen) is reduced to the first year."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "1999-2000",
        }
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("(1999)", folders[0])

    def test_utf8_umlauts_in_title(self):
        """Umlauts in movie title are preserved correctly."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "München",
            "Year": "2005",
        }
        with patch('medianame.get_movie_data', return_value=mock_response):
            medianame.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("München", folders[0])
        self.assertIn("(2005)", folders[0])

    def test_tt_id_in_middle_of_line(self):
        """tt-number in the middle of a line is recognized."""
        medianame.INPUT_FILE = self._create_input_file([
            "See tt0133093 for details",
        ])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("tt0133093", folders[0])

    def test_multiple_different_movies(self):
        """Multiple movies → multiple folders."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093", "tt0167260"])
        def mock_get_movie(imdb_id):
            if imdb_id == "tt0133093":
                return {"Response": "True", "Title": "The Matrix", "Year": "1999"}
            return {"Response": "True", "Title": "The Lord of the Rings", "Year": "2003"}
        with patch("medianame.get_movie_data", side_effect=mock_get_movie):
            medianame.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 2)
        folder_names = " ".join(folders)
        self.assertIn("The Matrix", folder_names)
        self.assertIn("The Lord of the Rings", folder_names)

    def test_get_movie_data_returns_none(self):
        """API error (None) → no folder, no crash."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        with patch("medianame.get_movie_data", return_value=None):
            medianame.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_interactive_accepts_ja_as_confirmation(self):
        """Interactive mode accepts 'ja' as confirmation."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="ja"):
                medianame.process_list(interactive=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_prompt_mode_creates_folder_from_input(self):
        """Prompt mode: entered link creates a folder, movies.txt is not used."""
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                medianame.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_prompt_mode_empty_input_creates_nothing(self):
        """Prompt mode with immediate empty input → no processing."""
        with patch("medianame.get_movie_data") as mock_api:
            with patch("builtins.input", return_value=""):
                medianame.process_list(prompt_mode=True)
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_prompt_mode_multiple_links(self):
        """Prompt mode with multiple links → multiple folders."""
        def mock_get_movie(imdb_id):
            if imdb_id == "tt0133093":
                return {"Response": "True", "Title": "The Matrix", "Year": "1999"}
            return {"Response": "True", "Title": "Inception", "Year": "2010"}
        with patch("medianame.get_movie_data", side_effect=mock_get_movie):
            with patch("builtins.input", side_effect=["tt0133093", "tt1375666", ""]):
                medianame.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 2)
        self.assertIn("The Matrix", " ".join(folders))
        self.assertIn("Inception", " ".join(folders))

    def test_prompt_mode_invalid_input_then_valid(self):
        """Prompt mode: invalid input is skipped, valid input is processed."""
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["invalid", "tt0133093", ""]):
                medianame.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_remove_processed_links_creates_backup(self):
        """After processing, links are removed from file and a backup is created."""
        input_path = self._create_input_file(["tt0133093"])
        medianame.INPUT_FILE = input_path
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list()
        with open(input_path, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("tt0133093", content)
        self.assertTrue(os.path.exists(input_path + ".bak"))
        with open(input_path + ".bak", encoding="utf-8") as f:
            bak_content = f.read()
        self.assertIn("tt0133093", bak_content)

    def test_prompt_mode_does_not_modify_file(self):
        """Prompt mode does not modify movies.txt (use_from_file=False)."""
        input_path = self._create_input_file(["tt0133093"])
        medianame.INPUT_FILE = input_path
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                medianame.process_list(prompt_mode=True)
        with open(input_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("tt0133093", content)
        self.assertFalse(os.path.exists(input_path + ".bak"))

    def test_custom_output_path(self):
        """-o overrides target path."""
        custom_dir = os.path.join(self.temp_dir, "custom_movies")
        os.makedirs(custom_dir)
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list(output_path=custom_dir)
        folders = [f for f in os.listdir(custom_dir) if os.path.isdir(os.path.join(custom_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_custom_input_file(self):
        """-f overrides input file."""
        other_input = os.path.join(self.temp_input_dir, "other.txt")
        with open(other_input, "w", encoding="utf-8") as f:
            f.write("tt0133093\n")
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list(input_file=other_input)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_file_not_found(self):
        """Missing input file → error message, no processing."""
        medianame.INPUT_FILE = "/nonexistent/file_xyz.txt"
        with patch("medianame.get_movie_data") as mock_api:
            medianame.process_list()
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_empty_file_fallback_prompt_with_link(self):
        """Empty file → prompt mode → entered link is processed."""
        medianame.INPUT_FILE = self._create_input_file([])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                medianame.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    # --- TV show tests (TMDB) ---

    def test_series_prompt_creates_tmdb_folder_with_seasons(self):
        """TV show via title search creates folder with tmdb tag and Season subfolders."""
        self.series_dir = tempfile.mkdtemp()
        medianame.SERIES_PATH = self.series_dir
        search_response = {"results": [
            {"id": 1396, "media_type": "tv", "name": "Breaking Bad", "first_air_date": "2008-01-20"},
        ]}
        details_response = {
            "id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20",
            "number_of_seasons": 5,
            "credits": {"cast": [{"name": "Bryan Cranston"}, {"name": "Aaron Paul"}]},
        }
        with patch("medianame._tmdb_request", side_effect=[search_response, details_response]):
            with patch("builtins.input", side_effect=["breaking bad", "", "", ""]):
                medianame.process_list(prompt_mode=True)
        folders = [f for f in os.listdir(self.series_dir)
                   if os.path.isdir(os.path.join(self.series_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("Breaking Bad", folders[0])
        self.assertIn("{tmdb-1396}", folders[0])
        self.assertIn("(2008)", folders[0])
        series_path = os.path.join(self.series_dir, folders[0])
        season_dirs = sorted(os.listdir(series_path))
        self.assertEqual(len(season_dirs), 5)
        self.assertEqual(season_dirs[0], "Season 01")
        self.assertEqual(season_dirs[4], "Season 05")
        shutil.rmtree(self.series_dir, ignore_errors=True)

    def test_movie_via_tmdb_search_uses_imdb_tag(self):
        """Movie via TMDB title search creates folder with imdb tag."""
        search_response = {"results": [
            {"id": 27205, "media_type": "movie", "title": "Inception", "release_date": "2010-07-16"},
        ]}
        details_response = {
            "id": 27205, "title": "Inception", "release_date": "2010-07-16",
            "imdb_id": "tt1375666",
            "credits": {"cast": [{"name": "Leonardo DiCaprio"}]},
        }
        with patch("medianame._tmdb_request", side_effect=[search_response, details_response]):
            with patch("medianame.get_movie_data", return_value={
                "Response": "True", "Title": "Inception", "Year": "2010",
            }):
                with patch("builtins.input", side_effect=["inception", "", ""]):
                    medianame.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("Inception", folders[0])
        self.assertIn("{imdb-tt1375666}", folders[0])

    def test_tmdb_url_recognized_as_series(self):
        """TMDB URL is recognized as a TV show."""
        self.series_dir = tempfile.mkdtemp()
        medianame.SERIES_PATH = self.series_dir
        mock_details = {
            "Response": "True", "Title": "Breaking Bad", "Year": "2008",
            "Actors": "Bryan Cranston", "Seasons": 5,
        }
        with patch("medianame.get_tmdb_details", return_value=mock_details):
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/1396-breaking-bad", "", ""
            ]):
                medianame.process_list(prompt_mode=True)
        folders = [f for f in os.listdir(self.series_dir)
                   if os.path.isdir(os.path.join(self.series_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("{tmdb-1396}", folders[0])
        shutil.rmtree(self.series_dir, ignore_errors=True)

    def test_series_different_target_path(self):
        """TV shows go to SERIES_PATH, not MOVIE_PATH."""
        self.series_dir = tempfile.mkdtemp()
        medianame.SERIES_PATH = self.series_dir
        mock_details = {
            "Response": "True", "Title": "Breaking Bad", "Year": "2008",
            "Actors": "Bryan Cranston", "Seasons": 1,
        }
        with patch("medianame.get_tmdb_details", return_value=mock_details):
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/1396-breaking-bad", "", ""
            ]):
                medianame.process_list(prompt_mode=True)
        # Movie folder must be empty
        self.assertEqual(len(self._get_created_folders()), 0)
        # Series folder must have content
        series_folders = [f for f in os.listdir(self.series_dir)
                          if os.path.isdir(os.path.join(self.series_dir, f))]
        self.assertEqual(len(series_folders), 1)
        shutil.rmtree(self.series_dir, ignore_errors=True)

    # --- _prompt_seasons edge cases ---

    def test_prompt_seasons_known_count_accepts_enter(self):
        """Empty input with known season count → returns known count."""
        with patch("builtins.input", return_value=""):
            result = medianame._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 5)

    def test_prompt_seasons_numeric_override(self):
        """User enters a number → that number is used."""
        with patch("builtins.input", return_value="3"):
            result = medianame._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 3)

    def test_prompt_seasons_zero_clamped_to_one(self):
        """User enters 0 → clamped to 1."""
        with patch("builtins.input", return_value="0"):
            result = medianame._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 1)

    def test_prompt_seasons_negative_clamped_to_one(self):
        """User enters negative → clamped to 1."""
        with patch("builtins.input", return_value="-3"):
            result = medianame._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 1)

    def test_prompt_seasons_non_numeric_with_known(self):
        """Non-numeric input with known count → returns known count."""
        with patch("builtins.input", return_value="five"):
            result = medianame._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 5)

    def test_prompt_seasons_non_numeric_without_known(self):
        """Non-numeric input without known count → returns 1."""
        with patch("builtins.input", return_value="abc"):
            result = medianame._prompt_seasons(known_seasons=None)
        self.assertEqual(result, 1)

    def test_prompt_seasons_eof_returns_default(self):
        """EOFError during input → returns known count."""
        with patch("builtins.input", side_effect=EOFError()):
            result = medianame._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 5)

    def test_prompt_seasons_eof_no_known(self):
        """EOFError with no known count → returns 1."""
        with patch("builtins.input", side_effect=EOFError()):
            result = medianame._prompt_seasons(known_seasons=None)
        self.assertEqual(result, 1)

    # --- get_movie_data retry and cache ---

    def test_get_movie_data_uses_cache(self):
        """Second call with same id returns cached result without HTTP."""
        medianame._movie_cache["tt1234567"] = {"Response": "True", "Title": "Cached"}
        with patch("medianame.requests.get") as mock_get:
            result = medianame.get_movie_data("tt1234567")
        mock_get.assert_not_called()
        self.assertEqual(result["Title"], "Cached")

    def test_get_movie_data_retries_on_network_error(self):
        """Transient network errors trigger retries until success."""
        call_count = {"n": 0}

        class MockResponse:
            def json(self):
                return {"Response": "True", "Title": "OK", "Year": "2020"}

        def flaky_get(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise Exception("transient")
            return MockResponse()

        with patch("medianame.requests.get", side_effect=flaky_get):
            with patch("medianame.time.sleep"):
                result = medianame.get_movie_data("tt9999001")
        self.assertEqual(call_count["n"], 3)
        self.assertIsNotNone(result)
        self.assertEqual(result["Title"], "OK")

    def test_get_movie_data_all_retries_fail(self):
        """All retries fail → returns None."""
        with patch("medianame.requests.get", side_effect=Exception("down")):
            with patch("medianame.time.sleep"):
                result = medianame.get_movie_data("tt9999002")
        self.assertIsNone(result)

    # --- search_by_title Stage 2 (numbered list) ---

    def test_search_stage2_user_picks_number(self):
        """User rejects best match, picks number 2 from the list."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "First", "release_date": "2000-01-01"},
            {"id": 2, "media_type": "movie", "title": "Second", "release_date": "2001-01-01"},
            {"id": 3, "media_type": "tv", "name": "Third", "first_air_date": "2002-01-01"},
        ]}
        details_first = {"Response": "True", "Title": "First", "Year": "2000",
                         "Actors": "X", "imdbID": "tt0000001"}
        details_second = {"Response": "True", "Title": "Second", "Year": "2001",
                          "Actors": "Y", "imdbID": "tt0000002"}
        with patch("medianame._tmdb_request", return_value=search_response):
            with patch("medianame.get_tmdb_details",
                       side_effect=[details_first, details_second]):
                with patch("builtins.input", side_effect=["n", "2"]):
                    result = medianame.search_by_title("foo")
        self.assertEqual(result, ("tt0000002", "movie", None))

    def test_search_stage2_picks_tv_show(self):
        """Stage 2: user picks a TV show entry."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "First", "release_date": "2000-01-01"},
            {"id": 1396, "media_type": "tv", "name": "Breaking Bad",
             "first_air_date": "2008-01-01"},
        ]}
        details_first = {"Response": "True", "Title": "First", "Year": "2000",
                         "Actors": "X", "imdbID": "tt0000001"}
        details_tv = {"Response": "True", "Title": "Breaking Bad", "Year": "2008",
                      "Actors": "Bryan", "Seasons": 5}
        with patch("medianame._tmdb_request", return_value=search_response):
            with patch("medianame.get_tmdb_details",
                       side_effect=[details_first, details_tv]):
                with patch("builtins.input", side_effect=["n", "2", ""]):
                    result = medianame.search_by_title("foo")
        self.assertEqual(result, ("1396", "tv", 5))

    def test_search_stage2_invalid_number(self):
        """User picks out-of-range number → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Only", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Only", "Year": "2000",
                   "Actors": "X", "imdbID": "tt0000001"}
        with patch("medianame._tmdb_request", return_value=search_response):
            with patch("medianame.get_tmdb_details", return_value=details):
                with patch("builtins.input", side_effect=["n", "99"]):
                    result = medianame.search_by_title("foo")
        self.assertIsNone(result)

    def test_search_stage2_non_numeric_input(self):
        """User enters non-numeric choice in Stage 2 → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Only", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Only", "Year": "2000",
                   "Actors": "X", "imdbID": "tt0000001"}
        with patch("medianame._tmdb_request", return_value=search_response):
            with patch("medianame.get_tmdb_details", return_value=details):
                with patch("builtins.input", side_effect=["n", "abc"]):
                    result = medianame.search_by_title("foo")
        self.assertIsNone(result)

    def test_search_stage2_empty_skip(self):
        """User enters empty choice in Stage 2 → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Only", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Only", "Year": "2000",
                   "Actors": "X", "imdbID": "tt0000001"}
        with patch("medianame._tmdb_request", return_value=search_response):
            with patch("medianame.get_tmdb_details", return_value=details):
                with patch("builtins.input", side_effect=["n", ""]):
                    result = medianame.search_by_title("foo")
        self.assertIsNone(result)

    def test_search_no_results_returns_none(self):
        """TMDB returns empty result list → search returns None."""
        with patch("medianame._tmdb_request", return_value={"results": []}):
            result = medianame.search_by_title("nonexistent-xyz")
        self.assertIsNone(result)

    def test_search_movie_without_imdb_id(self):
        """Best-match movie without IMDb ID → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Obscure", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Obscure", "Year": "2000",
                   "Actors": "X", "imdbID": ""}
        with patch("medianame._tmdb_request", return_value=search_response):
            with patch("medianame.get_tmdb_details", return_value=details):
                with patch("builtins.input", return_value=""):
                    result = medianame.search_by_title("obscure")
        self.assertIsNone(result)

    def test_search_network_error(self):
        """Network error during search → returns None without crash."""
        with patch("medianame._tmdb_request", side_effect=Exception("net down")):
            result = medianame.search_by_title("anything")
        self.assertIsNone(result)

    # --- get_tmdb_details error paths ---

    def test_get_tmdb_details_network_error(self):
        """Network error during details fetch → returns None."""
        with patch("medianame._tmdb_request", side_effect=Exception("timeout")):
            result = medianame.get_tmdb_details("1396", "tv")
        self.assertIsNone(result)

    def test_get_tmdb_details_invalid_response(self):
        """TMDB returns response without 'id' → returns None."""
        with patch("medianame._tmdb_request",
                   return_value={"status_code": 34, "status_message": "Not found"}):
            result = medianame.get_tmdb_details("99999", "tv")
        self.assertIsNone(result)

    def test_tmdb_cache_avoids_duplicate_calls(self):
        """Second fetch of the same TMDB id uses the cache."""
        details_response = {
            "id": 1396, "name": "BB", "first_air_date": "2008-01-01",
            "number_of_seasons": 5, "credits": {"cast": []},
        }
        with patch("medianame._tmdb_request", return_value=details_response) as mock_req:
            medianame.get_tmdb_details("1396", "tv")
            medianame.get_tmdb_details("1396", "tv")
        self.assertEqual(mock_req.call_count, 1)

    # --- Interactive mode variants ---

    def test_interactive_cancel_on_confirm(self):
        """Interactive mode: user answers 'n' on final confirm → no folders."""
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="n"):
                medianame.process_list(interactive=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_dry_run_series_creates_nothing(self):
        """Dry run for TV show: prints preview, creates no folders."""
        self.series_dir = tempfile.mkdtemp()
        medianame.SERIES_PATH = self.series_dir
        search_response = {"results": [
            {"id": 1396, "media_type": "tv", "name": "Breaking Bad",
             "first_air_date": "2008-01-20"},
        ]}
        details_response = {
            "id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20",
            "number_of_seasons": 5,
            "credits": {"cast": [{"name": "Bryan Cranston"}]},
        }
        with patch("medianame._tmdb_request",
                   side_effect=[search_response, details_response]):
            with patch("builtins.input", side_effect=["breaking bad", "", ""]):
                medianame.process_list(dry_run=True, prompt_mode=True)
        folders = [f for f in os.listdir(self.series_dir)
                   if os.path.isdir(os.path.join(self.series_dir, f))]
        self.assertEqual(len(folders), 0)
        shutil.rmtree(self.series_dir, ignore_errors=True)

    # --- Input file edge cases ---

    def test_comment_lines_preserved(self):
        """# comment lines are preserved after processing."""
        input_path = self._create_input_file([
            "# My movies to process",
            "tt0133093",
            "",
            "# Inception next",
            "tt1375666",
        ])
        medianame.INPUT_FILE = input_path

        def mock_api(imdb_id):
            return {"Response": "True", "Title": f"Movie {imdb_id}", "Year": "2000"}

        with patch("medianame.get_movie_data", side_effect=mock_api):
            medianame.process_list()
        with open(input_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("# My movies to process", content)
        self.assertIn("# Inception next", content)
        self.assertNotIn("tt0133093", content)
        self.assertNotIn("tt1375666", content)

    def test_comment_only_file_falls_through_to_prompt(self):
        """File with only comments → falls through to prompt mode."""
        medianame.INPUT_FILE = self._create_input_file([
            "# just comments",
            "# no links here",
        ])
        with patch("builtins.input", return_value=""):
            medianame.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    # --- Seasons with double-digit formatting ---

    def test_twelve_seasons_formatting(self):
        """Shows with 10+ seasons get correctly formatted Season 10, 11, 12."""
        self.series_dir = tempfile.mkdtemp()
        medianame.SERIES_PATH = self.series_dir
        details_response = {
            "id": 999, "name": "Long Show", "first_air_date": "1990-01-01",
            "number_of_seasons": 12,
            "credits": {"cast": [{"name": "X"}]},
        }
        with patch("medianame._tmdb_request", return_value=details_response):
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/999-long-show", "", ""
            ]):
                medianame.process_list(prompt_mode=True)
        folders = [f for f in os.listdir(self.series_dir)
                   if os.path.isdir(os.path.join(self.series_dir, f))]
        self.assertEqual(len(folders), 1)
        series_path = os.path.join(self.series_dir, folders[0])
        seasons = sorted(os.listdir(series_path))
        self.assertEqual(len(seasons), 12)
        self.assertIn("Season 01", seasons)
        self.assertIn("Season 10", seasons)
        self.assertIn("Season 12", seasons)
        shutil.rmtree(self.series_dir, ignore_errors=True)

    # --- format_folder_name (pure function, all preset × id_type combinations) ---

    def test_format_name_plex_movie(self):
        self.assertEqual(
            medianame.format_folder_name("Inception", "2010", "imdb", "tt1375666", "plex"),
            "Inception (2010) {imdb-tt1375666}",
        )

    def test_format_name_plex_series(self):
        self.assertEqual(
            medianame.format_folder_name("Breaking Bad", "2008", "tmdb", "1396", "plex"),
            "Breaking Bad (2008) {tmdb-1396}",
        )

    def test_format_name_jellyfin_movie_imdb(self):
        self.assertEqual(
            medianame.format_folder_name("Inception", "2010", "imdb", "tt1375666", "jellyfin"),
            "Inception (2010) [imdbid-tt1375666]",
        )

    def test_format_name_jellyfin_movie_tmdb(self):
        self.assertEqual(
            medianame.format_folder_name("Inception", "2010", "tmdb", "27205", "jellyfin"),
            "Inception (2010) [tmdbid-27205]",
        )

    def test_format_name_jellyfin_series_tmdb(self):
        self.assertEqual(
            medianame.format_folder_name("Breaking Bad", "2008", "tmdb", "1396", "jellyfin"),
            "Breaking Bad (2008) [tmdbid-1396]",
        )

    def test_format_name_jellyfin_series_imdb(self):
        self.assertEqual(
            medianame.format_folder_name("Breaking Bad", "2008", "imdb", "tt0903747", "jellyfin"),
            "Breaking Bad (2008) [imdbid-tt0903747]",
        )

    def test_format_name_unknown_preset_falls_back_to_plex(self):
        """Unknown preset should behave like Plex (defensive default)."""
        self.assertEqual(
            medianame.format_folder_name("Foo", "2020", "imdb", "tt123", "emby"),
            "Foo (2020) {imdb-tt123}",
        )

    # --- Jellyfin preset integration ---

    def test_jellyfin_movie_from_imdb_url(self):
        """Jellyfin + movie + imdb source → [imdbid-ttXXX] folder."""
        medianame.NAMING_PRESET = "jellyfin"
        medianame.MOVIE_ID_SOURCE = "imdb"
        try:
            medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
            mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
            with patch("medianame.get_movie_data", return_value=mock_response):
                medianame.process_list()
            folders = self._get_created_folders()
            self.assertEqual(len(folders), 1)
            self.assertIn("[imdbid-tt0133093]", folders[0])
        finally:
            medianame.NAMING_PRESET = "plex"

    def test_jellyfin_movie_tmdb_source_triggers_find_lookup(self):
        """Jellyfin + movie + tmdb source: IMDb input → /find → tmdb ID in folder."""
        medianame.NAMING_PRESET = "jellyfin"
        medianame.MOVIE_ID_SOURCE = "tmdb"
        try:
            medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
            mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
            find_response = {"movie_results": [{"id": 603, "title": "The Matrix"}]}
            with patch("medianame.get_movie_data", return_value=mock_response):
                with patch("medianame._tmdb_request", return_value=find_response):
                    medianame.process_list()
            folders = self._get_created_folders()
            self.assertEqual(len(folders), 1)
            self.assertIn("[tmdbid-603]", folders[0])
        finally:
            medianame.NAMING_PRESET = "plex"
            medianame.MOVIE_ID_SOURCE = "imdb"

    def test_jellyfin_series_imdb_source_uses_external_ids(self):
        """Jellyfin + series + imdb source: TMDB details → imdb_id via external_ids."""
        self.series_dir = tempfile.mkdtemp()
        medianame.SERIES_PATH = self.series_dir
        medianame.NAMING_PRESET = "jellyfin"
        medianame.SERIES_ID_SOURCE = "imdb"
        try:
            # Details response now includes external_ids (the feature we added)
            details_response = {
                "id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20",
                "number_of_seasons": 5,
                "credits": {"cast": [{"name": "Bryan Cranston"}]},
                "external_ids": {"imdb_id": "tt0903747"},
            }
            with patch("medianame._tmdb_request", return_value=details_response):
                with patch("builtins.input", side_effect=[
                    "https://www.themoviedb.org/tv/1396-breaking-bad", "", ""
                ]):
                    medianame.process_list(prompt_mode=True)
            folders = [f for f in os.listdir(self.series_dir)
                       if os.path.isdir(os.path.join(self.series_dir, f))]
            self.assertEqual(len(folders), 1)
            self.assertIn("[imdbid-tt0903747]", folders[0])
        finally:
            medianame.NAMING_PRESET = "plex"
            medianame.SERIES_ID_SOURCE = "tmdb"
            shutil.rmtree(self.series_dir, ignore_errors=True)

    def test_preset_override_wins_over_config(self):
        """preset_override argument overrides the configured preset for one run."""
        # Configured as Plex, but override as Jellyfin for this run
        medianame.NAMING_PRESET = "plex"
        medianame.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("medianame.get_movie_data", return_value=mock_response):
            medianame.process_list(preset_override="jellyfin")
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("[imdbid-tt0133093]", folders[0])
        self.assertNotIn("{", folders[0])

    def test_jellyfin_series_imdb_missing_skipped(self):
        """Jellyfin + series + imdb source, but no imdb_id from TMDB → skip."""
        self.series_dir = tempfile.mkdtemp()
        medianame.SERIES_PATH = self.series_dir
        medianame.NAMING_PRESET = "jellyfin"
        medianame.SERIES_ID_SOURCE = "imdb"
        try:
            details_response = {
                "id": 1396, "name": "Obscure Show", "first_air_date": "2020-01-01",
                "number_of_seasons": 1,
                "credits": {"cast": []},
                "external_ids": {"imdb_id": None},
            }
            with patch("medianame._tmdb_request", return_value=details_response):
                with patch("builtins.input", side_effect=[
                    "https://www.themoviedb.org/tv/1396-obscure", "", ""
                ]):
                    medianame.process_list(prompt_mode=True)
            folders = [f for f in os.listdir(self.series_dir)
                       if os.path.isdir(os.path.join(self.series_dir, f))]
            self.assertEqual(len(folders), 0)
        finally:
            medianame.NAMING_PRESET = "plex"
            medianame.SERIES_ID_SOURCE = "tmdb"
            shutil.rmtree(self.series_dir, ignore_errors=True)

    # --- get_tmdb_id_from_imdb ---

    def test_get_tmdb_id_from_imdb_movie(self):
        """IMDb ID resolves to TMDB ID for a movie."""
        response = {"movie_results": [{"id": 603, "title": "X"}], "tv_results": []}
        with patch("medianame._tmdb_request", return_value=response):
            result = medianame.get_tmdb_id_from_imdb("tt0133093", "movie")
        self.assertEqual(result, "603")

    def test_get_tmdb_id_from_imdb_tv(self):
        """IMDb ID resolves to TMDB ID for a TV show."""
        response = {"movie_results": [], "tv_results": [{"id": 1396, "name": "Y"}]}
        with patch("medianame._tmdb_request", return_value=response):
            result = medianame.get_tmdb_id_from_imdb("tt0903747", "tv")
        self.assertEqual(result, "1396")

    def test_get_tmdb_id_from_imdb_not_found(self):
        """No results → returns None."""
        with patch("medianame._tmdb_request",
                   return_value={"movie_results": [], "tv_results": []}):
            self.assertIsNone(medianame.get_tmdb_id_from_imdb("tt9999999", "movie"))

    def test_get_tmdb_id_from_imdb_network_error(self):
        """Network error → returns None."""
        with patch("medianame._tmdb_request", side_effect=Exception("down")):
            self.assertIsNone(medianame.get_tmdb_id_from_imdb("tt0133093", "movie"))

    def _create_input_file(self, lines):
        """Helper: create a temporary input file (not in the target directory)."""
        path = os.path.join(self.temp_input_dir, "test_movies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path

    def _get_created_folders(self):
        """Return only directories in the target path (no files)."""
        return [f for f in os.listdir(self.temp_dir)
                if os.path.isdir(os.path.join(self.temp_dir, f))]


class TestScanFeature(unittest.TestCase):
    """Tests for the v1.2.0 scan feature."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp()
        self.movie_target = tempfile.mkdtemp()
        self.series_target = tempfile.mkdtemp()
        self._orig_movie = medianame.MOVIE_PATH
        self._orig_series = medianame.SERIES_PATH
        medianame.MOVIE_PATH = self.movie_target
        medianame.SERIES_PATH = self.series_target
        medianame._movie_cache.clear()
        medianame._tmdb_cache.clear()

    def tearDown(self):
        medianame.MOVIE_PATH = self._orig_movie
        medianame.SERIES_PATH = self._orig_series
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.movie_target, ignore_errors=True)
        shutil.rmtree(self.series_target, ignore_errors=True)

    def _write_file(self, path, size_bytes=0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            if size_bytes:
                f.seek(size_bytes - 1)
                f.write(b"\0")

    # --- parse_release_name -------------------------------------------------

    def test_parse_movie_with_year(self):
        info = medianame.parse_release_name(
            "Goon.2011.2160p.UPSCALED.BluRay.DoVi.HDR10.x265.DTS-HD.MA.5.1-RANSOM.mkv"
        )
        self.assertEqual(info["title"], "Goon")
        self.assertEqual(info["year"], 2011)
        self.assertEqual(info["type"], "movie")

    def test_parse_movie_multi_word_title(self):
        info = medianame.parse_release_name(
            "Beverly.Hills.Cop.1984.Hybrid.2160p.UHD.Blu-ray.Remux.DV.HDR10P.HEVC.FLAC.2.0-CiNEPHiLES.mkv"
        )
        self.assertEqual(info["title"], "Beverly Hills Cop")
        self.assertEqual(info["year"], 1984)
        self.assertEqual(info["type"], "movie")

    def test_parse_tv_season_folder(self):
        info = medianame.parse_release_name(
            "The.Knick.S01.1080p.DTS-HD.MA.5.1.AVC.REMUX-FraMeSToR"
        )
        self.assertEqual(info["title"], "The Knick")
        self.assertEqual(info["season"], 1)
        self.assertEqual(info["type"], "tv")

    def test_parse_tv_webdl(self):
        info = medianame.parse_release_name(
            "Strange.Angel.S01.1080p.AMZN.WEB-DL.DD+5.1.H.264-AJP69"
        )
        self.assertEqual(info["title"], "Strange Angel")
        self.assertEqual(info["season"], 1)
        self.assertEqual(info["type"], "tv")

    # --- _classify_media_file ----------------------------------------------

    def test_classify_video_above_threshold(self):
        f = os.path.join(self.source_dir, "movie.mkv")
        self._write_file(f, medianame.MIN_VIDEO_BYTES)
        self.assertEqual(medianame._classify_media_file(f), "video")

    def test_classify_video_below_threshold(self):
        f = os.path.join(self.source_dir, "small.mkv")
        self._write_file(f, 1024)
        self.assertIsNone(medianame._classify_media_file(f))

    def test_classify_threshold_is_configurable(self):
        """Adjusting MIN_VIDEO_BYTES at runtime changes what qualifies."""
        f = os.path.join(self.source_dir, "medium.mkv")
        self._write_file(f, 10 * 1024 * 1024)  # 10 MB
        original = medianame.MIN_VIDEO_BYTES
        try:
            medianame.MIN_VIDEO_BYTES = 5 * 1024 * 1024  # lower to 5 MB
            self.assertEqual(medianame._classify_media_file(f), "video")
            medianame.MIN_VIDEO_BYTES = 50 * 1024 * 1024  # raise to 50 MB
            self.assertIsNone(medianame._classify_media_file(f))
        finally:
            medianame.MIN_VIDEO_BYTES = original

    def test_classify_subtitle_any_size(self):
        f = os.path.join(self.source_dir, "movie.en.srt")
        self._write_file(f, 100)
        self.assertEqual(medianame._classify_media_file(f), "subtitle")

    def test_classify_sample_ignored(self):
        f = os.path.join(self.source_dir, "sample.mkv")
        self._write_file(f, medianame.MIN_VIDEO_BYTES)
        self.assertIsNone(medianame._classify_media_file(f))

    def test_classify_unknown_extension(self):
        f = os.path.join(self.source_dir, "readme.nfo")
        self._write_file(f, 500)
        self.assertIsNone(medianame._classify_media_file(f))

    # --- _collect_media_files ----------------------------------------------

    def test_collect_from_folder_skips_samples(self):
        folder = os.path.join(self.source_dir, "Movie.2020")
        self._write_file(os.path.join(folder, "Movie.2020.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        self._write_file(os.path.join(folder, "Movie.2020.en.srt"), 50)
        self._write_file(os.path.join(folder, "Sample", "sample.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        self._write_file(os.path.join(folder, "readme.nfo"), 10)
        results = medianame._collect_media_files(folder)
        kinds = sorted(kind for _, kind in results)
        self.assertEqual(kinds, ["subtitle", "video"])

    def test_collect_single_file(self):
        f = os.path.join(self.source_dir, "Movie.2020.mkv")
        self._write_file(f, medianame.MIN_VIDEO_BYTES)
        results = medianame._collect_media_files(f)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "video")

    # --- scan_source --------------------------------------------------------

    def test_scan_source_skips_empty_items(self):
        # Folder with no qualifying media → skipped
        empty = os.path.join(self.source_dir, "Empty.2020")
        os.makedirs(empty)
        self._write_file(os.path.join(empty, "readme.nfo"), 10)
        # Folder with a valid video → kept
        keep = os.path.join(self.source_dir, "Movie.2020")
        self._write_file(os.path.join(keep, "Movie.2020.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        items = medianame.scan_source(self.source_dir)
        names = [i["name"] for i in items]
        self.assertEqual(names, ["Movie.2020"])

    def test_scan_source_nonexistent(self):
        items = medianame.scan_source(os.path.join(self.source_dir, "nope"))
        self.assertEqual(items, [])

    # --- _destination_for ---------------------------------------------------

    def test_destination_for_movie(self):
        entry = {
            "target_path": "/lib/Movies/Goon (2011) {imdb-tt1499666}",
            "media_type": "movie",
            "parsed_season": None,
        }
        dest = medianame._destination_for(entry, "/src/Goon.2011.mkv")
        self.assertEqual(
            dest, "/lib/Movies/Goon (2011) {imdb-tt1499666}/Goon.2011.mkv"
        )

    def test_destination_for_tv_uses_parsed_season(self):
        entry = {
            "target_path": "/lib/TV/The Knick (2014) {tmdb-1259}",
            "media_type": "tv",
            "parsed_season": 1,
        }
        dest = medianame._destination_for(
            entry, "/src/The.Knick.S01E01.mkv"
        )
        self.assertEqual(
            dest,
            "/lib/TV/The Knick (2014) {tmdb-1259}/Season 01/The.Knick.S01E01.mkv",
        )

    def test_destination_for_tv_defaults_to_season_1(self):
        entry = {
            "target_path": "/lib/TV/Show (2020) {tmdb-1}",
            "media_type": "tv",
            "parsed_season": None,
        }
        dest = medianame._destination_for(entry, "/src/Show.mkv")
        self.assertIn("Season 01", dest)

    # --- execute_scan_plan end-to-end --------------------------------------

    def test_execute_plan_moves_files(self):
        src = os.path.join(self.source_dir, "Movie.2020.mkv")
        self._write_file(src, medianame.MIN_VIDEO_BYTES)
        target = os.path.join(self.movie_target, "Movie (2020) {imdb-tt1}")
        plan = [{
            "source": src,
            "source_name": "Movie.2020.mkv",
            "target_path": target,
            "folder_name": "Movie (2020) {imdb-tt1}",
            "media_type": "movie",
            "seasons": None,
            "parsed_season": None,
            "media_files": [(src, "video")],
        }]
        counts = medianame.execute_scan_plan(plan, operation="move")
        self.assertEqual(counts["moved"], 1)
        self.assertTrue(os.path.exists(
            os.path.join(target, "Movie.2020.mkv")
        ))
        self.assertFalse(os.path.exists(src))

    def test_execute_plan_removes_source_folder_on_move(self):
        """After a successful move, the original source folder is deleted
        (including leftover .nfo, samples, etc.)."""
        folder = os.path.join(self.source_dir, "Movie.2020.BluRay-GROUP")
        video = os.path.join(folder, "Movie.2020.mkv")
        self._write_file(video, medianame.MIN_VIDEO_BYTES)
        # Leftovers that won't be in media_files
        self._write_file(os.path.join(folder, "Movie.2020.nfo"), 500)
        self._write_file(os.path.join(folder, "screenshot.jpg"), 500)
        target = os.path.join(self.movie_target, "Movie (2020) {imdb-tt1}")
        plan = [{
            "source": folder,
            "source_name": "Movie.2020.BluRay-GROUP",
            "target_path": target,
            "folder_name": "Movie (2020) {imdb-tt1}",
            "media_type": "movie",
            "seasons": None,
            "parsed_season": None,
            "media_files": [(video, "video")],
        }]
        counts = medianame.execute_scan_plan(plan, operation="move")
        self.assertEqual(counts["moved"], 1)
        self.assertEqual(counts["cleaned"], 1)
        # Source folder (and all leftovers inside) is gone
        self.assertFalse(os.path.exists(folder))
        # Video made it to the target
        self.assertTrue(os.path.exists(
            os.path.join(target, "Movie.2020.mkv")
        ))

    def test_execute_plan_does_not_delete_source_on_copy(self):
        folder = os.path.join(self.source_dir, "Movie.2020")
        video = os.path.join(folder, "Movie.2020.mkv")
        self._write_file(video, 1024)
        self._write_file(os.path.join(folder, "Movie.2020.nfo"), 50)
        target = os.path.join(self.movie_target, "Movie (2020) {imdb-tt1}")
        plan = [{
            "source": folder,
            "source_name": "Movie.2020",
            "target_path": target,
            "folder_name": "Movie (2020) {imdb-tt1}",
            "media_type": "movie",
            "seasons": None,
            "parsed_season": None,
            "media_files": [(video, "video")],
        }]
        counts = medianame.execute_scan_plan(plan, operation="copy")
        self.assertEqual(counts["copied"], 1)
        self.assertEqual(counts.get("cleaned", 0), 0)
        # Source folder preserved in full
        self.assertTrue(os.path.isdir(folder))
        self.assertTrue(os.path.exists(
            os.path.join(folder, "Movie.2020.nfo")
        ))

    def test_execute_plan_keeps_source_if_move_incomplete(self):
        """If any media file is skipped due to conflict, keep the source."""
        folder = os.path.join(self.source_dir, "Movie.2020")
        video = os.path.join(folder, "Movie.2020.mkv")
        with open(self._ensure_parent(video), "wb") as f:
            f.write(b"new")
        target = os.path.join(self.movie_target, "Movie (2020) {imdb-tt1}")
        os.makedirs(target)
        existing = os.path.join(target, "Movie.2020.mkv")
        with open(existing, "wb") as f:
            f.write(b"old")
        plan = [{
            "source": folder,
            "source_name": "Movie.2020",
            "target_path": target,
            "folder_name": "Movie (2020) {imdb-tt1}",
            "media_type": "movie",
            "seasons": None,
            "parsed_season": None,
            "media_files": [(video, "video")],
        }]
        with patch("builtins.input", return_value="s"):  # skip conflict
            counts = medianame.execute_scan_plan(plan, operation="move")
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts.get("cleaned", 0), 0)
        self.assertTrue(os.path.isdir(folder))  # preserved

    def _ensure_parent(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def test_execute_plan_copies_files(self):
        src = os.path.join(self.source_dir, "Movie.2020.mkv")
        self._write_file(src, 1024)  # small; we're bypassing classification
        target = os.path.join(self.movie_target, "Movie (2020) {imdb-tt1}")
        plan = [{
            "source_name": "Movie.2020.mkv",
            "target_path": target,
            "folder_name": "Movie (2020) {imdb-tt1}",
            "media_type": "movie",
            "seasons": None,
            "parsed_season": None,
            "media_files": [(src, "video")],
        }]
        counts = medianame.execute_scan_plan(plan, operation="copy")
        self.assertEqual(counts["copied"], 1)
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(
            os.path.join(target, "Movie.2020.mkv")
        ))

    def test_execute_plan_creates_season_folder_for_tv(self):
        src = os.path.join(self.source_dir, "Show.S01E01.mkv")
        self._write_file(src, 1024)
        target = os.path.join(self.series_target, "Show (2020) {tmdb-1}")
        plan = [{
            "source_name": "Show.S01",
            "target_path": target,
            "folder_name": "Show (2020) {tmdb-1}",
            "media_type": "tv",
            "seasons": 1,
            "parsed_season": 1,
            "media_files": [(src, "video")],
        }]
        medianame.execute_scan_plan(plan, operation="move")
        self.assertTrue(os.path.isdir(os.path.join(target, "Season 01")))
        self.assertTrue(os.path.exists(
            os.path.join(target, "Season 01", "Show.S01E01.mkv")
        ))

    def test_execute_plan_conflict_skip(self):
        src = os.path.join(self.source_dir, "Movie.mkv")
        self._write_file(src, 1024)
        target = os.path.join(self.movie_target, "Movie (2020) {imdb-tt1}")
        os.makedirs(target)
        # Pre-create destination so it conflicts
        existing = os.path.join(target, "Movie.mkv")
        with open(existing, "wb") as f:
            f.write(b"existing")
        plan = [{
            "source_name": "Movie.mkv",
            "target_path": target,
            "folder_name": "Movie (2020) {imdb-tt1}",
            "media_type": "movie",
            "seasons": None,
            "parsed_season": None,
            "media_files": [(src, "video")],
        }]
        with patch("builtins.input", return_value="s"):
            counts = medianame.execute_scan_plan(plan, operation="move")
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["moved"], 0)
        # Source is still there (skipped)
        self.assertTrue(os.path.exists(src))
        # Existing untouched
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"existing")

    # --- skip filters -------------------------------------------------------

    def test_scan_skips_ignored_names(self):
        # Two entries: one ignored, one kept
        ignored = os.path.join(self.source_dir, "#recycle")
        self._write_file(os.path.join(ignored, "junk.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        keep = os.path.join(self.source_dir, "Movie.2020")
        self._write_file(os.path.join(keep, "Movie.2020.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        # #recycle is in DEFAULT_SCAN_IGNORE
        items = medianame.scan_source(self.source_dir)
        self.assertEqual([i["name"] for i in items], ["Movie.2020"])

    def test_scan_skips_library_folders(self):
        """Folders already tagged with {imdb-...} / [tmdbid-...] are skipped."""
        done = os.path.join(self.source_dir, "Inception (2010) {imdb-tt1375666}")
        self._write_file(os.path.join(done, "movie.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        done_jelly = os.path.join(self.source_dir,
                                   "Tenet (2020) [imdbid-tt6723592]")
        self._write_file(os.path.join(done_jelly, "movie.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        new = os.path.join(self.source_dir, "Raw.2024")
        self._write_file(os.path.join(new, "Raw.2024.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        items = medianame.scan_source(self.source_dir)
        self.assertEqual([i["name"] for i in items], ["Raw.2024"])

    def test_is_library_folder_regex(self):
        self.assertTrue(medianame._is_library_folder("Inception (2010) {imdb-tt1}"))
        self.assertTrue(medianame._is_library_folder("Show (2020) {tmdb-1}"))
        self.assertTrue(medianame._is_library_folder("Show (2020) [tmdbid-1]"))
        self.assertTrue(medianame._is_library_folder("A (2020) [imdbid-tt9]"))
        self.assertFalse(medianame._is_library_folder("Movie.2011.BluRay"))
        self.assertFalse(medianame._is_library_folder("Downloads"))

    def test_scan_applies_max_age_days(self):
        recent = os.path.join(self.source_dir, "Recent.2024")
        self._write_file(os.path.join(recent, "Recent.2024.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        old = os.path.join(self.source_dir, "Old.2015")
        self._write_file(os.path.join(old, "Old.2015.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        # Age `old` to 30 days ago
        old_time = time.time() - 30 * 86400
        os.utime(old, (old_time, old_time))
        items = medianame.scan_source(self.source_dir, max_age_days=7)
        self.assertEqual([i["name"] for i in items], ["Recent.2024"])

    def test_collect_respects_depth_limit(self):
        """At depth > max_depth, subdirectories are no longer walked."""
        root = os.path.join(self.source_dir, "TopLevel")
        # Videos at depth 0 and 1 are in-range for SCAN_MAX_DEPTH=2
        self._write_file(os.path.join(root, "shallow.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        self._write_file(os.path.join(root, "sub", "mid.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        # Video nested too deep (depth 3+) is skipped
        self._write_file(os.path.join(root, "a", "b", "c", "deep.mkv"),
                         medianame.MIN_VIDEO_BYTES)
        results = medianame._collect_media_files(root, max_depth=2)
        names = sorted(os.path.basename(p) for p, _ in results)
        self.assertIn("shallow.mkv", names)
        self.assertIn("mid.mkv", names)
        self.assertNotIn("deep.mkv", names)

    # --- search_by_title year hint -----------------------------------------

    def test_search_by_title_year_hint_reranks(self):
        """When year_hint is passed, results matching the year come first."""
        fake = {
            "results": [
                {"id": 2005, "media_type": "movie", "title": "Recycle",
                 "release_date": "2005-01-01"},
                {"id": 1993, "media_type": "movie", "title": "Re-Cycle",
                 "release_date": "1993-01-01"},
                {"id": 2019, "media_type": "movie", "title": "Re-Cycle",
                 "release_date": "2019-01-01"},
            ]
        }
        with patch("medianame._tmdb_request", return_value=fake), \
             patch("medianame.get_tmdb_details",
                   return_value={"Response": "True", "Title": "Re-Cycle",
                                 "Year": "1993", "Actors": "X",
                                 "imdbID": "tt0133093"}), \
             patch("builtins.input", return_value=""):
            result = medianame.search_by_title("recycle", year_hint=1993)
        # Best match should be the 1993 one → we confirm and get its IMDb ID
        self.assertEqual(result, ("tt0133093", "movie", None))

    def test_execute_plan_conflict_overwrite(self):
        src = os.path.join(self.source_dir, "Movie.mkv")
        with open(src, "wb") as f:
            f.write(b"new-content")
        target = os.path.join(self.movie_target, "Movie (2020) {imdb-tt1}")
        os.makedirs(target)
        existing = os.path.join(target, "Movie.mkv")
        with open(existing, "wb") as f:
            f.write(b"old")
        plan = [{
            "source_name": "Movie.mkv",
            "target_path": target,
            "folder_name": "Movie (2020) {imdb-tt1}",
            "media_type": "movie",
            "seasons": None,
            "parsed_season": None,
            "media_files": [(src, "video")],
        }]
        with patch("builtins.input", return_value="o"):
            counts = medianame.execute_scan_plan(plan, operation="move")
        self.assertEqual(counts["moved"], 1)
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"new-content")


if __name__ == "__main__":
    unittest.main(verbosity=2)
