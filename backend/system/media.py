"""Media control: Spotify Web API first, Windows media keys as fallback.

Spotify handles "play [song] (by [artist])" via search + direct playback.
Bare transport commands (pause/skip/previous) go through media keys — they
work regardless of which player is active and cost zero network time.
"""
import logging
import os
import subprocess
import time

import keyboard
from rapidfuzz import fuzz

import config

log = logging.getLogger("jarvis.media")

# Three-tier confidence for Spotify search. STT mangles song titles constantly
# ("sign of the times" -> "sign off time zone"), but Spotify's own search is
# usually still capable — so unlike LOCAL file matching (where a wrong open is
# bad), we auto-play only a strong match, ASK on a plausible one, and fail only
# when even the top result is a poor match.
SONG_HIGH = 78            # auto-play
SONG_PLAUSIBLE = 50       # "did you mean ...?"

_spotify = None
_spotify_err: str | None = None


def _get_spotify():
    """Lazy-init spotipy client. Returns None if not configured."""
    global _spotify, _spotify_err
    if _spotify is not None or _spotify_err is not None:
        return _spotify
    if not (config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET):
        _spotify_err = "not configured"
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
        _spotify = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=config.SPOTIFY_CLIENT_ID,
            client_secret=config.SPOTIFY_CLIENT_SECRET,
            redirect_uri=config.SPOTIFY_REDIRECT_URI,
            scope="user-modify-playback-state user-read-playback-state",
            cache_path=str(config.SPOTIFY_TOKEN_CACHE),
            open_browser=True,
        ))
    except Exception as e:  # keep the assistant alive if auth fails
        log.error("spotify init failed: %s", e)
        _spotify_err = str(e)
    return _spotify


NO_PLAYBACK = ("Can't start playback, sir — check Spotify's open and you're "
               "logged into Premium.")


def _devices(sp) -> list[dict]:
    try:
        return sp.devices().get("devices", []) or []
    except Exception as e:
        log.warning("spotify devices() failed: %s", e)
        return []


def _pick_device(sp) -> str | None:
    """Prefer the device that's already active; otherwise any available one."""
    devices = _devices(sp)
    for d in devices:
        if d.get("is_active"):
            return d["id"]
    return devices[0]["id"] if devices else None


