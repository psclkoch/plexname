"""
Tests for movie_fix.py — Plex folder creation from IMDb links / TMDB.
"""
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import movie_fix


class TestMovieFix(unittest.TestCase):
    """Test scenarios for movie_fix.py"""

    def setUp(self):
        """Create temporary directories for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.temp_input_dir = tempfile.mkdtemp()
        self.original_movie_path = movie_fix.MOVIE_PATH
        self.original_input_file = movie_fix.INPUT_FILE
        movie_fix.MOVIE_PATH = self.temp_dir
        # Clear module caches so state doesn't leak between tests
        movie_fix._movie_cache.clear()
        movie_fix._tmdb_cache.clear()
        # Safety net: prevent real network calls. Individual tests override
        # this via `with patch("movie_fix._tmdb_request", ...)` as needed.
        self._tmdb_patcher = patch("movie_fix._tmdb_request", return_value={"results": []})
        self._tmdb_patcher.start()

    def tearDown(self):
        """Clean up."""
        self._tmdb_patcher.stop()
        movie_fix.MOVIE_PATH = self.original_movie_path
        movie_fix.INPUT_FILE = self.original_input_file
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
        movie_fix.INPUT_FILE = self._create_input_file([
            "https://www.google.com",
            "no-tt-number",
            "  ",
        ])
        with patch('movie_fix.get_movie_data', return_value=None):
            with patch('builtins.input', return_value=""):
                movie_fix.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_valid_movie_creates_folder(self):
        """Valid movie creates a Plex-format folder."""
        movie_fix.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/"
        ])
        mock_response = {
            "Response": "True",
            "Title": "The Matrix",
            "Year": "1999",
        }
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])
        self.assertIn("(1999)", folders[0])
        self.assertIn("{imdb-tt0133093}", folders[0])

    def test_special_characters_removed(self):
        """Special characters are removed from folder names."""
        movie_fix.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/"
        ])
        mock_response = {
            "Response": "True",
            "Title": "Star Wars: Episode IV - A New Hope",
            "Year": "1977",
        }
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertNotIn(":", folders[0])
        self.assertNotIn("/", folders[0])
        self.assertNotIn("\\", folders[0])

    def test_duplicate_not_recreated(self):
        """Already existing folder is not recreated."""
        movie_fix.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/",
            "https://www.imdb.com/title/tt0133093/",  # duplicate
        ])
        mock_response = {
            "Response": "True",
            "Title": "The Matrix",
            "Year": "1999",
        }
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_api_error_handled(self):
        """API errors are handled gracefully."""
        movie_fix.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt9999999/"
        ])
        mock_response = {"Response": "False", "Error": "Incorrect IMDb ID"}
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()

        self.assertEqual(len(self._get_created_folders()), 0)

    def test_path_not_found_aborts(self):
        """Aborts when target path does not exist."""
        movie_fix.MOVIE_PATH = "/nonexistent/path/xyz123"
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        with patch('movie_fix.get_movie_data') as mock_api:
            movie_fix.process_list()
            mock_api.assert_not_called()

    def test_empty_input_file(self):
        """Empty file → prompt mode; empty input → no API call."""
        movie_fix.INPUT_FILE = self._create_input_file([])
        with patch('movie_fix.get_movie_data') as mock_api:
            with patch('builtins.input', return_value=""):
                movie_fix.process_list()
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_deduplication_single_api_call(self):
        """Duplicate entries in input file → only 1 API call per tt-ID."""
        movie_fix.INPUT_FILE = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/",
            "tt0133093",
            "https://imdb.com/title/tt0133093/reviews",
        ])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response) as mock_api:
            movie_fix.process_list()
        self.assertEqual(mock_api.call_count, 1)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_dry_run_creates_nothing(self):
        """Dry run creates no folders."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list(dry_run=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_year_n_a_handling(self):
        """Year 'N/A' from OMDb is handled (no / in path)."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "N/A",
        }
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("NA", folders[0])
        self.assertNotIn("/", folders[0])

    def test_interactive_confirm_creates_folders(self):
        """Interactive mode with 'j' → folders are created."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="j"):
                movie_fix.process_list(interactive=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_interactive_decline_creates_nothing(self):
        """Interactive mode with 'n' → no folders created."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="n"):
                movie_fix.process_list(interactive=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_interactive_all_exist_no_prompt(self):
        """Interactive mode, all folders exist → input() is not called."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        os.makedirs(os.path.join(self.temp_dir, "The Matrix (1999) {imdb-tt0133093}"))
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input") as mock_input:
                movie_fix.process_list(interactive=True)
                mock_input.assert_not_called()

    def test_interactive_path_missing_on_confirm_aborts(self):
        """Interactive mode, target path missing on confirm → no folder created."""
        movie_fix.MOVIE_PATH = "/nonexistent/path/xyz789"
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="j"):
                movie_fix.process_list(interactive=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_year_range_extraction(self):
        """Year range '1999–2000' is reduced to the first year."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "1999–2000",  # en-dash
        }
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("(1999)", folders[0])
        self.assertNotIn("2000", folders[0])

    def test_year_range_ascii_hyphen(self):
        """Year range '1999-2000' (ASCII hyphen) is reduced to the first year."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "1999-2000",
        }
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("(1999)", folders[0])

    def test_utf8_umlauts_in_title(self):
        """Umlauts in movie title are preserved correctly."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "München",
            "Year": "2005",
        }
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("München", folders[0])
        self.assertIn("(2005)", folders[0])

    def test_tt_id_in_middle_of_line(self):
        """tt-number in the middle of a line is recognized."""
        movie_fix.INPUT_FILE = self._create_input_file([
            "See tt0133093 for details",
        ])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("tt0133093", folders[0])

    def test_multiple_different_movies(self):
        """Multiple movies → multiple folders."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093", "tt0167260"])
        def mock_get_movie(imdb_id):
            if imdb_id == "tt0133093":
                return {"Response": "True", "Title": "The Matrix", "Year": "1999"}
            return {"Response": "True", "Title": "The Lord of the Rings", "Year": "2003"}
        with patch("movie_fix.get_movie_data", side_effect=mock_get_movie):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 2)
        folder_names = " ".join(folders)
        self.assertIn("The Matrix", folder_names)
        self.assertIn("The Lord of the Rings", folder_names)

    def test_get_movie_data_returns_none(self):
        """API error (None) → no folder, no crash."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        with patch("movie_fix.get_movie_data", return_value=None):
            movie_fix.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_interactive_accepts_ja_as_confirmation(self):
        """Interactive mode accepts 'ja' as confirmation."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="ja"):
                movie_fix.process_list(interactive=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_prompt_mode_creates_folder_from_input(self):
        """Prompt mode: entered link creates a folder, movies.txt is not used."""
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                movie_fix.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_prompt_mode_empty_input_creates_nothing(self):
        """Prompt mode with immediate empty input → no processing."""
        with patch("movie_fix.get_movie_data") as mock_api:
            with patch("builtins.input", return_value=""):
                movie_fix.process_list(prompt_mode=True)
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_prompt_mode_multiple_links(self):
        """Prompt mode with multiple links → multiple folders."""
        def mock_get_movie(imdb_id):
            if imdb_id == "tt0133093":
                return {"Response": "True", "Title": "The Matrix", "Year": "1999"}
            return {"Response": "True", "Title": "Inception", "Year": "2010"}
        with patch("movie_fix.get_movie_data", side_effect=mock_get_movie):
            with patch("builtins.input", side_effect=["tt0133093", "tt1375666", ""]):
                movie_fix.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 2)
        self.assertIn("The Matrix", " ".join(folders))
        self.assertIn("Inception", " ".join(folders))

    def test_prompt_mode_invalid_input_then_valid(self):
        """Prompt mode: invalid input is skipped, valid input is processed."""
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["invalid", "tt0133093", ""]):
                movie_fix.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_remove_processed_links_creates_backup(self):
        """After processing, links are removed from file and a backup is created."""
        input_path = self._create_input_file(["tt0133093"])
        movie_fix.INPUT_FILE = input_path
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list()
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
        movie_fix.INPUT_FILE = input_path
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                movie_fix.process_list(prompt_mode=True)
        with open(input_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("tt0133093", content)
        self.assertFalse(os.path.exists(input_path + ".bak"))

    def test_custom_output_path(self):
        """-o overrides target path."""
        custom_dir = os.path.join(self.temp_dir, "custom_movies")
        os.makedirs(custom_dir)
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list(output_path=custom_dir)
        folders = [f for f in os.listdir(custom_dir) if os.path.isdir(os.path.join(custom_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_custom_input_file(self):
        """-f overrides input file."""
        other_input = os.path.join(self.temp_input_dir, "other.txt")
        with open(other_input, "w", encoding="utf-8") as f:
            f.write("tt0133093\n")
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list(input_file=other_input)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_file_not_found(self):
        """Missing input file → error message, no processing."""
        movie_fix.INPUT_FILE = "/nonexistent/file_xyz.txt"
        with patch("movie_fix.get_movie_data") as mock_api:
            movie_fix.process_list()
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_empty_file_fallback_prompt_with_link(self):
        """Empty file → prompt mode → entered link is processed."""
        movie_fix.INPUT_FILE = self._create_input_file([])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    # --- TV show tests (TMDB) ---

    def test_series_prompt_creates_tmdb_folder_with_seasons(self):
        """TV show via title search creates folder with tmdb tag and Season subfolders."""
        self.series_dir = tempfile.mkdtemp()
        movie_fix.SERIES_PATH = self.series_dir
        search_response = {"results": [
            {"id": 1396, "media_type": "tv", "name": "Breaking Bad", "first_air_date": "2008-01-20"},
        ]}
        details_response = {
            "id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20",
            "number_of_seasons": 5,
            "credits": {"cast": [{"name": "Bryan Cranston"}, {"name": "Aaron Paul"}]},
        }
        with patch("movie_fix._tmdb_request", side_effect=[search_response, details_response]):
            with patch("builtins.input", side_effect=["breaking bad", "", "", ""]):
                movie_fix.process_list(prompt_mode=True)
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
        with patch("movie_fix._tmdb_request", side_effect=[search_response, details_response]):
            with patch("movie_fix.get_movie_data", return_value={
                "Response": "True", "Title": "Inception", "Year": "2010",
            }):
                with patch("builtins.input", side_effect=["inception", "", ""]):
                    movie_fix.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("Inception", folders[0])
        self.assertIn("{imdb-tt1375666}", folders[0])

    def test_tmdb_url_recognized_as_series(self):
        """TMDB URL is recognized as a TV show."""
        self.series_dir = tempfile.mkdtemp()
        movie_fix.SERIES_PATH = self.series_dir
        mock_details = {
            "Response": "True", "Title": "Breaking Bad", "Year": "2008",
            "Actors": "Bryan Cranston", "Seasons": 5,
        }
        with patch("movie_fix.get_tmdb_details", return_value=mock_details):
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/1396-breaking-bad", "", ""
            ]):
                movie_fix.process_list(prompt_mode=True)
        folders = [f for f in os.listdir(self.series_dir)
                   if os.path.isdir(os.path.join(self.series_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("{tmdb-1396}", folders[0])
        shutil.rmtree(self.series_dir, ignore_errors=True)

    def test_series_different_target_path(self):
        """TV shows go to SERIES_PATH, not MOVIE_PATH."""
        self.series_dir = tempfile.mkdtemp()
        movie_fix.SERIES_PATH = self.series_dir
        mock_details = {
            "Response": "True", "Title": "Breaking Bad", "Year": "2008",
            "Actors": "Bryan Cranston", "Seasons": 1,
        }
        with patch("movie_fix.get_tmdb_details", return_value=mock_details):
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/1396-breaking-bad", "", ""
            ]):
                movie_fix.process_list(prompt_mode=True)
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
            result = movie_fix._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 5)

    def test_prompt_seasons_numeric_override(self):
        """User enters a number → that number is used."""
        with patch("builtins.input", return_value="3"):
            result = movie_fix._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 3)

    def test_prompt_seasons_zero_clamped_to_one(self):
        """User enters 0 → clamped to 1."""
        with patch("builtins.input", return_value="0"):
            result = movie_fix._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 1)

    def test_prompt_seasons_negative_clamped_to_one(self):
        """User enters negative → clamped to 1."""
        with patch("builtins.input", return_value="-3"):
            result = movie_fix._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 1)

    def test_prompt_seasons_non_numeric_with_known(self):
        """Non-numeric input with known count → returns known count."""
        with patch("builtins.input", return_value="five"):
            result = movie_fix._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 5)

    def test_prompt_seasons_non_numeric_without_known(self):
        """Non-numeric input without known count → returns 1."""
        with patch("builtins.input", return_value="abc"):
            result = movie_fix._prompt_seasons(known_seasons=None)
        self.assertEqual(result, 1)

    def test_prompt_seasons_eof_returns_default(self):
        """EOFError during input → returns known count."""
        with patch("builtins.input", side_effect=EOFError()):
            result = movie_fix._prompt_seasons(known_seasons=5)
        self.assertEqual(result, 5)

    def test_prompt_seasons_eof_no_known(self):
        """EOFError with no known count → returns 1."""
        with patch("builtins.input", side_effect=EOFError()):
            result = movie_fix._prompt_seasons(known_seasons=None)
        self.assertEqual(result, 1)

    # --- get_movie_data retry and cache ---

    def test_get_movie_data_uses_cache(self):
        """Second call with same id returns cached result without HTTP."""
        movie_fix._movie_cache["tt1234567"] = {"Response": "True", "Title": "Cached"}
        with patch("movie_fix.requests.get") as mock_get:
            result = movie_fix.get_movie_data("tt1234567")
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

        with patch("movie_fix.requests.get", side_effect=flaky_get):
            with patch("movie_fix.time.sleep"):
                result = movie_fix.get_movie_data("tt9999001")
        self.assertEqual(call_count["n"], 3)
        self.assertIsNotNone(result)
        self.assertEqual(result["Title"], "OK")

    def test_get_movie_data_all_retries_fail(self):
        """All retries fail → returns None."""
        with patch("movie_fix.requests.get", side_effect=Exception("down")):
            with patch("movie_fix.time.sleep"):
                result = movie_fix.get_movie_data("tt9999002")
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
        with patch("movie_fix._tmdb_request", return_value=search_response):
            with patch("movie_fix.get_tmdb_details",
                       side_effect=[details_first, details_second]):
                with patch("builtins.input", side_effect=["n", "2"]):
                    result = movie_fix.search_by_title("foo")
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
        with patch("movie_fix._tmdb_request", return_value=search_response):
            with patch("movie_fix.get_tmdb_details",
                       side_effect=[details_first, details_tv]):
                with patch("builtins.input", side_effect=["n", "2", ""]):
                    result = movie_fix.search_by_title("foo")
        self.assertEqual(result, ("1396", "tv", 5))

    def test_search_stage2_invalid_number(self):
        """User picks out-of-range number → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Only", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Only", "Year": "2000",
                   "Actors": "X", "imdbID": "tt0000001"}
        with patch("movie_fix._tmdb_request", return_value=search_response):
            with patch("movie_fix.get_tmdb_details", return_value=details):
                with patch("builtins.input", side_effect=["n", "99"]):
                    result = movie_fix.search_by_title("foo")
        self.assertIsNone(result)

    def test_search_stage2_non_numeric_input(self):
        """User enters non-numeric choice in Stage 2 → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Only", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Only", "Year": "2000",
                   "Actors": "X", "imdbID": "tt0000001"}
        with patch("movie_fix._tmdb_request", return_value=search_response):
            with patch("movie_fix.get_tmdb_details", return_value=details):
                with patch("builtins.input", side_effect=["n", "abc"]):
                    result = movie_fix.search_by_title("foo")
        self.assertIsNone(result)

    def test_search_stage2_empty_skip(self):
        """User enters empty choice in Stage 2 → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Only", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Only", "Year": "2000",
                   "Actors": "X", "imdbID": "tt0000001"}
        with patch("movie_fix._tmdb_request", return_value=search_response):
            with patch("movie_fix.get_tmdb_details", return_value=details):
                with patch("builtins.input", side_effect=["n", ""]):
                    result = movie_fix.search_by_title("foo")
        self.assertIsNone(result)

    def test_search_no_results_returns_none(self):
        """TMDB returns empty result list → search returns None."""
        with patch("movie_fix._tmdb_request", return_value={"results": []}):
            result = movie_fix.search_by_title("nonexistent-xyz")
        self.assertIsNone(result)

    def test_search_movie_without_imdb_id(self):
        """Best-match movie without IMDb ID → returns None."""
        search_response = {"results": [
            {"id": 1, "media_type": "movie", "title": "Obscure", "release_date": "2000-01-01"},
        ]}
        details = {"Response": "True", "Title": "Obscure", "Year": "2000",
                   "Actors": "X", "imdbID": ""}
        with patch("movie_fix._tmdb_request", return_value=search_response):
            with patch("movie_fix.get_tmdb_details", return_value=details):
                with patch("builtins.input", return_value=""):
                    result = movie_fix.search_by_title("obscure")
        self.assertIsNone(result)

    def test_search_network_error(self):
        """Network error during search → returns None without crash."""
        with patch("movie_fix._tmdb_request", side_effect=Exception("net down")):
            result = movie_fix.search_by_title("anything")
        self.assertIsNone(result)

    # --- get_tmdb_details error paths ---

    def test_get_tmdb_details_network_error(self):
        """Network error during details fetch → returns None."""
        with patch("movie_fix._tmdb_request", side_effect=Exception("timeout")):
            result = movie_fix.get_tmdb_details("1396", "tv")
        self.assertIsNone(result)

    def test_get_tmdb_details_invalid_response(self):
        """TMDB returns response without 'id' → returns None."""
        with patch("movie_fix._tmdb_request",
                   return_value={"status_code": 34, "status_message": "Not found"}):
            result = movie_fix.get_tmdb_details("99999", "tv")
        self.assertIsNone(result)

    def test_tmdb_cache_avoids_duplicate_calls(self):
        """Second fetch of the same TMDB id uses the cache."""
        details_response = {
            "id": 1396, "name": "BB", "first_air_date": "2008-01-01",
            "number_of_seasons": 5, "credits": {"cast": []},
        }
        with patch("movie_fix._tmdb_request", return_value=details_response) as mock_req:
            movie_fix.get_tmdb_details("1396", "tv")
            movie_fix.get_tmdb_details("1396", "tv")
        self.assertEqual(mock_req.call_count, 1)

    # --- Interactive mode variants ---

    def test_interactive_cancel_on_confirm(self):
        """Interactive mode: user answers 'n' on final confirm → no folders."""
        movie_fix.INPUT_FILE = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="n"):
                movie_fix.process_list(interactive=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_dry_run_series_creates_nothing(self):
        """Dry run for TV show: prints preview, creates no folders."""
        self.series_dir = tempfile.mkdtemp()
        movie_fix.SERIES_PATH = self.series_dir
        search_response = {"results": [
            {"id": 1396, "media_type": "tv", "name": "Breaking Bad",
             "first_air_date": "2008-01-20"},
        ]}
        details_response = {
            "id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20",
            "number_of_seasons": 5,
            "credits": {"cast": [{"name": "Bryan Cranston"}]},
        }
        with patch("movie_fix._tmdb_request",
                   side_effect=[search_response, details_response]):
            with patch("builtins.input", side_effect=["breaking bad", "", ""]):
                movie_fix.process_list(dry_run=True, prompt_mode=True)
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
        movie_fix.INPUT_FILE = input_path

        def mock_api(imdb_id):
            return {"Response": "True", "Title": f"Movie {imdb_id}", "Year": "2000"}

        with patch("movie_fix.get_movie_data", side_effect=mock_api):
            movie_fix.process_list()
        with open(input_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("# My movies to process", content)
        self.assertIn("# Inception next", content)
        self.assertNotIn("tt0133093", content)
        self.assertNotIn("tt1375666", content)

    def test_comment_only_file_falls_through_to_prompt(self):
        """File with only comments → falls through to prompt mode."""
        movie_fix.INPUT_FILE = self._create_input_file([
            "# just comments",
            "# no links here",
        ])
        with patch("builtins.input", return_value=""):
            movie_fix.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    # --- Seasons with double-digit formatting ---

    def test_twelve_seasons_formatting(self):
        """Shows with 10+ seasons get correctly formatted Season 10, 11, 12."""
        self.series_dir = tempfile.mkdtemp()
        movie_fix.SERIES_PATH = self.series_dir
        details_response = {
            "id": 999, "name": "Long Show", "first_air_date": "1990-01-01",
            "number_of_seasons": 12,
            "credits": {"cast": [{"name": "X"}]},
        }
        with patch("movie_fix._tmdb_request", return_value=details_response):
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/999-long-show", "", ""
            ]):
                movie_fix.process_list(prompt_mode=True)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
