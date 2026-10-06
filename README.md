# Octave → BitChord addon

A small adapter between BitChord's custom-addon protocol and Octave's documented
search/playback-token/audio API.

## Important

This adapter only calls the public Octave API endpoints described by the
Octave integration documentation. It does **not** impersonate Deezer, scrape
credentials, or bypass a protected upstream endpoint.

Use it only where your use of the underlying music is authorized.

## Endpoints

BitChord sees:

- `GET /manifest.json`
- `GET /search?q=...`
- `GET /stream/<track-id>?quality=lossless`

The adapter calls Octave:

- `https://music.octavestreaming.com/api/search?q=...`
- `https://api.octavestreaming.com/api/playback-token`
- `https://api.octavestreaming.com/audio/<quality>?track=<id>&token=<token>`

## Local test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Then:

```bash
curl http://127.0.0.1:8080/manifest.json
curl 'http://127.0.0.1:8080/search?q=pesamale'
```

If search works, take an `id` from the returned JSON and test:

```bash
curl 'http://127.0.0.1:8080/stream/YOUR_ID?quality=lossless'
```

## Deploy to Render

1. Put these files in a GitHub repository.
2. In Render, create a new Web Service from that repository.
3. Build command:
   `pip install -r requirements.txt`
4. Start command:
   `gunicorn --bind 0.0.0.0:$PORT server:app`
5. Deploy.
6. Test:
   `https://YOUR-SERVICE.onrender.com/manifest.json`
7. Paste the service root or `/manifest.json` into BitChord → Sources → Add an addon.

## If Test fails

The adapter deliberately returns the Octave response in a normalized form.
If Octave changes its JSON schema or token method, inspect:

- `/search`
- `/stream/<id>`

and update the normalizer/token extraction accordingly.

Do not put Octave tokens into logs or source control.
# octaveadapter
