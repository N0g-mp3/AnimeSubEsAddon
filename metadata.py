"""
AnimeSubES — Metadata v2.5
==========================
Fuente exclusiva de sinopsis y títulos de episodios: TMDB.

Caché en DOS niveles:
  1. Memoria (dict _MEM): instantáneo, dura mientras el addon está vivo.
  2. Disco (tmdb_cache.json): persiste entre sesiones, TTL = 7 días.

Lectura de API key: ADDON.getSetting() (compatible Kodi 19/20/21).

Numeración acumulativa:
  AnimeFLV usa numeración continua (ep 1 … N) mientras TMDB divide
  en temporadas. Se acumulan todas las temporadas del show y se
  construye un mapa {num_acumulativo → datos_episodio}:
    Temporada 1 (20 eps)  → claves "1"…"20"
    Temporada 2 (21 eps)  → claves "21"…"41"
  Esto funciona para series largas como Bleach, Naruto, One Piece, etc.

NOTAS PARA ACTUALIZACIÓN FUTURA:
  - TTL_SECONDS puede aumentarse para series en emisión que cambian poco,
    o reducirse para series en curso que agregan episodios frecuentemente.
  - Para soportar búsqueda de películas de anime, agregar _tmdb_search_movie()
    y un parámetro mediatype="movie" en get_show_metadata().
  - Si TMDB depreca v3, actualizar TMDB_BASE a v4 y ajustar headers de auth.
  - La clave pública _DEFAULT_KEY es compartida. Si TMDB la revoca, el usuario
    debe ingresar su propia clave gratuita en los ajustes del addon.
"""
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request

try:
    import xbmc
    import xbmcaddon
    import xbmcvfs
    def _log(msg): xbmc.log(f"[AnimeSubES][meta] {msg}", xbmc.LOGINFO)
    def _err(msg): xbmc.log(f"[AnimeSubES][meta] ERR: {msg}", xbmc.LOGERROR)
    _ADDON = xbmcaddon.Addon()
    _CACHE_FILE = os.path.join(
        xbmcvfs.translatePath(_ADDON.getAddonInfo("profile")),
        "tmdb_cache.json",
    )
except ImportError:
    def _log(msg): pass
    def _err(msg): pass
    _ADDON = None
    _CACHE_FILE = "/tmp/tmdb_cache.json"

TMDB_BASE    = "https://api.themoviedb.org/3"
TMDB_IMG_W   = "https://image.tmdb.org/t/p/w400"
_DEFAULT_KEY = "a3b51918b1fc7859bcc394873734473b"
TTL_SECONDS  = 7 * 24 * 3600   # 7 días

# Caché en memoria: título → bundle
_MEM: dict = {}


# ── SSL ───────────────────────────────────────────────────────────────────────

def _ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get_json(url):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "AnimeSubES/2.5",
            "Accept":     "application/json",
        })
        with urllib.request.urlopen(req, timeout=10, context=_ssl()) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        _err(f"GET {url[:80]} → {e}")
        return None


# ── Caché en disco ─────────────────────────────────────────────────────────────

def _disk_load():
    """Carga el caché completo desde disco. Devuelve dict vacío si falla."""
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _disk_save(cache):
    """Guarda el caché completo en disco."""
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        _err(f"disk_save: {e}")


def _disk_get(title):
    """
    Devuelve el bundle cacheado en disco para este título si existe y no expiró.
    Retorna None si no existe o está expirado.
    """
    cache = _disk_load()
    entry = cache.get(title)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > TTL_SECONDS:
        return None
    return entry.get("bundle")


def _disk_set(title, bundle):
    """Guarda el bundle en el caché de disco con timestamp."""
    cache = _disk_load()
    cache[title] = {"bundle": bundle, "ts": int(time.time())}
    _disk_save(cache)


# ── API key ───────────────────────────────────────────────────────────────────

def _api_key():
    """
    Lee la clave TMDB. Usa getSetting() (compatible Kodi 19/20/21).
    Devuelve la clave por defecto si el addon no tiene settings definidos.
    """
    try:
        if _ADDON:
            k = _ADDON.getSetting("tmdb_api_key")
            if k and k.strip():
                return k.strip()
    except Exception:
        pass
    return _DEFAULT_KEY


# ── TMDB búsqueda y temporadas ────────────────────────────────────────────────

def _clean(title):
    t = re.sub(r"\(\d{4}\)\s*$", "", title)
    t = re.sub(r"\s*(TV|Movie|OVA|ONA|Special|Sub Español)\s*$", "", t, flags=re.I)
    return t.strip()


def _tmdb_search(title, key):
    """Devuelve el primer resultado de búsqueda de series TV en TMDB."""
    q = urllib.parse.quote(_clean(title))
    # Intento en español
    data = _get_json(f"{TMDB_BASE}/search/tv?api_key={key}&language=es-ES&query={q}")
    if data and data.get("results"):
        r = data["results"][0]
        # Si sinopsis española está vacía, obtener en inglés
        if not r.get("overview"):
            data_en = _get_json(f"{TMDB_BASE}/search/tv?api_key={key}&query={q}")
            if data_en and data_en.get("results"):
                r["overview"] = data_en["results"][0].get("overview", "")
        return r
    # Fallback en inglés
    data = _get_json(f"{TMDB_BASE}/search/tv?api_key={key}&query={q}")
    if data and data.get("results"):
        return data["results"][0]
    return None


