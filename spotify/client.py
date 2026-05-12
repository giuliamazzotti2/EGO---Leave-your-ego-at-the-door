import os
import hashlib
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

SCOPE = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-public "
    "playlist-modify-private"
)

CHUNK_SIZE = 100  # Spotify API limit for audio features and playlist operations


def _get_oauth():
    return SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
        scope=SCOPE,
        cache_path=".cache",
        show_dialog=True,
    )


def get_spotify_client(token_info):
    """Return an authenticated Spotipy client from a stored token dict."""
    return spotipy.Spotify(auth=token_info["access_token"])


def get_auth_url():
    """Return the Spotify authorization URL to redirect the user to."""
    return _get_oauth().get_authorize_url()


def get_token_from_code(code):
    """Exchange the authorization code for a token dict."""
    return _get_oauth().get_access_token(code, as_dict=True, check_cache=False)


def refresh_token_if_expired(token_info):
    """Return a fresh token dict if the current one is expired, else return as-is."""
    oauth = _get_oauth()
    if oauth.is_token_expired(token_info):
        return oauth.refresh_access_token(token_info["refresh_token"])
    return token_info


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _ms_to_duration(ms):
    """Convert milliseconds to a 'Xh Ym' or 'Ym Zs' string."""
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours:
        return f"{hours}h {minutes}m"
    seconds = total_seconds % 60
    return f"{minutes}m {seconds}s"


def get_user_playlists(sp):
    """
    Return all playlists owned or followed by the current user.

    Each item: {id, name, cover, n_tracks, duration_ms, duration_str, owner}
    """
    user_id = sp.me()["id"]
    playlists = []
    results = sp.current_user_playlists(limit=50)

    while results:
        for item in results["items"]:
            if item is None:
                continue
            if (item.get("owner") or {}).get("id") != user_id:
                continue
            cover = item["images"][0]["url"] if item.get("images") else None
            track_collection = item.get("items") or item.get("tracks") or {}
            playlists.append({
                "id": item["id"],
                "name": item["name"],
                "cover": cover,
                "n_tracks": track_collection.get("total", 0),
                "owner": (item.get("owner") or {}).get("display_name", ""),
            })
        results = sp.next(results) if results.get("next") else None

    return playlists


def get_playlist_tracks(sp, playlist_id):
    """
    Return all tracks in a playlist with basic metadata.

    Each item: {id, uri, name, artist, album, cover, preview_url, duration_ms}
    Skips local files and None entries.
    """
    tracks = []
    # Use the /items endpoint (replaces deprecated /tracks, required for apps created after Nov 2024)
    results = sp._get(
        f"playlists/{playlist_id}/items",
        limit=100,
        offset=0,
        additional_types="track",
    )

    while results:
        for item in results["items"]:
            # /items endpoint uses "item" key, old /tracks used "track"
            track = item.get("item") or item.get("track")
            if not track or track.get("id") is None:
                continue
            cover = None
            images = track.get("album", {}).get("images")
            if images:
                cover = images[0]["url"]
            artists = track.get("artists", [])
            artist = artists[0]["name"] if artists else "Unknown"
            tracks.append({
                "id": track["id"],
                "uri": track["uri"],
                "name": track["name"],
                "artist": artist,
                "album": track["album"]["name"],
                "cover": cover,
                "preview_url": track.get("preview_url"),
                "duration_ms": track.get("duration_ms", 0),
            })
        results = sp.next(results) if results.get("next") else None

    return tracks


def _deezer_lookup(track):
    """
    Fetch BPM and gain for a single track from Deezer.

    Searches by title+artist, then fetches the full track object for BPM.
    Returns (spotify_id, {"tempo": float, "loudness": float}) or (spotify_id, None).
    """
    try:
        query = f'track:"{track["name"]}" artist:"{track["artist"]}"'
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": 1},
            timeout=5,
        )
        hits = r.json().get("data", [])
        if not hits:
            return track["id"], None
        deezer_id = hits[0]["id"]
        r2 = requests.get(f"https://api.deezer.com/track/{deezer_id}", timeout=5)
        t = r2.json()
        bpm = t.get("bpm")
        if not bpm or bpm == 0:
            return track["id"], None
        return track["id"], {
            "tempo":    round(float(bpm), 1),
            "loudness": round(float(t.get("gain", -10.0)), 1),
        }
    except Exception:
        return track["id"], None


