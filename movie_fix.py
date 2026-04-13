"""
Plex-Ordner aus IMDb-Links / TMDB erstellen.

Liest eine Textdatei mit IMDb-URLs (oder tt-IDs), fragt die OMDb-API nach
Titel und Jahr ab und legt für jeden Film einen Plex-konformen Ordner an:
    Filmname (Jahr) {imdb-ttXXXXXXXX}

Serien werden über TMDB gesucht und als Plex-Ordner angelegt:
    Serienname (Jahr) {tmdb-XXXXX}

Verwendung:
    python3 movie_fix.py                    # Ordner anlegen (aus filme.txt)
    python3 movie_fix.py -n                 # Dry-Run (nur anzeigen, nichts ändern)
    python3 movie_fix.py -i                 # Interaktiv: Dry-Run + Bestätigung → Ordner anlegen
    python3 movie_fix.py -o /pfad/zu/filmen # Zielpfad überschreiben (gilt für Filme UND Serien)
    python3 movie_fix.py -f andere.txt      # Andere Input-Datei verwenden
    python3 movie_fix.py -p                 # Nach Filmen/Serien fragen (ohne filme.txt)
    # Wenn filme.txt leer ist, wird automatisch nach Titeln gefragt.
"""

import argparse
import os
import re
import shutil
import time

import requests


# --- Konfiguration (wird beim Start aus ~/.config/plexname/config.json geladen) ---

API_KEY = None
TMDB_TOKEN = None
ZIEL_PFAD = None
ZIEL_PFAD_SERIEN = None
INPUT_DATEI = "filme.txt"


MAX_RETRIES = 3
RETRY_DELAY = 2  # Sekunden zwischen Versuchen

# Cache für bereits abgerufene Daten (vermeidet doppelte API-Calls)
_movie_cache = {}
_tmdb_cache = {}