def _tmdb_seasons_list(tv_id, key):
    """
    Devuelve lista de (season_number, episode_count) para todas las
    temporadas regulares (excluye temporada 0 = especiales).
    """
    data = _get_json(f"{TMDB_BASE}/tv/{tv_id}?api_key={key}&language=es-ES")
    if not data:
        return []
    result = []
    for s in data.get("seasons", []):
        sn = s.get("season_number", 0)
        ec = s.get("episode_count", 0)
        if sn > 0 and ec > 0:
            result.append((sn, ec))
    return result


def _tmdb_season(tv_id, key, season_num):
    """
    Descarga una temporada de TMDB.
    Devuelve {ep_num_str → {name, overview, still_path}}.
    Si sinopsis española está mayormente vacía, reintenta en inglés.
    """
    data = _get_json(
        f"{TMDB_BASE}/tv/{tv_id}/season/{season_num}"
        f"?api_key={key}&language=es-ES")
    if not data or not data.get("episodes"):
        return {}

    eps = {}
    for ep in data["episodes"]:
        n = str(ep.get("episode_number", ""))
        if n:
            eps[n] = {
                "name":     ep.get("name") or "",
                "overview": ep.get("overview") or "",
                "still":    ep.get("still_path") or "",
            }

    # Si más de la mitad están vacíos en español, reintentar en inglés
    filled = sum(1 for v in eps.values() if v["overview"])
    if eps and filled < len(eps) // 2:
        data_en = _get_json(
            f"{TMDB_BASE}/tv/{tv_id}/season/{season_num}?api_key={key}")
        if data_en and data_en.get("episodes"):
            for ep in data_en["episodes"]:
                n = str(ep.get("episode_number", ""))
                if n in eps and not eps[n]["overview"]:
                    eps[n]["overview"] = ep.get("overview") or ""

    return eps


def _build_episode_map(tv_id, key, seasons_list):
    """
    Recorre todas las temporadas y construye un mapa con numeración
    ACUMULATIVA compatible con la numeración continua de AnimeFLV.

    Ejemplo:
      T1: 20 eps → claves "1"…"20"
      T2: 21 eps → claves "21"…"41"

    También guarda la clave directa (dentro de la temporada) como alias.

    NOTA FUTURA: Si AnimeFLV cambia a numeración por temporada, eliminar
    la numeración acumulativa y usar solo la clave directa TMDB.
    """
    result     = {}
    cumulative = 0

    for season_num, _ in seasons_list:
        season_eps = _tmdb_season(tv_id, key, season_num)
        if not season_eps:
            continue

        max_ep = max((int(n) for n in season_eps if n.isdigit()), default=0)

        for ep_num_str, ep_data in season_eps.items():
            try:
                n       = int(ep_num_str)
                acc_key = str(cumulative + n)
                if acc_key not in result:
                    result[acc_key] = ep_data
                # Alias directo (no sobrescribe acumulativo)
                if ep_num_str not in result:
                    result[ep_num_str] = ep_data
            except ValueError:
                pass

        cumulative += max_ep
        _log(f"TMDB T{season_num}: {len(season_eps)} eps "
             f"(acumulado hasta ep {cumulative})")

    return result


# ── API pública ───────────────────────────────────────────────────────────────

def get_show_metadata(anime_title):
    """
    Carga y devuelve el bundle de metadata para un anime.

    Orden de caché:
      1. Memoria (_MEM) → instantáneo
      2. Disco (_CACHE_FILE, TTL 7 días) → sin llamadas HTTP
      3. TMDB API → guarda en disco y memoria

    Bundle devuelto:
        {
          "overview":  str,
          "episodes":  { "293": {"name": "…", "overview": "…", "still": "/path.jpg"}, … }
        }
    """
    # 1. Caché en memoria
    if anime_title in _MEM:
        return _MEM[anime_title]

    # 2. Caché en disco
    cached = _disk_get(anime_title)
    if cached is not None:
        _MEM[anime_title] = cached
        _log(f"TMDB: '{anime_title}' desde caché disco")
        return cached

    # 3. Llamada a la API
    bundle = {"overview": "", "episodes": {}}
    key    = _api_key()

    if not key:
        _MEM[anime_title] = bundle
        return bundle

    show = _tmdb_search(anime_title, key)
    if not show:
        _log(f"TMDB: sin resultado para '{anime_title}'")
        _MEM[anime_title] = bundle
        return bundle

    tv_id              = show.get("id")
    bundle["overview"] = show.get("overview") or ""
    _log(f"TMDB: '{anime_title}' → id={tv_id}")

    seasons = _tmdb_seasons_list(tv_id, key)
    _log(f"TMDB: {len(seasons)} temporadas para id={tv_id}")

    bundle["episodes"] = _build_episode_map(tv_id, key, seasons)
    _log(f"TMDB: {len(bundle['episodes'])} entradas en mapa de episodios")

    # Guardar en ambos niveles de caché
    _disk_set(anime_title, bundle)
    _MEM[anime_title] = bundle
    return bundle


def episode_meta(bundle, ep_num, fallback_cover=""):
    """
    Devuelve los metadatos de un episodio concreto.

    Retorna:
        {
          "cover":    str,   # URL de imagen (TMDB still o fallback)
          "synopsis": str,   # sinopsis del episodio (vacía si no hay)
          "title":    str|None
        }
    """
    try:
        num = str(int(float(ep_num)))
    except Exception:
        num = str(ep_num)

    ep_data  = (bundle or {}).get("episodes", {}).get(num) or {}
    synopsis = ep_data.get("overview") or ""
    title    = ep_data.get("name") or None
    still    = ep_data.get("still") or ""
    cover    = (TMDB_IMG_W + still) if still else fallback_cover

    return {"cover": cover, "synopsis": synopsis, "title": title}
