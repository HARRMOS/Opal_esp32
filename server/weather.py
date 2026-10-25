"""Météo du jour pour l'animation des écrans (Open-Meteo : gratuit, sans clé).

Lieu : OPAL_WEATHER_LAT / OPAL_WEATHER_LON / OPAL_WEATHER_PLACE (défaut Paris).
"""

from __future__ import annotations

import os
import time
import unicodedata
from typing import Any

import httpx

LAT = float(os.environ.get("OPAL_WEATHER_LAT", "48.8566"))
LON = float(os.environ.get("OPAL_WEATHER_LON", "2.3522"))
PLACE = os.environ.get("OPAL_WEATHER_PLACE", "Paris")
CACHE_S = 15 * 60

# Codes météo WMO → (icône des écrans, description courte)
_CODES = [
    ((0,), "sun", "Ensoleille"),
    ((1, 2), "cloud", "Eclaircies"),
    ((3,), "cloud", "Couvert"),
    ((45, 48), "fog", "Brouillard"),
    ((51, 53, 55, 56, 57), "rain", "Bruine"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "rain", "Pluie"),
    ((71, 73, 75, 77, 85, 86), "snow", "Neige"),
    ((95, 96, 99), "storm", "Orage"),
]

_cache: tuple[float, dict[str, Any]] | None = None


def ascii_text(text: str) -> str:
    """Les polices des écrans n'ont pas d'accents : « Été » → « Ete »."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


async def today() -> dict[str, Any]:
    """Champs de l'animation « meteo » : temp, min, max, icon, desc, place."""
    global _cache
    if _cache and time.time() - _cache[0] < CACHE_S:
        return _cache[1]
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LAT, "longitude": LON, "timezone": "Europe/Paris", "forecast_days": 1,
                "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_min,temperature_2m_max",
            },
        )
    response.raise_for_status()
    data = response.json()
    code = int(data["current"]["weather_code"])
    icon, desc = next(((i, d) for codes, i, d in _CODES if code in codes), ("cloud", "Variable"))
    info = {
        "temp": round(data["current"]["temperature_2m"]),
        "min": round(data["daily"]["temperature_2m_min"][0]),
        "max": round(data["daily"]["temperature_2m_max"][0]),
        "icon": icon,
        "desc": desc,
        "place": ascii_text(PLACE)[:19],
    }
    _cache = (time.time(), info)
    return info
