import os
import time
import json
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

SEARCH_BASE = os.getenv("OCTAVE_SEARCH_BASE", "https://music.octavestreaming.com/api/search")
TOKEN_URL = os.getenv("OCTAVE_TOKEN_URL", "https://api.octavestreaming.com/api/playback-token")
AUDIO_BASE = os.getenv("OCTAVE_AUDIO_BASE", "https://api.octavestreaming.com/audio")

# Short in-memory token cache. Do not log tokens.
_token_cache = {"token": None, "expires_at": 0}

MANIFEST = {
    "id": "dev.octave.bitchord",
    "name": "Octave Lossless",
    "version": "1.0.0",
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
                {"label": "High", "value": "320"},
                {"label": "Low", "value": "128"},
            ],
        }
    ],
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "BitChord-Octave-Addon/1.0",
    "Accept": "application/json",
})


def first(obj, *keys):
    """Return the first non-empty value found under common key names."""
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if value not in (None, ""):
                return value
    return None


def extract_token(data):
    # Handle common response shapes without assuming one exact wrapper.
    if isinstance(data, str):
        return data

    if isinstance(data, dict):
        for key in ("token", "access_token", "playbackToken", "playback_token"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value

        for wrapper in ("data", "result", "playback", "session"):
            nested = data.get(wrapper)
            token = extract_token(nested)
            if token:
                return token

    return None


def get_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    # The public Octave integration documentation lists this endpoint as
    # the authenticated playback-token endpoint. Try GET first, then POST
    # because deployments can change the verb without changing the URL.
    for method in ("get", "post"):
        try:
            if method == "get":
                r = SESSION.get(TOKEN_URL, timeout=12)
            else:
                r = SESSION.post(TOKEN_URL, json={}, timeout=12)

            if not r.ok:
                continue

            try:
                data = r.json()
            except ValueError:
                data = r.text

            token = extract_token(data)
            if token:
                # Keep a conservative cache lifetime. Octave can rotate passes.
                _token_cache["token"] = token
                _token_cache["expires_at"] = now + int(os.getenv("TOKEN_CACHE_SECONDS", "300"))
                return token
        except requests.RequestException:
            continue

    return None


def normalize_tracks(data):
    """
    Convert common Octave search response shapes into BitChord's:
      {"tracks": [...]}
    """
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = None
        for key in ("tracks", "results", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                raw = value
                break
        if raw is None:
            # Some APIs wrap results one level deeper.
            for value in data.values():
                if isinstance(value, dict):
                    nested = normalize_tracks(value)
                    if nested["tracks"]:
                        return nested
        raw = raw or []
    else:
        raw = []

    tracks = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        tid = first(item, "id", "trackId", "track_id", "uuid")
        title = first(item, "title", "name")
        if tid is None or title is None:
            continue

        artist_obj = item.get("artist") or item.get("artists")
        if isinstance(artist_obj, list):
            artist = ", ".join(
                str(first(a, "name", "title") or "")
                for a in artist_obj if isinstance(a, dict)
            ).strip(", ")
        elif isinstance(artist_obj, dict):
            artist = first(artist_obj, "name", "title")
        else:
            artist = artist_obj if isinstance(artist_obj, str) else first(
                item, "artistName", "artist_name"
            )

        album_obj = item.get("album")
        if isinstance(album_obj, dict):
            album = first(album_obj, "title", "name")
            artwork = first(
                album_obj, "cover", "coverUrl", "cover_url",
                "artwork", "artworkURL", "artwork_url"
            )
        else:
            album = album_obj if isinstance(album_obj, str) else first(
                item, "albumTitle", "album_name"
            )
            artwork = None

        artwork = artwork or first(
            item, "cover", "coverUrl", "cover_url",
            "artwork", "artworkURL", "artwork_url"
        )

        duration = first(item, "duration", "durationSeconds", "duration_seconds")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None

        tracks.append({
            "id": str(tid),
            "title": str(title),
            "artist": str(artist or ""),
            "album": str(album or ""),
            **({"duration": duration} if duration is not None else {}),
            **({"artworkURL": str(artwork)} if artwork else {}),
            "format": "flac" if True else "audio",
            "audioQuality": "LOSSLESS",
        })

    return {"tracks": tracks}


@app.get("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "octave-bitchord-addon"})


@app.get("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"tracks": []})

    try:
        r = SESSION.get(
            SEARCH_BASE,
            params={"q": q},
            timeout=15,
        )
        if r.status_code == 404:
            return jsonify({"tracks": []})
        r.raise_for_status()
        return jsonify(normalize_tracks(r.json()))
    except requests.RequestException as exc:
        return jsonify({"tracks": [], "error": f"Octave search unavailable: {exc}"}), 502
    except ValueError:
        return jsonify({"tracks": [], "error": "Octave returned non-JSON search data"}), 502


@app.get("/stream/<path:track_id>")
def stream(track_id):
    quality = (request.args.get("quality") or "lossless").lower()

    # Only use the public Octave playback-token flow. No credential scraping,
    # account impersonation, or bypass of protected Deezer endpoints is done here.
    token = get_token()
    if not token:
        return jsonify({"error": "Could not obtain an Octave playback token"}), 502

    # Octave's documented integration uses:
    # /audio/{quality}?track={id}&token={token}
    url = (
        f"{AUDIO_BASE.rstrip('/')}/{quote(quality, safe='')}"
        f"?track={quote(track_id, safe='')}&token={quote(token, safe='')}"
    )

    # BitChord opens this URL directly.
    codec = "flac" if quality == "lossless" else "aac"
    container = "flac" if quality == "lossless" else "mp4"

    return jsonify({
        "url": url,
        "format": codec,
        "quality": "Lossless" if quality == "lossless" else quality,
        "codec": codec,
        "container": container,
        "manifest": "none",
        "encrypted": False,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
