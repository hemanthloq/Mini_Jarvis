"""Current weather via wttr.in — free, no API key, no signup.

Used in the boot briefing. Kept short and defensive: if the service is slow or
down, the briefing simply omits weather rather than failing.
"""
import logging

import httpx

import config

log = logging.getLogger("jarvis.weather")

CITY = config.WEATHER_CITY


def get_weather(city: str | None = None, timeout: float = 6.0) -> dict | None:
    """Return {temp_c, condition, feels_c} for the city, or None on any failure."""
    city = city or CITY
    try:
        r = httpx.get(f"https://wttr.in/{city}", params={"format": "j1"},
                      timeout=timeout, headers={"User-Agent": "curl/8"})
        if r.status_code != 200:
            log.info("wttr.in %s for %s", r.status_code, city)
            return None
        cur = r.json()["current_condition"][0]
        return {
            "temp_c": int(cur["temp_C"]),
            "feels_c": int(cur["FeelsLikeC"]),
            "condition": cur["weatherDesc"][0]["value"].strip(),
            "city": city,
        }
    except Exception as e:
        log.info("weather unavailable for %s: %s", city, e)
        return None


def short_phrase(city: str | None = None) -> str | None:
    """A spoken clause like 'Bengaluru's sitting at 24 degrees and clear', or None."""
    w = get_weather(city)
    if not w:
        return None
    return f"{w['city']}'s sitting at {w['temp_c']} degrees and {w['condition'].lower()}"
