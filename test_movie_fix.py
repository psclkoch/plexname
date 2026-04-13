"""
Tests für movie_fix.py - Plex-Ordner-Erstellung aus IMDb-Links
"""
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

# Import der zu testenden Funktionen
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import movie_fix


class TestMovieFix(unittest.TestCase):
    """Test-Szenarien für movie_fix.py"""

    def setUp(self):
        """Temporäres Verzeichnis für jeden Test"""
        self.temp_dir = tempfile.mkdtemp()
        self.temp_input_dir = tempfile.mkdtemp()  # Separates Verzeichnis für Input-Datei
        self.original_ziel = movie_fix.ZIEL_PFAD
        self.original_input = movie_fix.INPUT_DATEI
        movie_fix.ZIEL_PFAD = self.temp_dir

    def tearDown(self):
        """Aufräumen"""
        movie_fix.ZIEL_PFAD = self.original_ziel
        movie_fix.INPUT_DATEI = self.original_input
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        shutil.rmtree(self.temp_input_dir, ignore_errors=True)

    def test_imdb_id_extraction(self):
        """Test: tt-Nummer wird aus verschiedenen URL-Formaten extrahiert"""
        test_cases = [
            ("https://www.imdb.com/title/tt0133093/", "tt0133093"),
            ("https://imdb.com/title/tt0133093", "tt0133093"),
            ("https://www.imdb.com/title/tt0133093/reviews", "tt0133093"),
            ("tt0133093", "tt0133093"),
            ("  tt0133093  ", "tt0133093"),
        ]
        for url, expected in test_cases:
            match = re.search(r'tt\d+', url.strip())
            self.assertIsNotNone(match, f"Kein Match für: {url}")
            self.assertEqual(match.group(), expected)

    def test_invalid_url_skipped(self):
        """Test: Ungültige URLs werden übersprungen → Eingabe-Modus mit leerer Eingabe"""
        movie_fix.INPUT_DATEI = self._create_input_file([
            "https://www.google.com",
            "keine-tt-nummer",
            "  ",
        ])
        with patch('movie_fix.get_movie_data', return_value=None):
            with patch('builtins.input', return_value=""):
                movie_fix.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_valid_movie_creates_folder(self):
        """Test: Gültiger Film erstellt Ordner im Plex-Format"""
        movie_fix.INPUT_DATEI = self._create_input_file([
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
        """Test: Sonderzeichen werden aus Ordnernamen entfernt"""
        movie_fix.INPUT_DATEI = self._create_input_file([
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
        # Doppelpunkt und andere Sonderzeichen dürfen nicht im Pfad sein
        self.assertNotIn(":", folders[0])
        self.assertNotIn("/", folders[0])
        self.assertNotIn("\\", folders[0])

    def test_duplicate_not_recreated(self):
        """Test: Bereits existierender Ordner wird nicht neu erstellt"""
        movie_fix.INPUT_DATEI = self._create_input_file([
            "https://www.imdb.com/title/tt0133093/",
            "https://www.imdb.com/title/tt0133093/",  # Duplikat
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
        """Test: API-Fehler werden abgefangen"""
        movie_fix.INPUT_DATEI = self._create_input_file([
            "https://www.imdb.com/title/tt9999999/"  # Ungültige ID
        ])
        mock_response = {"Response": "False", "Error": "Incorrect IMDb ID"}
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()

        self.assertEqual(len(self._get_created_folders()), 0)

    def test_path_not_found_aborts(self):
        """Test: Abbruch wenn Zielpfad nicht existiert"""
        movie_fix.ZIEL_PFAD = "/nicht/existierender/pfad/xyz123"
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        with patch('movie_fix.get_movie_data') as mock_api:
            movie_fix.process_list()
            mock_api.assert_not_called()

    def test_empty_input_file(self):
        """Test: Leere Datei → Eingabe-Modus; leere Eingabe → kein API-Aufruf"""
        movie_fix.INPUT_DATEI = self._create_input_file([])
        with patch('movie_fix.get_movie_data') as mock_api:
            with patch('builtins.input', return_value=""):
                movie_fix.process_list()
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_deduplication_single_api_call(self):
        """Test: Duplikate in Input-Datei → nur 1 API-Aufruf pro tt-ID"""
        movie_fix.INPUT_DATEI = self._create_input_file([
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
        """Test: Dry-Run erstellt keine Ordner"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list(dry_run=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_year_n_a_handling(self):
        """Test: Jahr 'N/A' von OMDb wird behandelt (kein / im Pfad)"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "N/A",  # OMDb kann N/A zurückgeben - "/" würde Pfad zerstören
        }
        with patch('movie_fix.get_movie_data', return_value=mock_response):
            movie_fix.process_list()

        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("NA", folders[0])  # N/A → NA nach Entfernung von /
        self.assertNotIn("/", folders[0])

    def test_interactive_confirm_creates_folders(self):
        """Test: Interaktiv mit 'j' → Ordner werden angelegt"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="j"):
                movie_fix.process_list(interactive=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_interactive_decline_creates_nothing(self):
        """Test: Interaktiv mit 'n' → keine Ordner angelegt"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="n"):
                movie_fix.process_list(interactive=True)
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_interactive_all_exist_no_prompt(self):
        """Test: Interaktiv, alle Ordner existieren → input() wird nicht aufgerufen"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        # Ordner vorab anlegen
        os.makedirs(os.path.join(self.temp_dir, "The Matrix (1999) {imdb-tt0133093}"))
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input") as mock_input:
                movie_fix.process_list(interactive=True)
                mock_input.assert_not_called()

    def test_interactive_path_missing_on_confirm_aborts(self):
        """Test: Interaktiv, Zielpfad existiert bei Bestätigung nicht → kein Ordner erstellt"""
        movie_fix.ZIEL_PFAD = "/nicht/existierender/pfad/xyz789"
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="j"):
                movie_fix.process_list(interactive=True)
        # temp_dir ist leer, da ZIEL_PFAD überschrieben und dort nichts angelegt wurde
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_year_range_extraction(self):
        """Test: Jahr-Bereich '1999–2000' wird auf erstes Jahr reduziert"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "Test Film",
            "Year": "1999–2000",  # En-Dash
        }
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("(1999)", folders[0])
        self.assertNotIn("2000", folders[0])

    def test_year_range_ascii_hyphen(self):
        """Test: Jahr-Bereich '1999-2000' (ASCII) wird auf erstes Jahr reduziert"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
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
        """Test: Umlaute im Filmtitel werden korrekt übernommen"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {
            "Response": "True",
            "Title": "München",
            "Year": "2005",
        }
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("München", folders[0])
        self.assertIn("(2005)", folders[0])

    def test_tt_id_in_middle_of_line(self):
        """Test: tt-Nummer mitten in Zeile wird erkannt"""
        movie_fix.INPUT_DATEI = self._create_input_file([
            "Siehe tt0133093 für Details",
        ])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("tt0133093", folders[0])

    def test_multiple_different_movies(self):
        """Test: Mehrere Filme → mehrere Ordner"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093", "tt0167260"])
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
        """Test: API-Fehler (None) → kein Ordner, kein Absturz"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        with patch("movie_fix.get_movie_data", return_value=None):
            movie_fix.process_list()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_interactive_accepts_ja_as_confirmation(self):
        """Test: Interaktiv akzeptiert 'ja' als Bestätigung"""
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", return_value="ja"):
                movie_fix.process_list(interactive=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_prompt_mode_creates_folder_from_input(self):
        """Test: -p Modus: eingegebener Link erstellt Ordner, filme.txt wird nicht verwendet"""
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                movie_fix.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_prompt_mode_empty_input_creates_nothing(self):
        """Test: -p Modus mit sofort leerer Eingabe → keine Verarbeitung"""
        with patch("movie_fix.get_movie_data") as mock_api:
            with patch("builtins.input", return_value=""):
                movie_fix.process_list(prompt_mode=True)
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_prompt_mode_multiple_links(self):
        """Test: -p Modus mit mehreren Links → mehrere Ordner"""
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
        """Test: -p Modus: ungültige Eingabe wird übersprungen, gültige verarbeitet"""
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["ungültig", "tt0133093", ""]):
                movie_fix.process_list(prompt_mode=True)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_remove_processed_links_creates_backup(self):
        """Test: Nach Verarbeitung werden Links aus Datei entfernt, Backup erstellt"""
        input_path = self._create_input_file(["tt0133093"])
        movie_fix.INPUT_DATEI = input_path
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
        """Test: Prompt-Modus ändert filme.txt nicht (use_from_file=False)"""
        input_path = self._create_input_file(["tt0133093"])
        movie_fix.INPUT_DATEI = input_path
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                movie_fix.process_list(prompt_mode=True)
        with open(input_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("tt0133093", content)
        self.assertFalse(os.path.exists(input_path + ".bak"))

    def test_custom_output_path(self):
        """Test: -o überschreibt Zielpfad"""
        custom_dir = os.path.join(self.temp_dir, "custom_filme")
        os.makedirs(custom_dir)
        movie_fix.INPUT_DATEI = self._create_input_file(["tt0133093"])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list(ziel_pfad=custom_dir)
        folders = [f for f in os.listdir(custom_dir) if os.path.isdir(os.path.join(custom_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("The Matrix", folders[0])

    def test_custom_input_file(self):
        """Test: -f überschreibt Input-Datei"""
        other_input = os.path.join(self.temp_input_dir, "andere.txt")
        with open(other_input, "w", encoding="utf-8") as f:
            f.write("tt0133093\n")
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            movie_fix.process_list(input_datei=other_input)
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    def test_file_not_found(self):
        """Test: Fehlende Input-Datei → Fehlermeldung, keine Verarbeitung"""
        movie_fix.INPUT_DATEI = "/nicht/existierende/datei_xyz.txt"
        with patch("movie_fix.get_movie_data") as mock_api:
            movie_fix.process_list()
            mock_api.assert_not_called()
        self.assertEqual(len(self._get_created_folders()), 0)

    def test_empty_file_fallback_prompt_with_link(self):
        """Test: Leere Datei → Eingabe-Modus → eingegebener Link wird verarbeitet"""
        movie_fix.INPUT_DATEI = self._create_input_file([])
        mock_response = {"Response": "True", "Title": "The Matrix", "Year": "1999"}
        with patch("movie_fix.get_movie_data", return_value=mock_response):
            with patch("builtins.input", side_effect=["tt0133093", ""]):
                movie_fix.process_list()
        folders = self._get_created_folders()
        self.assertEqual(len(folders), 1)

    # --- Tests für Serien (TMDB) ---

    def test_series_prompt_creates_tmdb_folder_with_seasons(self):
        """Test: Serie via Titelsuche erstellt Ordner mit tmdb-Tag und Season-Unterordnern"""
        self.serien_dir = tempfile.mkdtemp()
        movie_fix.ZIEL_PFAD_SERIEN = self.serien_dir
        search_response = {"results": [
            {"id": 1396, "media_type": "tv", "name": "Breaking Bad", "first_air_date": "2008-01-20"},
        ]}
        details_response = {
            "id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20",
            "number_of_seasons": 5,
            "credits": {"cast": [{"name": "Bryan Cranston"}, {"name": "Aaron Paul"}]},
        }
        # input: title, confirm, seasons (Enter=5), empty to start
        with patch("movie_fix._tmdb_request", side_effect=[search_response, details_response]):
            with patch("builtins.input", side_effect=["breaking bad", "", "", ""]):
                movie_fix.process_list(prompt_mode=True)
        folders = [f for f in os.listdir(self.serien_dir)
                   if os.path.isdir(os.path.join(self.serien_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("Breaking Bad", folders[0])
        self.assertIn("{tmdb-1396}", folders[0])
        self.assertIn("(2008)", folders[0])
        # Season-Unterordner prüfen
        series_path = os.path.join(self.serien_dir, folders[0])
        season_dirs = sorted(os.listdir(series_path))
        self.assertEqual(len(season_dirs), 5)
        self.assertEqual(season_dirs[0], "Season 01")
        self.assertEqual(season_dirs[4], "Season 05")
        shutil.rmtree(self.serien_dir, ignore_errors=True)

    def test_movie_via_tmdb_search_uses_imdb_tag(self):
        """Test: Film via TMDB-Titelsuche erstellt Ordner mit imdb-Tag"""
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
        """Test: TMDB-URL wird als Serie erkannt"""
        self.serien_dir = tempfile.mkdtemp()
        movie_fix.ZIEL_PFAD_SERIEN = self.serien_dir
        mock_details = {
            "Response": "True", "Title": "Breaking Bad", "Year": "2008",
            "Actors": "Bryan Cranston", "Seasons": 5,
        }
        with patch("movie_fix.get_tmdb_details", return_value=mock_details):
            # input: TMDB-URL, seasons (Enter=5), empty to start
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/1396-breaking-bad", "", ""
            ]):
                movie_fix.process_list(prompt_mode=True)
        folders = [f for f in os.listdir(self.serien_dir)
                   if os.path.isdir(os.path.join(self.serien_dir, f))]
        self.assertEqual(len(folders), 1)
        self.assertIn("{tmdb-1396}", folders[0])
        shutil.rmtree(self.serien_dir, ignore_errors=True)

    def test_series_different_target_path(self):
        """Test: Serien landen in ZIEL_PFAD_SERIEN, nicht in ZIEL_PFAD"""
        self.serien_dir = tempfile.mkdtemp()
        movie_fix.ZIEL_PFAD_SERIEN = self.serien_dir
        mock_details = {
            "Response": "True", "Title": "Breaking Bad", "Year": "2008",
            "Actors": "Bryan Cranston", "Seasons": 1,
        }
        with patch("movie_fix.get_tmdb_details", return_value=mock_details):
            # input: TMDB-URL, seasons (Enter=1), empty to start
            with patch("builtins.input", side_effect=[
                "https://www.themoviedb.org/tv/1396-breaking-bad", "", ""
            ]):
                movie_fix.process_list(prompt_mode=True)
        # Film-Ordner muss leer sein
        self.assertEqual(len(self._get_created_folders()), 0)
        # Serien-Ordner muss befüllt sein
        serien_folders = [f for f in os.listdir(self.serien_dir)
                          if os.path.isdir(os.path.join(self.serien_dir, f))]
        self.assertEqual(len(serien_folders), 1)
        shutil.rmtree(self.serien_dir, ignore_errors=True)

    def _create_input_file(self, lines):
        """Hilfsfunktion: Erstellt temporäre Input-Datei (nicht im Zielverzeichnis)"""
        path = os.path.join(self.temp_input_dir, "test_filme.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path

    def _get_created_folders(self):
        """Nur Ordner im Zielverzeichnis (keine Dateien)"""
        return [f for f in os.listdir(self.temp_dir)
                if os.path.isdir(os.path.join(self.temp_dir, f))]


if __name__ == "__main__":
    unittest.main(verbosity=2)