def get_audio_features(sp, tracks):
    """
    Return audio features for a list of track dicts.

    Priority:
    1. Spotify /audio-features (real data — requires Extended Access for new apps)
    2. Deezer API (real BPM + gain; energy/valence/danceability stay synthetic)
    3. Hash-based synthetic fallback (deterministic, no external call)
    """
    track_ids = [t["id"] for t in tracks]
    KEYS = {
        "energy", "valence", "danceability", "tempo",
        "acousticness", "instrumentalness", "speechiness", "loudness",
    }
    features_map = {}
    spotify_ok = True

    # --- 1. Spotify audio-features ---
    for i in range(0, len(track_ids), CHUNK_SIZE):
        chunk = track_ids[i : i + CHUNK_SIZE]
        try:
            results = sp.audio_features(chunk)
            if not results:
                continue
            for feat in results:
                if feat is None:
                    continue
                features_map[feat["id"]] = {k: feat[k] for k in KEYS if k in feat}
        except Exception:
            spotify_ok = False
            break

    if spotify_ok:
        return features_map

    # --- 2. Deezer BPM (parallel) ---
    deezer_map = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_deezer_lookup, t): t for t in tracks}
        for future in as_completed(futures):
            tid, data = future.result()
            if data:
                deezer_map[tid] = data

    # Build features combining real Deezer BPM + synthetic energy/valence
    for track in tracks:
        tid = track["id"]
        synth = _synthetic_features(tid, track.get("duration_ms", 240000))
        deezer = deezer_map.get(tid, {})
        features_map[tid] = {**synth, **deezer}  # Deezer overwrites tempo+loudness

    return features_map


def _synthetic_features(track_id, duration_ms):
    """
    Generate deterministic pseudo-features from track ID hash + duration.

    Used when Spotify's audio-features endpoint is unavailable.
    Values are consistent (same track always gets the same result) and varied
    across tracks, making the sorting algorithms meaningful for demo purposes.
    """
    h = int(hashlib.md5(track_id.encode()).hexdigest(), 16)
    b0 = ((h >> 0)  & 0xFF) / 255.0
    b1 = ((h >> 8)  & 0xFF) / 255.0
    b2 = ((h >> 16) & 0xFF) / 255.0
    b3 = ((h >> 24) & 0xFF) / 255.0

    # Longer tracks tend to be more ambient → lower energy
    dur_minutes = max(1, duration_ms / 60000)
    dur_factor = max(0.0, min(1.0, 1.0 - (dur_minutes - 2) / 6))

    energy  = round(b0 * 0.6 + dur_factor * 0.4, 3)
    valence = round(b1, 3)
    tempo   = round(80 + b2 * 100, 1)   # 80–180 BPM range

    return {
        "energy":           energy,
        "valence":          valence,
        "danceability":     round(b3, 3),
        "tempo":            tempo,
        "acousticness":     round(1.0 - dur_factor, 3),
        "instrumentalness": 0.0,
        "speechiness":      0.0,
        "loudness":         round(-30 + energy * 20, 1),
    }


def merge_tracks_with_features(tracks, features_map):
    """
    Merge track metadata list with the audio features map.

    Tracks missing features receive synthetic values derived from their ID and
    duration — varied and consistent, so sorting algorithms remain meaningful.
    """
    merged = []
    for track in tracks:
        feat = features_map.get(track["id"])
        if feat is None:
            feat = _synthetic_features(track["id"], track.get("duration_ms", 240000))
        merged.append({**track, **feat})
    return merged


def _playlist_add_items_new(sp, playlist_id, uris):
    """POST to the new /items endpoint (replaces deprecated /tracks)."""
    for i in range(0, len(uris), CHUNK_SIZE):
        chunk = uris[i : i + CHUNK_SIZE]
        sp._post(f"playlists/{playlist_id}/items", payload={"uris": chunk})


def reorder_playlist(sp, playlist_id, track_uris):
    """
    Replace the entire playlist with the given ordered list of track URIs.

    Uses PUT /items to clear+set first 100, then POST /items for the rest.
    """
    first_chunk = track_uris[:CHUNK_SIZE]
    sp._put(f"playlists/{playlist_id}/items", payload={"uris": first_chunk})
    if len(track_uris) > CHUNK_SIZE:
        _playlist_add_items_new(sp, playlist_id, track_uris[CHUNK_SIZE:])


def create_playlist(sp, original_name, track_uris):
    """
    Create a new playlist named 'EGO — <original_name>' and populate it.

    Returns the new playlist dict from the API.
    """
    user_id = sp.me()["id"]
    new_name = f"EGO — {original_name}"
    playlist = sp.user_playlist_create(
        user=user_id,
        name=new_name,
        public=False,
        description="Sequenced with EGO — Leave your ego at the door.",
    )
    _playlist_add_items_new(sp, playlist["id"], track_uris)
    return playlist
