"""
Konfigurationsverwaltung für plexname.

Speichert API-Keys und Pfade in ~/.config/plexname/config.json.
Beim ersten Start wird der Nutzer interaktiv nach den Werten gefragt.
"""

import json
import os
import stat

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "plexname")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

REQUIRED_KEYS = ["omdb_api_key", "tmdb_token", "movie_path", "series_path"]


def load_config():
    """
    Liest die Konfiguration aus ~/.config/plexname/config.json.

    Returns:
        dict mit allen Konfigurationswerten, oder None falls die Datei
        fehlt oder unvollständig ist.
    """
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    # Prüfen, ob alle Pflichtfelder vorhanden und nicht leer sind
    for key in REQUIRED_KEYS:
        if not cfg.get(key):
            return None
    return cfg


def save_config(cfg):
    """Speichert die Konfiguration und setzt restriktive Dateiberechtigungen."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    # Nur Besitzer darf lesen/schreiben (enthält API-Token)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)


def run_setup():
    """
    Interaktives Setup: fragt den Nutzer nach API-Keys und Pfaden.

    Returns:
        dict mit der vollständigen Konfiguration.
    """
    print("=" * 50)
    print("  🎬 plexname — Ersteinrichtung")
    print("=" * 50)

    # Bestehende Konfiguration als Vorschlag laden
    existing = load_config() or {}

    # 1. OMDb API Key
    print()
    print("1) OMDb API Key (für Film-Lookups via IMDb-ID)")
    print("   Kostenlos erstellen: https://www.omdbapi.com/apikey.aspx")
    print("   → 'FREE' wählen, E-Mail eingeben, Key kommt per Mail.")
    default = existing.get("omdb_api_key", "")
    omdb_key = _prompt_value("   API Key", default)

    # 2. TMDB Token
    print()
    print("2) TMDB Read Access Token (für Titelsuche, Serien, Cast)")
    print("   Account erstellen: https://www.themoviedb.org/signup")
    print("   Token holen: https://www.themoviedb.org/settings/api")
    print("   → Den langen 'API-Token für Lesezugriff' kopieren (nicht den kurzen API-Schlüssel).")
    default = existing.get("tmdb_token", "")
    tmdb_token = _prompt_value("   Token", default)

    # 3. Film-Pfad
    print()
    print("3) Plex-Filmordner (Stammverzeichnis der Filmbibliothek)")
    print("   Beispiel: /Volumes/NAS/Filme oder /mnt/media/movies")
    default = existing.get("movie_path", "")
    movie_path = _prompt_value("   Pfad", default)

    # 4. Serien-Pfad
    print()
    print("4) Plex-Serienordner (Stammverzeichnis der Serienbibliothek)")
    print("   Beispiel: /Volumes/NAS/Serien oder /mnt/media/tv")
    default = existing.get("series_path", "")
    series_path = _prompt_value("   Pfad", default)

    cfg = {
        "omdb_api_key": omdb_key,
        "tmdb_token": tmdb_token,
        "movie_path": movie_path,
        "series_path": series_path,
    }

    save_config(cfg)
    print()
    print(f"✅ Konfiguration gespeichert: {CONFIG_PATH}")
    print("   Erneut einrichten: plexname setup")
    print()
    return cfg


def get_config():
    """
    Lädt die Konfiguration. Startet das Setup, falls noch nicht eingerichtet.

    Returns:
        dict mit allen Konfigurationswerten.
    """
    cfg = load_config()
    if cfg is not None:
        return cfg
    print("Noch nicht eingerichtet. Starte Setup...\n")
    return run_setup()


def _prompt_value(label, default=""):
    """Fragt einen Wert ab, zeigt ggf. den bestehenden Wert als Vorschlag."""
    if default:
        # Kurze Vorschau für lange Tokens
        preview = default if len(default) <= 30 else default[:20] + "..." + default[-7:]
        eingabe = input(f"{label} [{preview}]: ").strip()
        return eingabe if eingabe else default
    else:
        while True:
            eingabe = input(f"{label}: ").strip()
            if eingabe:
                return eingabe
            print("   Eingabe erforderlich.")
