import os
from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    jsonify,
    url_for,
)
from flask_session import Session
from dotenv import load_dotenv

from spotify.client import (
    get_auth_url,
    get_token_from_code,
    refresh_token_if_expired,
    get_spotify_client,
    get_user_playlists,
    get_playlist_tracks,
    get_audio_features,
    merge_tracks_with_features,
    reorder_playlist,
    create_playlist,
)
from sequencer.algorithms import apply as apply_algorithm

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# Server-side filesystem sessions — avoids the 4KB cookie size limit
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = os.path.join(os.path.dirname(__file__), ".flask_session")
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
Session(app)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_sp():
    """
    Return an authenticated Spotipy client, refreshing the token if needed.
    Returns None if the user is not logged in.
    """
    token_info = session.get("token_info")
    if not token_info:
        return None
    token_info = refresh_token_if_expired(token_info)
    session["token_info"] = token_info
    return get_spotify_client(token_info)


def _require_auth():
    """Return a redirect to /login if the user is not authenticated."""
    if not session.get("token_info"):
        return redirect(url_for("login"))
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    logged_in = bool(session.get("token_info"))
    return render_template("index.html", logged_in=logged_in)


@app.route("/login")
def login():
    auth_url = get_auth_url()
    return redirect(auth_url)


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return render_template("index.html", error=error, logged_in=False)

    code = request.args.get("code")
    if not code:
        return redirect(url_for("index"))

    token_info = get_token_from_code(code)
    session["token_info"] = token_info
    return redirect(url_for("playlists"))


@app.route("/playlists")
def playlists():
    guard = _require_auth()
    if guard:
        return guard

    sp = _get_sp()
    try:
        user_playlists = get_user_playlists(sp)
        user = sp.me()
    except Exception as e:
        return render_template("playlists.html", error=str(e), playlists=[], user=None)

    return render_template("playlists.html", playlists=user_playlists, user=user)


@app.route("/sequencer/<playlist_id>")
def sequencer(playlist_id):
    guard = _require_auth()
    if guard:
        return guard

    sp = _get_sp()
    try:
        tracks = get_playlist_tracks(sp, playlist_id)
        if not tracks:
            return render_template(
                "sequencer.html",
                error="This playlist has no playable tracks.",
                playlist=None,
                tracks=[],
            )

        features_map = get_audio_features(sp, tracks)
        enriched = merge_tracks_with_features(tracks, features_map)

        playlist_meta = sp.playlist(playlist_id, fields="id,name,images,tracks(total)")
        cover = playlist_meta["images"][0]["url"] if playlist_meta.get("images") else None
        playlist = {
            "id": playlist_id,
            "name": playlist_meta["name"],
            "cover": cover,
            "n_tracks": len(enriched),
        }

        # Store enriched tracks in session for use by /sequence and /save
        session["current_playlist_id"] = playlist_id
        session["current_playlist_name"] = playlist_meta["name"]
        session["enriched_tracks"] = enriched

    except Exception as e:
        return render_template(
            "sequencer.html", error=str(e), playlist=None, tracks=[]
        )

    return render_template("sequencer.html", playlist=playlist, tracks=enriched)


@app.route("/sequence", methods=["POST"])
def sequence():
    guard = _require_auth()
    if guard:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    algorithm = data.get("algorithm")
    if not algorithm:
        return jsonify({"error": "Missing algorithm parameter"}), 400

    enriched = session.get("enriched_tracks")
    if not enriched:
        return jsonify({"error": "No tracks loaded. Open a playlist first."}), 400

    try:
        sequenced = apply_algorithm(algorithm, enriched)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Sequencing failed: {e}"}), 500

    # Persist sequenced order for /save
    session["sequenced_tracks"] = sequenced
    session["current_algorithm"] = algorithm

    return jsonify({
        "tracks": [
            {
                "id": t["id"],
                "uri": t["uri"],
                "name": t["name"],
                "artist": t["artist"],
                "cover": t.get("cover"),
                "tempo": round(t.get("tempo", 0), 1),
                "energy": round(t.get("energy", 0), 3),
                "valence": round(t.get("valence", 0), 3),
            }
            for t in sequenced
        ]
    })


@app.route("/result")
def result():
    guard = _require_auth()
    if guard:
        return guard

    original = session.get("enriched_tracks")
    sequenced = session.get("sequenced_tracks")
    playlist_name = session.get("current_playlist_name", "")
    algorithm = session.get("current_algorithm", "")

    if not original or not sequenced:
        return redirect(url_for("playlists"))

    return render_template(
        "result.html",
        original=original,
        sequenced=sequenced,
        playlist_name=playlist_name,
        algorithm=algorithm,
        playlist_id=session.get("current_playlist_id"),
    )


@app.route("/save", methods=["POST"])
def save():
    guard = _require_auth()
    if guard:
        return jsonify({"error": "Not authenticated"}), 401

    sp = _get_sp()
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")  # "overwrite" or "new"

    sequenced = session.get("sequenced_tracks")
    playlist_id = session.get("current_playlist_id")
    playlist_name = session.get("current_playlist_name", "Playlist")

    if not sequenced:
        return jsonify({"error": "No sequenced tracks found."}), 400

    track_uris = [t["uri"] for t in sequenced]

    try:
        if mode == "overwrite":
            if not playlist_id:
                return jsonify({"error": "Missing playlist ID."}), 400
            reorder_playlist(sp, playlist_id, track_uris)
            return jsonify({"status": "saved", "message": "Playlist updated on Spotify."})

        elif mode == "new":
            new_playlist = create_playlist(sp, playlist_name, track_uris)
            return jsonify({
                "status": "saved",
                "message": f'New playlist "{new_playlist["name"]}" created.',
                "playlist_url": new_playlist["external_urls"]["spotify"],
            })

        else:
            return jsonify({"error": "Invalid mode. Use 'overwrite' or 'new'."}), 400

    except Exception as e:
        return jsonify({"error": f"Save failed: {e}"}), 500


@app.route("/logout")
def logout():
    session.clear()
    # Remove Spotipy cache file if present
    cache_path = ".cache"
    if os.path.exists(cache_path):
        os.remove(cache_path)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
