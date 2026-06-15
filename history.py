"""
AnimeSubES — Historial v2.5 (por serie, estilo Otaku)
======================================================
Guarda el ÚLTIMO episodio visto de cada serie, no episodios individuales.
Estructura del archivo: dict keyed por "slug|source".

Así funciona:
  - add()        → registra/actualiza el último ep visto de una serie
  - get_recent() → devuelve lista de series ordenadas por timestamp
  - get_last_ep()→ devuelve el nº del último ep visto para una serie
  - remove()     → elimina una serie del historial
  - clear()      → borra todo

NOTAS PARA ACTUALIZACIÓN FUTURA:
  - Si se agrega soporte multi-fuente por serie, ampliar la clave
    a "slug|source|language" para separar dub/sub.
  - Para sincronización en la nube, exponer _load/_save como
    backend intercambiable (local / Kodi sync / REST).
  - MAX_SERIES limita cuántas series se guardan. Subir si se
    quiere un historial más largo.
"""
import json
import os
import time

import xbmcaddon
import xbmcvfs

ADDON      = xbmcaddon.Addon()
MAX_SERIES = 100

_FILE = os.path.join(
    xbmcvfs.translatePath(ADDON.getAddonInfo("profile")),
    "watch_history.json",
)


# ── I/O ───────────────────────────────────────────────────────────────────────

def _load():
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    with open(_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _key(slug, source):
    return f"{slug}|{source}"


# ── API pública ───────────────────────────────────────────────────────────────

def add(slug, ep, source, anime_title, cover="", total=None):
    """
    Registra o actualiza el último episodio visto de una serie.
    Llama a esto cada vez que el usuario inicia la reproducción de un episodio.
    """
    data = _load()
    k = _key(slug, source)

    data[k] = {
        "slug":        slug,
        "source":      source,
        "ep":          str(ep),
        "anime_title": anime_title,
        "cover":       cover,
        "total":       total,
        "ts":          int(time.time()),
    }

    # Podar series más antiguas si se supera el límite
    if len(data) > MAX_SERIES:
        sorted_keys = sorted(data, key=lambda x: data[x].get("ts", 0))
        for old in sorted_keys[: len(data) - MAX_SERIES]:
            data.pop(old, None)

    _save(data)


def get_recent(limit=50):
    """
    Devuelve lista de series ordenadas por timestamp descendente (más reciente primero).
    Cada entrada es el dict completo de la serie con su último episodio visto.
    """
    data = _load()
    entries = sorted(data.values(), key=lambda x: x.get("ts", 0), reverse=True)
    return entries[:limit]


def get_last_ep(slug, source):
    """
    Devuelve el número de episodio (str) del último visto para esta serie.
    Retorna None si no hay historial para ella.
    """
    data = _load()
    entry = data.get(_key(slug, source))
    return entry["ep"] if entry else None


def remove(slug, source):
    """Elimina una serie del historial (para opción en menú contextual)."""
    data = _load()
    data.pop(_key(slug, source), None)
    _save(data)


def clear():
    """Borra todo el historial."""
    _save({})
