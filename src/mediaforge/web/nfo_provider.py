import os
import xml.etree.ElementTree as ET
from xml.dom import minidom
from .db import get_setting
import logging

logger = logging.getLogger("MediaForge")

def _bool_setting(key, default="1"):
    return get_setting(key, default) == "1"

def _create_xml_element(parent, tag, text=None):
    elem = ET.SubElement(parent, tag)
    if text is not None:
        elem.text = str(text)
    return elem

def _write_nfo(root_elem, filepath):
    xml_str = ET.tostring(root_elem, encoding='utf-8')
    parsed = minidom.parseString(xml_str)
    pretty_xml = parsed.toprettyxml(indent="  ")
    # Remove the xml declaration if we want, but it's fine to keep it.
    # Usually NFOs have it.
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(pretty_xml)
        logger.info(f"[JellyfinNFO] Wrote NFO: {filepath}")
    except Exception as e:
        logger.error(f"[JellyfinNFO] Failed to write NFO {filepath}: {e}")

def _add_common_metadata(root, tmdb_data):
    if _bool_setting("jellyfin_nfo_meta_plot"):
        _create_xml_element(root, "plot", tmdb_data.get("overview") or tmdb_data.get("plot", ""))
        _create_xml_element(root, "outline", tmdb_data.get("overview") or tmdb_data.get("plot", ""))

    if _bool_setting("jellyfin_nfo_meta_genres"):
        for genre in tmdb_data.get("genres", []):
            name = genre if isinstance(genre, str) else genre.get("name")
            if name:
                _create_xml_element(root, "genre", name)

    if _bool_setting("jellyfin_nfo_meta_rating"):
        if tmdb_data.get("vote_average"):
            _create_xml_element(root, "rating", str(tmdb_data.get("vote_average")))

    if _bool_setting("jellyfin_nfo_meta_fsk"):
        if tmdb_data.get("fsk") or tmdb_data.get("certification"):
            _create_xml_element(root, "mpaa", tmdb_data.get("fsk") or tmdb_data.get("certification"))

    if _bool_setting("jellyfin_nfo_meta_trailer"):
        trailer_key = tmdb_data.get("trailer_key")
        if trailer_key:
            _create_xml_element(root, "trailer", f"plugin://plugin.video.youtube/?action=play_video&videoid={trailer_key}")

    if _bool_setting("jellyfin_nfo_meta_date"):
        date = tmdb_data.get("release_date") or tmdb_data.get("first_air_date") or tmdb_data.get("air_date")
        if date:
            _create_xml_element(root, "premiered", date)
            _create_xml_element(root, "releasedate", date)
            year = date.split("-")[0]
            if year:
                _create_xml_element(root, "year", year)

    if _bool_setting("jellyfin_nfo_meta_studio"):
        for network in tmdb_data.get("networks", []) + tmdb_data.get("production_companies", []):
            if network.get("name"):
                _create_xml_element(root, "studio", network["name"])

    if _bool_setting("jellyfin_nfo_meta_actors"):
        credits = tmdb_data.get("credits", {})
        for cast in credits.get("cast", []):
            actor_elem = ET.SubElement(root, "actor")
            _create_xml_element(actor_elem, "name", cast.get("name"))
            _create_xml_element(actor_elem, "role", cast.get("character"))
            if cast.get("profile_path"):
                _create_xml_element(actor_elem, "thumb", f"https://image.tmdb.org/t/p/w500{cast['profile_path']}")

    # External IDs
    ext_ids = tmdb_data.get("external_ids", {})
    if ext_ids.get("imdb_id"):
        _create_xml_element(root, "imdbid", ext_ids["imdb_id"])
    if tmdb_data.get("tmdb_id") or tmdb_data.get("id"):
        _create_xml_element(root, "tmdbid", tmdb_data.get("tmdb_id") or tmdb_data.get("id"))
    if ext_ids.get("tvdb_id"):
        _create_xml_element(root, "tvdbid", ext_ids["tvdb_id"])

