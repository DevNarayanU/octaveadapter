import os
import time
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# Current Octave web-app endpoints observed from the browser network requests.
SEARCH_URL = os.getenv(
    "OCTAVE_SEARCH_URL",
    "https://api.octavestreaming.com/page",
)
TOKEN_URL = os.getenv(
    "OCTAVE_TOKEN_URL",
    "https://api.octavestreaming.com/playback-token",
)
LOSSLESS_URL = os.getenv(
    "OCTAVE_LOSSLESS_URL",
    "https://api.octavestreaming.com/audio/lossless",
)

# Keep the same search source used by the Octave web app.
SEARCH_LIMIT = int(os.getenv("OCTAVE_SEARCH_LIMIT", "30"))

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; Octave-BitChord/1.1)",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://music.octavestreaming.com/",
})

token_cache = {"token": None, "expires_at": 0}

# Metadata cache populated by /page search so /stream does not depend on
# BitChord forwarding title/artist/duration as query parameters.
track_cache = {}


MANIFEST = {
    "id": "dev.octave.bitchord",
    "name": "Octave Lossless",
    "version": "1.2.0",
    "resources": ["search", "stream"],
    "allowDownloads": 0,
    "checkValidLossless": 0,
    "settings": [
        {
            "key": "quality",
            "type": "select",
            "default": "lossless",
            "options": [
                {"label": "Lossless", "value": "lossless"},
            ],
        }
    ],
}


def get_token():
    now = time.time()

    if token_cache["token"] and now < token_cache["expires_at"] - 30:
        return token_cache["token"]

    try:
        r = session.get(TOKEN_URL, timeout=15)
        r.raise_for_status()
        data = r.json()

        token = data.get("token")
        expires = int(data.get("expiresIn", 1800))

        if not token:
            return None

        token_cache["token"] = token
        token_cache["expires_at"] = now + expires
        return token

    except (requests.RequestException, ValueError, TypeError):
        return None


def make_track(item, kind="track"):
    """Normalize Octave's current music search response."""
    if not isinstance(item, dict):
        return None

    track_id = item.get("id")
    title = item.get("title")
    if not track_id or not title:
        return None

    artist_obj = item.get("artist")
    if isinstance(artist_obj, dict):
        artist = artist_obj.get("name", "")
    else:
        artist = str(artist_obj or "")

    # Prefer the full artist list when available.
    artists = item.get("artists")
    if isinstance(artists, list):
        names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
        if names:
            artist = ", ".join(names)

    album_obj = item.get("album")
    album = album_obj.get("title", "") if isinstance(album_obj, dict) else str(album_obj or "")

    artwork = ""
    if isinstance(album_obj, dict):
        artwork = album_obj.get("cover_big") or album_obj.get("cover_medium") or album_obj.get("cover_small") or ""

    duration = item.get("duration", 0)

    track = {
        "id": str(track_id),
        "title": str(title),
        "artist": artist,
        "album": album,
        "duration": duration if duration is not None else 0,
        "artworkURL": artwork,
        "format": "flac",
        "codec": "flac",
        "container": "flac",
        "audioQuality": "LOSSLESS",
    }

    # Keep exactly the fields needed to construct Octave's /audio/lossless URL.
    track_cache[str(track_id)] = {
        "title": str(title),
        "artist": artist,
        "duration": duration if duration is not None else 0,
    }
    return track


@app.get("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "octave-bitchord-addon",
        "version": "1.2.0",
    })


@app.get("/search")
def search():
    q = (request.args.get("q") or "").strip()

    if not q:
        return jsonify({"tracks": []})

    try:
        # Current Octave music search endpoint observed in the web app:
        # /page?query=<q>&limit=30
        r = session.get(
            SEARCH_URL,
            params={
                "query": q,
                "limit": SEARCH_LIMIT,
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        tracks = []

        # Current Octave /page response contains music in `tracks`.
        for item in data.get("tracks", []):
            track = make_track(item, "track")
            if track:
                tracks.append(track)

        return jsonify({"tracks": tracks})

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        return jsonify({
            "tracks": [],
            "error": f"Octave search HTTP {status}",
        }), 502

    except requests.RequestException as exc:
        return jsonify({
            "tracks": [],
            "error": f"Octave search unavailable: {exc}",
        }), 502

    except ValueError:
        return jsonify({
            "tracks": [],
            "error": "Octave returned invalid JSON",
        }), 502


@app.get("/stream/<path:track_id>")
def stream(track_id):
    token = get_token()

    if not token:
        return jsonify({
            "error": "Could not obtain Octave playback token"
        }), 502

    # Use metadata cached during /search. BitChord normally sends only the
    # stream ID, so do not require title/artist/duration query parameters.
    meta = track_cache.get(str(track_id), {})
    title = meta.get("title") or request.args.get("title", "")
    artist = meta.get("artist") or request.args.get("artist", "")
    duration = meta.get("duration")
    if duration is None:
        duration = request.args.get("duration", "0")

    params = {
        "track": track_id,
        "t": title,
        "a": artist,
        "d": duration,
        "x": "0",
        "k": token,
    }

    # Return the URL rather than proxying the audio through Render.
    # This keeps the FLAC bytes between BitChord and Octave.
    url = LOSSLESS_URL + "?" + "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
        for k, v in params.items()
    )

    return jsonify({
        "url": url,
        "format": "flac",
        "codec": "flac",
        "container": "flac",
        "quality": "Lossless",
        "encrypted": False,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