def get_movie_data(imdb_id):
    """
    Ruft Filmdaten von der OMDb-API ab (mit Retry bei temporären Fehlern).

    Args:
        imdb_id: IMDb-ID (z.B. tt0133093)

    Returns:
        dict mit Title, Year etc. bei Erfolg, sonst None.
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
            last_error = data.get("Error", "Unbekannter API-Fehler")
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES:
                print(f"  ⚠️ Versuch {attempt}/{MAX_RETRIES} fehlgeschlagen, erneuter Versuch in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"Fehler bei {imdb_id} nach {MAX_RETRIES} Versuchen: {last_error}")
                return None
    return None


def _tmdb_request(endpoint, params=None):
    """
    Führt einen authentifizierten TMDB-API-Request aus.

    Args:
        endpoint: API-Pfad (z.B. /search/multi)
        params: Query-Parameter als dict

    Returns:
        JSON-Response als dict.
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
    Ruft Detail-Daten (Titel, Jahr, Cast) von TMDB ab.

    Args:
        tmdb_id: TMDB-ID (z.B. 1396)
        media_type: "tv" oder "movie"

    Returns:
        dict mit Response, Title, Year, Actors (und imdbID bei Filmen).
        None bei Fehler.
    """
    cache_key = f"{media_type}-{tmdb_id}"
    if cache_key in _tmdb_cache:
        return _tmdb_cache[cache_key]

    endpoint = f"/{'tv' if media_type == 'tv' else 'movie'}/{tmdb_id}"
    try:
        data = _tmdb_request(endpoint, {"append_to_response": "credits"})
    except Exception as e:
        print(f"  ❌ TMDB-Fehler: {e}")
        return None

    if "id" not in data:
        return None

    cast = data.get("credits", {}).get("cast", [])
    actors = ", ".join(c["name"] for c in cast[:2]) if cast else "N/A"

    if media_type == "tv":
        result = {
            "Response": "True",
            "Title": data.get("name", ""),
            "Year": (data.get("first_air_date") or "")[:4],
            "Actors": actors,
            "Seasons": data.get("number_of_seasons"),
        }
    else:
        result = {
            "Response": "True",
            "Title": data.get("title", ""),
            "Year": (data.get("release_date") or "")[:4],
            "imdbID": data.get("imdb_id", ""),
            "Actors": actors,
        }
        # Movie-Cache befüllen, damit get_movie_data() den Film findet
        if result.get("imdbID"):
            _movie_cache[result["imdbID"]] = result

    _tmdb_cache[cache_key] = result
    return result


def remove_processed_links_from_file(processed_imdb_ids, input_datei):
    """
    Entfernt die verarbeiteten IMDb-Links aus der Input-Datei.
    Kommentarzeilen (#) und Leerzeilen bleiben erhalten.
    Erstellt vor dem Überschreiben ein Backup (.bak), das dauerhaft
    erhalten bleibt (manuelles Löschen möglich).
    """
    if not processed_imdb_ids or not os.path.exists(input_datei):
        return
    with open(input_datei, "r", encoding="utf-8") as f:
        lines = f.readlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        match = re.search(r"tt\d+", line)
        if match and match.group() in processed_imdb_ids:
            continue  # Diese Zeile (Link) entfernen
        new_lines.append(line)
    backup_path = input_datei + ".bak"
    shutil.copy2(input_datei, backup_path)
    with open(input_datei, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"📦 Backup gespeichert: {backup_path}")
    print(f"🗑️ {len(processed_imdb_ids)} IMDb-Link(s) aus {input_datei} entfernt.")


def _can_write_to_path(pfad):
    """Prüft, ob in den Zielpfad geschrieben werden kann."""
    if not os.path.exists(pfad):
        return False
    return os.access(pfad, os.W_OK)


def _prompt_seasons(known_seasons=None):
    """
    Fragt den Nutzer nach der Anzahl der Staffeln.

    Args:
        known_seasons: Bekannte Staffelanzahl von TMDB (als Vorschlag).

    Returns:
        Anzahl Staffeln (int), mindestens 1.
    """
    if known_seasons:
        prompt = f"  📺 Staffeln anlegen (TMDB: {known_seasons}, Enter = übernehmen): "
    else:
        prompt = "  📺 Wie viele Staffeln anlegen? "
    try:
        eingabe = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return known_seasons or 1
    if not eingabe and known_seasons:
        return known_seasons
    try:
        n = int(eingabe)
        return max(1, n)
    except ValueError:
        if known_seasons:
            return known_seasons
        return 1


def search_by_title(title):
    """
    Sucht einen Film oder eine Serie per Titel über die TMDB-API.

    Stufe 1: Bester Treffer aus /search/multi mit Bestätigung
             (Titel, Jahr, Typ, Cast).
    Stufe 2: Bei Ablehnung nummerierte Liste der Top 5.

    Args:
        title: Suchbegriff (Film- oder Serientitel).

    Returns:
        Tuple (id, media_type, seasons) bei Erfolg:
            - Film:  ("tt1375666", "movie", None)
            - Serie: ("1396", "tv", 5)
        None bei Fehler oder Abbruch.
    """
    try:
        data = _tmdb_request("/search/multi", {"query": title})
    except Exception as e:
        print(f"  ❌ Suchfehler: {e}")
        return None

    results = [r for r in data.get("results", [])
               if r.get("media_type") in ("movie", "tv")]

    if not results:
        print(f"  ❌ Kein Ergebnis für \"{title}\".")
        return None

    # Stufe 1: Bester Treffer mit Details
    best = results[0]
    tmdb_id = str(best["id"])
    media_type = best["media_type"]

    details = get_tmdb_details(tmdb_id, media_type)
    if details and details.get("Response") == "True":
        typ_label = "Serie" if media_type == "tv" else "Film"
        actors = details.get("Actors", "N/A")
        print(f"  🔍 {details['Title']} ({details['Year']}) — {typ_label} — mit {actors}")
        try:
            antwort = input("     Korrekt? (Enter/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if antwort in ("", "j", "ja", "y", "yes"):
            if media_type == "tv":
                seasons = _prompt_seasons(details.get("Seasons"))
                print(f"  ✅ tmdb-{tmdb_id} übernommen.")
                return (tmdb_id, "tv", seasons)
            else:
                imdb_id = details.get("imdbID", "")
                if not imdb_id:
                    print("  ❌ Keine IMDb-ID für diesen Film gefunden.")
                    return None
                print(f"  ✅ {imdb_id} übernommen.")
                return (imdb_id, "movie", None)

    # Stufe 2: Listensuche
    display_results = results[:5]
    print(f"  Ergebnisse für \"{title}\":")
    for i, r in enumerate(display_results, 1):
        typ_label = "Serie" if r["media_type"] == "tv" else "Film"
        if r["media_type"] == "tv":
            name = r.get("name", "?")
            date = r.get("first_air_date", "")
        else:
            name = r.get("title", "?")
            date = r.get("release_date", "")
        year = date[:4] if date else "?"
        print(f"    [{i}] {name} ({year}) — {typ_label}")

    try:
        wahl = input("  Nummer wählen (oder leer = überspringen): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not wahl:
        return None
    try:
        idx = int(wahl) - 1
        if 0 <= idx < len(display_results):
            chosen = display_results[idx]
            tmdb_id = str(chosen["id"])
            media_type = chosen["media_type"]
            details = get_tmdb_details(tmdb_id, media_type)
            if details and details.get("Response") == "True":
                if media_type == "tv":
                    seasons = _prompt_seasons(details.get("Seasons"))
                    print(f"  ✅ tmdb-{tmdb_id} übernommen.")
                    return (tmdb_id, "tv", seasons)
                else:
                    imdb_id = details.get("imdbID", "")
                    if imdb_id:
                        print(f"  ✅ {imdb_id} übernommen.")
                        return (imdb_id, "movie", None)
                    print("  ❌ Keine IMDb-ID für diesen Film gefunden.")
        else:
            print("  Ungültige Auswahl.")
    except ValueError:
        print("  Ungültige Eingabe.")
    return None


def _prompt_for_links():
    """
    Fragt den Nutzer interaktiv nach IMDb-URLs, TMDB-URLs oder Titeln.
    Leere Eingabe beendet die Eingabe.

    Returns:
        Liste von Tupeln (id, media_type, seasons):
            - Film:  (imdb_id, "movie", None)
            - Serie: (tmdb_id, "tv", 5)
    """
    print("IMDb-URL, TMDB-URL oder Titel eingeben (leere Zeile startet die Verarbeitung):")
    seen = set()
    entries = []
    while True:
        try:
            eingabe = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not eingabe:
            break

        # TMDB-URL erkennen: themoviedb.org/tv/1396-...
        tmdb_match = re.search(r"themoviedb\.org/tv/(\d+)", eingabe)
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

        # IMDb-URL oder tt-ID erkennen
        imdb_match = re.search(r"tt\d+", eingabe)
        if imdb_match:
            imdb_id = imdb_match.group()
            if imdb_id not in seen:
                seen.add(imdb_id)
                entries.append((imdb_id, "movie", None))
            continue

        # Eingabe als Titel interpretieren (Film oder Serie)
        result = search_by_title(eingabe)
        if result:
            entry_id, media_type, seasons = result
            key = entry_id if media_type == "movie" else f"tmdb-{entry_id}"
            if key not in seen:
                seen.add(key)
                entries.append(result)
                break  # Direkt zur Verarbeitung
    return entries


def process_list(dry_run=False, interactive=False, ziel_pfad=None, input_datei=None, prompt_mode=False, direct_title=None):
    """
    Liest die Input-Datei (oder fragt interaktiv), dedupliziert IDs
    und erstellt Plex-Ordner für Filme und Serien.

    Args:
        dry_run: Wenn True, werden keine Ordner angelegt, nur die
                 geplanten Aktionen ausgegeben.
        interactive: Wenn True, nach Dry-Run-Ausgabe Bestätigung abfragen
                     und bei "j" die Ordner anlegen.
        ziel_pfad: Überschreibt den Standard-Zielpfad (z.B. per -o).
                   Gilt für Filme UND Serien.
        input_datei: Überschreibt die Standard-Input-Datei (z.B. per -f).
        prompt_mode: Wenn True, filme.txt wird nicht gelesen; stattdessen
                     werden Titel/Links interaktiv abgefragt.
        direct_title: Wenn gesetzt, wird direkt nach diesem Titel gesucht
                      (z.B. "plexname breaking bad").
    """
    pfad = ziel_pfad if ziel_pfad is not None else ZIEL_PFAD
    serien_pfad = ziel_pfad if ziel_pfad is not None else ZIEL_PFAD_SERIEN
    datei = input_datei if input_datei is not None else INPUT_DATEI
    use_from_file = True

    # Zielpfad prüfen (nur bei Datei-Modus ohne Dry-Run/Interaktiv)
    if not dry_run and not interactive and not prompt_mode:
        if not os.path.exists(pfad):
            print(f"❌ ABBRUCH: Pfad '{pfad}' nicht gefunden. NAS verbunden?")
            return
        if os.path.exists(pfad) and not _can_write_to_path(pfad):
            print(f"❌ ABBRUCH: Keine Schreibrechte für '{pfad}'.")
            return

    valid_count = 0
    if direct_title:
        # Direktsuche: Titel wurde als Argument übergeben
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
        if not os.path.exists(datei):
            print(f"Datei {datei} nicht gefunden!")
            return

        with open(datei, "r", encoding="utf-8") as f:
            lines = f.readlines()

        seen = set()
        entries = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.search(r"tt\d+", line)
            if not match:
                print(f"Ungültige URL übersprungen: {line}")
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
        print("Keine Filme zum Verarbeiten.")
        return

    if use_from_file and valid_count > len(entries):
        print(f"ℹ️ {valid_count - len(entries)} Duplikate übersprungen → {len(entries)} eindeutige Filme")

    mode = "Dry-Run" if (dry_run or interactive) else "Verarbeitung"
    print(f"Starte {mode} von {len(entries)} Einträgen...")

    to_create = []  # Bei interaktiv: (folder_name, full_path, seasons) Tupel
    successfully_processed = set()  # IMDb-IDs für Datei-Bereinigung
    count_created = 0
    count_existing = 0
    count_failed = 0

    for idx, (entry_id, media_type, seasons) in enumerate(entries, 1):
        if len(entries) > 1:
            print(f"  [{idx}/{len(entries)}] ", end="")

        # Daten abrufen und Zielpfad bestimmen
        if media_type == "tv":
            data = get_tmdb_details(entry_id, "tv")
            id_tag = f"tmdb-{entry_id}"
            target_path = serien_pfad
        else:
            data = get_movie_data(entry_id)
            id_tag = f"imdb-{entry_id}"
            target_path = pfad

        if data and data.get("Response") == "True":
            title = data["Title"]
            year = data["Year"]

            # Jahr: Bereich wie "1999–2000" auf erstes Jahr reduzieren
            year = str(year).split("–")[0].split("-")[0].strip()

            # Ordnername: Sonderzeichen entfernen (/: etc. sind unter Windows/Unix ungültig)
            clean_title = re.sub(r'[<>:"/\\|?*]', '', title)
            clean_year = re.sub(r'[<>:"/\\|?*]', '', year) or "0000"
            folder_name = f"{clean_title} ({clean_year}) {{{id_tag}}}"

            full_path = os.path.join(target_path, folder_name)
            exists = os.path.exists(full_path)

            if media_type == "movie":
                successfully_processed.add(entry_id)

            if dry_run or interactive:
                status = "würde erstellt" if not exists else "existiert bereits"
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
                    print(f"❌ Pfad '{target_path}' nicht gefunden. NAS verbunden?")
                    count_failed += 1
                    continue
                os.makedirs(full_path)
                print(f"✅ Erstellt: {folder_name}")
                if seasons:
                    for s in range(1, seasons + 1):
                        season_path = os.path.join(full_path, f"Season {s:02d}")
                        os.makedirs(season_path)
                        print(f"   ✅ Season {s:02d}")
                count_created += 1
            else:
                print(f"ℹ️ Existiert bereits: {folder_name}")
                count_existing += 1
        else:
            print(f"❌ Fehler bei ID {entry_id}: {data.get('Error') if data else 'Keine Antwort'}")
            count_failed += 1

        # Rate-Limiting: API nicht überlasten
        time.sleep(0.2)

    # Zusammenfassung
    summary_parts = []
    if count_created:
        summary_parts.append(f"{count_created} erstellt")
    if count_existing:
        summary_parts.append(f"{count_existing} existierten bereits")
    if count_failed:
        summary_parts.append(f"{count_failed} fehlgeschlagen")
    if summary_parts:
        print(f"\n📊 Zusammenfassung: {', '.join(summary_parts)}")

    # Normaler Modus: Links nach erfolgreicher Ordnererstellung aus Input-Datei entfernen
    if use_from_file and not dry_run and not interactive and successfully_processed:
        remove_processed_links_from_file(successfully_processed, datei)

    # Interaktiv: Nach Bestätigung Ordner anlegen (ohne erneute API-Aufrufe)
    if interactive and to_create:
        paths_needed = set(os.path.dirname(fp) for _, fp, _ in to_create)
        for p in paths_needed:
            if not os.path.exists(p):
                print(f"❌ ABBRUCH: Pfad '{p}' nicht gefunden. NAS verbunden?")
                return
            if not _can_write_to_path(p):
                print(f"❌ ABBRUCH: Keine Schreibrechte für '{p}'.")
                return
        antwort = input(f"\n{len(to_create)} Ordner anlegen? (j/n): ").strip().lower()
        if antwort in ("j", "ja", "y", "yes"):
            for folder_name, full_path, seasons in to_create:
                os.makedirs(full_path)
                print(f"✅ Erstellt: {folder_name}")
                if seasons:
                    for s in range(1, seasons + 1):
                        season_path = os.path.join(full_path, f"Season {s:02d}")
                        os.makedirs(season_path)
                        print(f"   ✅ Season {s:02d}")
            if use_from_file and successfully_processed:
                remove_processed_links_from_file(successfully_processed, datei)
        else:
            print("Abgebrochen.")


def _show_help():
    """Zeigt eine ausführliche Hilfe an."""
    movie_path = ZIEL_PFAD or "<nicht konfiguriert>"
    series_path = ZIEL_PFAD_SERIEN or "<nicht konfiguriert>"
    lines = [
        "🎬 plexname — Plex-Ordner aus IMDb/TMDB erstellen",
        "",
        "Verwendung:",
        "  plexname                      Interaktiver Modus (Titel eingeben)",
        "  plexname <titel>              Direktsuche (z.B. plexname breaking bad)",
        "  plexname -n <titel>           Dry-Run: nur anzeigen, nichts anlegen",
        "  plexname -i <titel>           Interaktiv: anzeigen, dann bestätigen",
        "  plexname -f filme.txt         Filme aus Datei verarbeiten (IMDb-URLs)",
        "  plexname -o /pfad <titel>     Zielpfad überschreiben (Filme + Serien)",
        "  plexname setup                Konfiguration (neu) einrichten",
        "  plexname help                 Diese Hilfe anzeigen",
        "",
        "Eingabeformate:",
        "  breaking bad                  Titelsuche (Film oder Serie via TMDB)",
        "  https://imdb.com/title/tt...  IMDb-URL → Film",
        "  tt1375666                     IMDb-ID → Film",
        "  https://themoviedb.org/tv/... TMDB-URL → Serie",
        "",
        "Ordnerformat:",
        f"  Filme:   Inception (2010) {{imdb-tt1375666}}      → {movie_path}",
        f"  Serien:  Breaking Bad (2008) {{tmdb-1396}}         → {series_path}",
        "           └── Season 01, Season 02, ...",
    ]
    print("\n".join(lines))


def _load_config():
    """Lädt die Konfiguration und setzt die Modul-Globals."""
    global API_KEY, TMDB_TOKEN, ZIEL_PFAD, ZIEL_PFAD_SERIEN
    import config
    cfg = config.get_config()
    API_KEY = cfg["omdb_api_key"]
    TMDB_TOKEN = cfg["tmdb_token"]
    ZIEL_PFAD = cfg["movie_path"]
    ZIEL_PFAD_SERIEN = cfg["series_path"]


def main():
    parser = argparse.ArgumentParser(
        prog="plexname",
        description="Plex-Ordner aus IMDb-Links / TMDB erstellen",
    )
    parser.add_argument("title", nargs="*", help="Filmtitel oder Serienname (optional, startet Direktsuche)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Zeigt nur, was erstellt würde (keine Änderungen)")
    parser.add_argument("-i", "--interactive", action="store_true", help="Dry-Run, dann Bestätigung: Ordner anlegen? (j/n)")
    parser.add_argument("-o", "--output", metavar="PFAD", help="Zielpfad für Plex-Ordner überschreiben")
    parser.add_argument("-f", "--file", metavar="DATEI", dest="input_file", help="Alternative Input-Datei (Standard: filme.txt)")
    parser.add_argument("-p", "--prompt", action="store_true", help="Nach Filmen/Serien fragen (ohne filme.txt)")
    args = parser.parse_args()

    # Setup und Help vor dem Config-Laden abfangen
    if args.title and args.title[0].lower() == "setup":
        import config
        config.run_setup()
        return

    if args.title and args.title[0].lower() == "help":
        # Config laden falls vorhanden, aber kein Setup erzwingen
        import config
        cfg = config.load_config()
        if cfg:
            global API_KEY, TMDB_TOKEN, ZIEL_PFAD, ZIEL_PFAD_SERIEN
            ZIEL_PFAD = cfg["movie_path"]
            ZIEL_PFAD_SERIEN = cfg["series_path"]
        _show_help()
        return

    # Konfiguration laden (startet Setup beim ersten Mal)
    _load_config()

    if args.title:
        # Direktsuche: "plexname breaking bad" → Titel zusammenfügen
        title = " ".join(args.title)
        process_list(dry_run=args.dry_run, interactive=args.interactive,
                     ziel_pfad=args.output, input_datei=args.input_file,
                     prompt_mode=True, direct_title=title)
    elif args.input_file:
        # Datei-Modus: nur wenn explizit -f angegeben
        process_list(dry_run=args.dry_run, interactive=args.interactive,
                     ziel_pfad=args.output, input_datei=args.input_file)
    else:
        # Ohne Argumente oder mit -p → interaktiver Prompt
        process_list(dry_run=args.dry_run, interactive=args.interactive,
                     ziel_pfad=args.output, prompt_mode=True)


if __name__ == "__main__":
    main()