def generate_nfo_for_download(file_path, tmdb_data, media_type, season_data=None, episode_data=None):
    if not _bool_setting("jellyfin_nfo_enabled", "0"):
        return

    # tmdb_data contains the show/movie level cached data.
    # it includes the "raw_details" key which we appended credits/external_ids to.
    show_details = tmdb_data.get("raw_details", tmdb_data)
    show_details["fsk"] = tmdb_data.get("fsk")
    show_details["trailer_key"] = tmdb_data.get("trailer_key")
    show_details["tmdb_id"] = tmdb_data.get("tmdb_id")

    dir_name = os.path.dirname(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]

    if media_type == "movie":
        if _bool_setting("jellyfin_nfo_create_movie"):
            # Check if there are other files in the directory. If it's in its own folder, movie.nfo is preferred.
            # But creating {base_name}.nfo is safer. We'll do both or just {base_name}.nfo.
            nfo_path = os.path.join(dir_name, f"{base_name}.nfo")
            
            root = ET.Element("movie")
            _create_xml_element(root, "title", tmdb_data.get("title") or show_details.get("title"))
            _create_xml_element(root, "originaltitle", show_details.get("original_title"))
            
            _add_common_metadata(root, show_details)
            _write_nfo(root, nfo_path)

            # If "Film-Unterordner aktivieren" is on, also create movie.nfo
            if _bool_setting("movie_subfolder", "0") or _bool_setting("filmpalast_movie_subfolder", "0"):
                movie_nfo_path = os.path.join(dir_name, "movie.nfo")
                if not os.path.exists(movie_nfo_path):
                    _write_nfo(root, movie_nfo_path)

    elif media_type in ("tv", "series"):
        # 1. tvshow.nfo (in the series root)
        if _bool_setting("jellyfin_nfo_create_series"):
            # For episodes, file_path is usually Series/Season X/Episode.mkv
            # So series root is two directories up.
            series_dir = os.path.dirname(os.path.dirname(file_path))
            # Wait, what if there's no season folder?
            # MediaForge usually puts them in `SeriesName/Season X/Episode...`
            # Let's assume series_dir is correct if it ends with "Season X"
            if "Season" in os.path.basename(dir_name) or "Staffel" in os.path.basename(dir_name):
                series_dir = os.path.dirname(dir_name)
            else:
                series_dir = dir_name
                
            tvshow_nfo_path = os.path.join(series_dir, "tvshow.nfo")
            if not os.path.exists(tvshow_nfo_path):
                root = ET.Element("tvshow")
                _create_xml_element(root, "title", tmdb_data.get("title") or show_details.get("name"))
                _create_xml_element(root, "originaltitle", show_details.get("original_name"))
                _add_common_metadata(root, show_details)
                _write_nfo(root, tvshow_nfo_path)

        # 2. season.nfo (in the season root)
        if _bool_setting("jellyfin_nfo_create_season") and season_data:
            season_nfo_path = os.path.join(dir_name, "season.nfo")
            if not os.path.exists(season_nfo_path):
                root = ET.Element("season")
                _create_xml_element(root, "title", season_data.get("name"))
                _create_xml_element(root, "seasonnumber", season_data.get("season_number"))
                _add_common_metadata(root, season_data)
                _write_nfo(root, season_nfo_path)

        # 3. episode.nfo (<filename>.nfo)
        if _bool_setting("jellyfin_nfo_create_episode") and episode_data:
            ep_nfo_path = os.path.join(dir_name, f"{base_name}.nfo")
            root = ET.Element("episodedetails")
            _create_xml_element(root, "title", episode_data.get("name"))
            _create_xml_element(root, "season", episode_data.get("season_number"))
            _create_xml_element(root, "episode", episode_data.get("episode_number"))
            
            _add_common_metadata(root, episode_data)
            _write_nfo(root, ep_nfo_path)