def _launch_spotify() -> None:
    """Start/focus the desktop app so it registers as a Spotify Connect device.

    This machine has the Microsoft Store build, which has no Spotify.exe on PATH
    — `start spotify:` alone did not reliably bring up a Connect device.
    """
    try:
        os.startfile("spotify:")            # protocol handler (Store or desktop)
        log.info("launched Spotify via protocol handler")
    except OSError as e:
        log.warning("spotify: URI launch failed (%s) — trying the Store app", e)
        try:
            subprocess.Popen(
                ["explorer.exe",
                 r"shell:AppsFolder\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"],
                creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as e2:
            log.error("could not launch Spotify: %s", e2)


def _wait_for_device(sp, seconds: int = 12) -> str | None:
    """Spotify Connect can take several seconds to register a freshly-opened app."""
    for _ in range(seconds):
        d = _pick_device(sp)
        if d:
            log.info("spotify device available after wait")
            return d
        time.sleep(1)
    return None


def _now_playing_matches(sp, track_id: str) -> bool:
    """Confirm playback actually started — never claim success on faith."""
    try:
        cur = sp.current_playback()
    except Exception:
        return False
    if not cur or not cur.get("is_playing"):
        return False
    item = cur.get("item") or {}
    return item.get("id") == track_id


def _track_label(track: dict) -> str:
    name = track["name"]
    by = track["artists"][0]["name"] if track.get("artists") else ""
    return f"{name} by {by}" if by else name


def _score_track(query: str, track: dict) -> float:
    """How well a Spotify result matches what was asked for (0..100).

    Deliberately uses ratio + token_sort (order-sensitive) rather than WRatio /
    token_set (which ignore word order and extra words). Those are too generous
    for songs: they scored a garbled 'sign off time zone' at 85 against 'Sign of
    the Times', skipping the 'did you mean' step. Here an exact request still hits
    ~100, while a mangled-but-recognisable one lands in the plausible band."""
    name = track["name"].lower()
    by = track["artists"][0]["name"].lower() if track.get("artists") else ""
    q = query.lower().strip()
    cands = [name, f"{name} {by}"]
    return max(max(fuzz.ratio(q, c), fuzz.token_sort_ratio(q, c)) for c in cands)


def search_track(song: str, artist: str | None = None):
    """(best_track, tier). tier is 'high' | 'plausible' | 'none' | 'unavailable'."""
    sp = _get_spotify()
    if sp is None:
        return None, "unavailable"
    try:
        q = f"track:{song}" + (f" artist:{artist}" if artist else "")
        items = sp.search(q=q, type="track", limit=6)["tracks"]["items"]
        if not items:
            loose = f"{song} {artist or ''}".strip()
            items = sp.search(q=loose, type="track", limit=6)["tracks"]["items"]
        if not items:
            return None, "none"
    except Exception as e:
        log.error("spotify search failed: %s", e)
        return None, "none"
    query = f"{song} {artist or ''}".strip()
    best = max(items, key=lambda t: _score_track(query, t))
    score = _score_track(query, best)
    tier = ("high" if score >= SONG_HIGH else
            "plausible" if score >= SONG_PLAUSIBLE else "none")
    log.info("spotify search %r -> %s (score %.0f, %s)", query,
             _track_label(best), score, tier)
    return best, tier


def play_song(song: str, artist: str | None = None) -> str:
    """Search Spotify (top result) and start playback. Auto-plays a high OR
    plausible match — the interactive 'did you mean' path lives in fast_path,
    which has a confirmation channel; this is the direct-play entry point."""
    track, tier = search_track(song, artist)
    if tier == "unavailable":
        return f"Spotify isn't set up, sir, so I can't play {song}."
    if tier == "none" or track is None:
        return f"I couldn't find {song} on Spotify, sir."
    return play_track(track)


def play_track(track: dict) -> str:
    """Start playback of an already-chosen track, verifying it truly plays.

    Flow: start on an available Connect device; if none, launch the app and wait;
    if the Web API still can't (no device / not Premium), hand the URI to the
    desktop app; then verify — never claim success on faith.
    """
    sp = _get_spotify()
    if sp is None:
        return NO_PLAYBACK
    name = track["name"]
    said = f"Playing {_track_label(track)}."
    log.info("spotify: playing %s (%s)", _track_label(track), track["uri"])

    # ── 2/3. play via the Web API, launching the app if needed ──
    device = _pick_device(sp)
    if device is None:
        log.info("no Spotify device — launching the app and waiting")
        _launch_spotify()
        device = _wait_for_device(sp)

    if device is not None:
        try:
            sp.start_playback(device_id=device, uris=[track["uri"]])
            time.sleep(1.2)
            if _now_playing_matches(sp, track["id"]):
                log.info("spotify: playback started via Web API")
                return said
            log.warning("start_playback returned but nothing is playing")
        except Exception as e:
            # 403 = Premium required / restricted device
            log.error("spotify start_playback failed: %s", e)

    # ── 4. fallback: let the desktop app play the URI itself ────
    # (works without Spotify Connect, and without the playback API)
    log.info("falling back to the spotify: track URI")
    try:
        os.startfile(track["uri"])          # spotify:track:<id>
    except OSError as e:
        log.error("track URI launch failed: %s", e)
        return NO_PLAYBACK

    for _ in range(8):                      # ── 5. verify, don't assume
        time.sleep(1)
        if _now_playing_matches(sp, track["id"]):
            log.info("spotify: playback started via track URI")
            return said

    log.error("spotify: could not confirm playback of %r", name)
    return NO_PLAYBACK


# ── Media-key transport (player-agnostic, offline) ─────────────
def pause_play() -> str:
    keyboard.send("play/pause media")
    return "Done."


def next_track() -> str:
    keyboard.send("next track")
    return "Skipping."


def previous_track() -> str:
    keyboard.send("previous track")
    return "Going back."


def stop() -> str:
    keyboard.send("stop media")
    return "Stopped."


def close_active_window() -> str:
    keyboard.send("alt+f4")
    return "Closed."
