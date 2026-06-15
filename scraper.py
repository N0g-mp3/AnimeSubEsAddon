"""
AnimeSubES — Scraper v2.3
==========================
Fuentes : AnimeFLV + JKAnime (fallback automático entre ellas)
Resolvers: streamwish, streamtape, doodstream, voe, vidguard,
           mp4upload, yourupload, netu/netuplayer, ok.ru,
           maru, fembed/femax, embedsito + genérico
"""
import gzip
import json
import re
import ssl
import urllib.parse
import urllib.request

try:
    import xbmc
    def _log(msg): xbmc.log(f"[AnimeSubES] {msg}", xbmc.LOGINFO)
    def _err(msg): xbmc.log(f"[AnimeSubES] ERR: {msg}", xbmc.LOGERROR)
except ImportError:
    def _log(msg): pass
    def _err(msg): pass


# ── Constantes ─────────────────────────────────────────────────────────────────

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")

_BASE_HDR = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection":      "keep-alive",
}

AFL_BASE        = "https://www3.animeflv.net"
JKA_BASE        = "https://jkanime.net"
TIMEOUT         = 12   # timeout general por petición HTTP
TIMEOUT_FAST    = 3   # 3s: suficiente para 403/522 (<1s); ahorra tiempo en servers muertos    # timeout para intentos en bucle multi-dominio
                       # 4s es suficiente: 522 y 403 fallan en <1s, DNS en <0.1s.
                       # Si se sube este valor, cada dominio muerto añade ese tiempo.

# Servidores ignorados por no ser accesibles vía plugin
_SKIP    = {"mega", "mediafire"}

# Orden de preferencia de servidores (menor índice = mayor prioridad)
# NOTA FUTURA: reordenar según disponibilidad real; los primeros se intentan antes.
# Si un servidor tiene mucho lag, subirlo en la lista puede perjudicar la experiencia.
# Orden de preferencia 2025-2026 optimizado para bajo consumo hasta 720p:
# 1. Filemoon: HLS estable, buena disponibilidad en AnimeFLV.
# 2. StreamTape: MP4 directo → demuxer interno Kodi (menos CPU que HLS adaptive).
# 3. VOE: HLS estable.
# 4. OK.ru: buena disponibilidad.
# 5. Streamwish y variantes: funciona cuando el CDN no está saturado.
# 6. FileLions, Speedfiles, DoodStream, etc.
# 7. Netu: último recurso (token IP, SSL problemático).
_ORDER   = [
    # Filemoon y variantes — HLS estable, muy común en AnimeFLV 2025-2026
    "filemoon", "moonplayer", "moonmovies",
    # StreamTape — MP4 directo, bajo consumo CPU (sin adaptive bitrate)
    "streamtape", "stape",
    # VOE — HLS estable
    "voe.sx", "voe",
    # OK.ru — buena disponibilidad
    "ok.ru", "okru",
    # Streamwish y variantes
    "streamwish", "sw", "awish", "strwish", "hlswish",
    # FileLions
    "filelions",
    # Speedfiles
    "speedfiles",
    # DoodStream
    "doodstream", "dood",
    # YourupLoad
    "yourupload",
    # VidGuard
    "vidguard", "listeamed", "bembed", "vgfplay",
    # MP4Upload
    "mp4upload",
    # Otros
    "maru",
    "fembed", "femax",
    "embedsito",
    # Netu/NetuPlayer — último recurso: token atado a IP, SSL problemático
    "netu", "netuplayer",
]

# URLs que indican "sin vídeo" en el servidor
_INVALID = (
    "novideo", "no_video", "nourl", "noembed", "notfound",
    "placeholder", "/embed/novideo", "video_not_found",
)


# ── SSL ───────────────────────────────────────────────────────────────────────

def _ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get(url, referer=None, extra=None):
    h = dict(_BASE_HDR)
    if referer: h["Referer"] = referer
    if extra:   h.update(extra)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl()) as r:
            raw = r.read()
            if r.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="ignore")
    except Exception as e:
        _err(f"GET {url[:70]} → {e}")
        return None


def _post(url, data, referer=None):
    h = dict(_BASE_HDR)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    if referer: h["Referer"] = referer
    enc = urllib.parse.urlencode(data).encode()
    try:
        req = urllib.request.Request(url, data=enc, headers=h)
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl()) as r:
            raw = r.read()
            if r.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="ignore")
    except Exception as e:
        _err(f"POST {url[:70]} → {e}")
        return None


def _strip_tags(s):
    return re.sub(r"<[^>]+>", "", s).strip()


# ── Validación de URLs ────────────────────────────────────────────────────────

def _ok(url):
    """True si la URL es absoluta, tiene un host con pinta de dominio real
    y no es un placeholder.

    Esto evita que un regex que solo capturó un fragmento (p.ej. por un
    string partido en varios trozos con comillas dobles/simples mezcladas)
    se cuele como "URL válida" y termine en Kodi como
    'https://strea' → CCurlFile no puede resolver el host."""
    if not url or not url.startswith("http"):
        return False
    lo = url.lower()
    if any(b in lo for b in _INVALID):
        return False
    try:
        netloc = urllib.parse.urlparse(url).netloc
    except Exception:
        return False
    # El host debe parecer un dominio real: "algo.tld" y un mínimo de longitud
    return "." in netloc and len(netloc) >= 4


def _priority(url_or_name):
    lo = url_or_name.lower()
    if any(s in lo for s in _SKIP):
        return 999
    for i, k in enumerate(_ORDER):
        if k in lo:
            return i
    return len(_ORDER)


# ── AnimeFLV ──────────────────────────────────────────────────────────────────

def afl_search(q):
    html = _get(f"{AFL_BASE}/browse?q={urllib.parse.quote(q)}")
    if not html:
        return []
    out = []
    for blk in re.findall(
            r'<article[^>]+class="[^"]*Anime[^"]*"[^>]*>(.*?)</article>', html, re.S):
        sm = re.search(r'href="/anime/([^"]+)"', blk)
        tm = re.search(r'<h3[^>]*>(.*?)</h3>', blk, re.S)
        if not (sm and tm):
            continue
        cm = re.search(r'src="(/uploads/[^"]+)"', blk)
        cover = (AFL_BASE + cm.group(1)) if cm else ""
        out.append({"title":  _strip_tags(tm.group(1)),
                    "slug":   sm.group(1).strip("/"),
                    "cover":  cover,
                    "source": "animeflv"})
    return out


def afl_episodes(slug):
    html = _get(f"{AFL_BASE}/anime/{slug}")
    if not html:
        return None
    tm = re.search(r'<h1[^>]+class="[^"]*Title[^"]*"[^>]*>(.*?)</h1>', html, re.S)
    title = _strip_tags(tm.group(1)) if tm else slug
    cm = re.search(
        r'<div[^>]+class="[^"]*AnimeCover[^"]*"[^>]*>.*?<img[^>]+src="([^"]+)"',
        html, re.S)
    cover = cm.group(1) if cm else ""
    if cover.startswith("/"): cover = AFL_BASE + cover

    eps = []
    # Método 1: variable JS `episodes`
    # AnimeFLV devuelve el array en orden DESCENDENTE (último primero).
    # Siempre ordenar ascendente para que la playlist vaya de ep 1 en adelante.
    em = re.search(r'var episodes\s*=\s*(\[.*?\]);', html, re.S)
    if em:
        try:
            for ep in json.loads(em.group(1)):
                eps.append({"num": str(ep[0]), "title": f"Episodio {ep[0]}", "cover": cover})
            eps.sort(key=lambda x: float(x["num"]))   # ← orden ascendente
        except Exception:
            pass
    # Método 2: hrefs
    if not eps:
        seen = set()
        for m in re.finditer(r'href="/ver/[^/]+-(\d+)"', html):
            n = m.group(1)
            if n not in seen:
                seen.add(n)
                eps.append({"num": n, "title": f"Episodio {n}", "cover": cover})
        eps.sort(key=lambda x: float(x["num"]))

    return {"title": title, "cover": cover, "episodes": eps}


def afl_stream(slug, ep_num):
    # El slug del anime puede ya tener el sufijo de episodio → quitarlo
    base_slug = re.sub(r'-\d+$', '', slug) if re.search(r'-\d+$', slug) else slug
    page      = f"{AFL_BASE}/ver/{base_slug}-{ep_num}"
    _log(f"afl_stream → {page}")

    html = _get(page, referer=f"{AFL_BASE}/anime/{base_slug}")
    if not html:
        return None

    raw_js = None
    for var in ("videos", "videosJSPath", "episodeServers"):
        m = re.search(rf'var {re.escape(var)}\s*=\s*(\{{.*?\}}|\[.*?\]);',
                      html, re.S)
        if m:
            raw_js = m.group(1)
            break

    if not raw_js:
        _err(f"afl: no encontré var videos en {page}")
        return None

    try:
        raw  = json.loads(raw_js)
        srvs = raw.get("SUB", raw) if isinstance(raw, dict) else raw
    except Exception as e:
        _err(f"afl JSON: {e}")
        return None

    embeds = []
    for srv in srvs:
        name = (srv.get("title") or srv.get("server") or "").lower()
        code = srv.get("code") or srv.get("url") or ""
        if not code:
            continue
        if code.startswith("//"): code = "https:" + code
        if any(s in name or s in code.lower() for s in _SKIP):
            continue
        embeds.append((code, name))

    embeds.sort(key=lambda x: min(_priority(x[0]), _priority(x[1])))
    _log(f"afl: {len(embeds)} embeds {[e[1] for e in embeds]}")

    for embed, name in embeds:
        _log(f"  intentando '{name}'")
        url = _resolve(embed, referer=page)
        if url:
            _log(f"  ✓ '{name}' → {url[:70]}")
            return url, embed
        _log(f"  ✗ '{name}'")

    return None


# ── JKAnime ───────────────────────────────────────────────────────────────────

def jka_search(q):
    html = _get(f"{JKA_BASE}/buscar/{urllib.parse.quote(q)}/")
    if not html:
        return []
    out = []
    for blk in re.findall(
            r'<div[^>]+class="[^"]*anime__item[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html, re.S):
        sm = re.search(rf'{re.escape(JKA_BASE)}/([^/"]+)/', blk)
        tm = re.search(r'<h5[^>]*>(.*?)</h5>', blk, re.S)
        if not (sm and tm):
            continue
        cm = re.search(r'(?:src|data-src)="([^"]+\.(?:jpg|png|webp)[^"]*)"', blk, re.I)
        out.append({"title":  _strip_tags(tm.group(1)),
                    "slug":   sm.group(1).strip("/"),
                    "cover":  cm.group(1) if cm else "",
                    "source": "jkanime"})
    return out


def jka_episodes(slug):
    html = _get(f"{JKA_BASE}/{slug}/")
    if not html:
        return None
    tm    = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    title = _strip_tags(tm.group(1)) if tm else slug
    cm    = (re.search(r'data-setbg="([^"]+)"', html) or
             re.search(r'<img[^>]+src="([^"]+\.(?:jpg|png|webp)[^"]*)"', html, re.I))
    cover = cm.group(1) if cm else ""
    nm    = re.search(r'<span[^>]*>\s*(\d+)\s*</span>\s*episodios', html, re.I)
    if nm:
        n   = int(nm.group(1))
        eps = [{"num": str(i), "title": f"Episodio {i}", "cover": cover}
               for i in range(1, n + 1)]
    else:
        seen = set()
        eps  = []
        for m in re.finditer(
                rf'{re.escape(JKA_BASE)}/{re.escape(slug)}/(\d+)/', html):
            n = m.group(1)
            if n not in seen:
                seen.add(n)
                eps.append({"num": n, "title": f"Episodio {n}", "cover": cover})
        eps.sort(key=lambda x: int(x["num"]))
    return {"title": title, "cover": cover, "episodes": eps}


def jka_stream(slug, ep_num):
    page = f"{JKA_BASE}/{slug}/{ep_num}/"
    _log(f"jka_stream → {page}")
    html = _get(page, referer=f"{JKA_BASE}/{slug}/")
    if not html:
        return None

    embeds = []
    for pat in [
        r'(https?://(?:streamwish|awish|strwish)[^\s"\'<>]+)',
        r'(https?://(?:dood|doodstream)[^\s"\'<>]+)',
        r'(https?://voe\.sx/[^\s"\'<>]+)',
        r'(https?://streamtape\.com/[^\s"\'<>]+)',
        r'(https?://ok\.ru/videoembed/[^\s"\'<>]+)',
        r'(https?://www\.yourupload\.com/embed/[^\s"\'<>]+)',
        r'(https?://(?:vidguard|listeamed|bembed|vgfplay)[^\s"\'<>]+)',
        r'(https?://mp4upload\.com/embed-[^\s"\'<>]+)',
        r'(https?://(?:netu|netuplayer)[^\s"\'<>]+)',
        r'(https?://(?:fembed|femax)[^\s"\'<>]+)',
        r'(https?://embedsito\.com/v/[^\s"\'<>]+)',
    ]:
        for m in re.finditer(pat, html, re.I):
            embeds.append(m.group(1))

    if not embeds:
        for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html):
            e = m.group(1)
            if e.startswith("//"): e = "https:" + e
            embeds.append(e)

    embeds = [e for e in embeds if not any(s in e.lower() for s in _SKIP)]
    embeds.sort(key=_priority)
    _log(f"jka: {len(embeds)} embeds")

    for embed in embeds:
        url = _resolve(embed, referer=page)
        if url:
            _log(f"  jka ✓ → {url[:70]}")
            return url, embed
    return None


# ── Resolvers ─────────────────────────────────────────────────────────────────

def _resolve(url, referer=None):
    u = url.lower()
    # StreamWish y todas sus variantes de dominio
    if any(k in u for k in ("streamwish", "awish", "strwish", "wish07",
                             "wishembed", "hlswish", "swdyu")):
        return _r_streamwish(url)
    # FileLions — usa la misma API que StreamWish
    if any(k in u for k in ("filelions", "lionscdn")):
        return _r_filelions(url)
    # Filemoon y variantes
    if any(k in u for k in ("filemoon", "moonplayer", "moonmovies", "filemon")):
        return _r_filemoon(url)
    # Speedfiles
    if "speedfiles" in u:
        return _r_speedfiles(url)
    if "streamtape" in u:
        return _r_streamtape(url)
    if "dood" in u:
        return _r_doodstream(url)
    if any(k in u for k in ("voe.sx", "voe")):
        return _r_voe(url)
    if any(k in u for k in ("vidguard", "listeamed", "bembed", "vgfplay")):
        return _r_vidguard(url)
    if "mp4upload" in u:
        return _r_mp4upload(url)
    if "yourupload" in u:
        return _r_yourupload(url, referer)
    if any(k in u for k in ("netu", "netuplayer")):
        return _r_netu(url, referer)
    if "ok.ru" in u:
        return _r_okru(url)
    if any(k in u for k in ("fembed", "femax")):
        return _r_fembed(url)
    if "embedsito" in u:
        return _r_embedsito(url, referer)
    return _r_generic(url, referer)


# ─── Helpers compartidos para extractores HLS/MP4 ────────────────────────────

def _extract_hls_mp4(html):
    """
    Intenta extraer una URL de stream (m3u8 o mp4) de un HTML.
    Primero intenta patrones directos; si no encuentra, intenta decodificar
    bloques eval(atob(...)) o eval(function(p,a,c,k,e,d){...}) que usan
    muchos players para ofuscar la URL del stream.

    NOTA FUTURA: Si aparecen nuevos métodos de ofuscación (ej. hexadecimal,
    XOR), añadir su decodificación aquí para que todos los resolvers
    se beneficien automáticamente.
    """
    _DIRECT = [
        r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'"file"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r"'hls'\s*:\s*'([^']+\.m3u8[^']*)'",
        r'"hls"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r'sources\s*:\s*\[.*?["\']?file["\']?\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
        r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
        r'"src"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)',
    ]
    for pat in _DIRECT:
        m = re.search(pat, html, re.S)
        if m and _ok(m.group(1)):
            return m.group(1)

    # Intentar decodificar eval(atob("base64..."))
    for b64 in re.findall(r'atob\(["\']([A-Za-z0-9+/=]{20,})["\']', html):
        try:
            import base64 as _b64
            decoded = _b64.b64decode(b64).decode("utf-8", errors="ignore")
            for pat in _DIRECT:
                m = re.search(pat, decoded, re.S)
                if m and _ok(m.group(1)):
                    return m.group(1)
        except Exception:
            pass

    # Intentar decodificar eval(function(p,a,c,k,e,d) — packed JS
    pack_m = re.search(r"eval\(function\(p,a,c,k,e,(?:r|d)\).*?'([^']{50,})'\.split", html, re.S)
    if pack_m:
        try:
            words = pack_m.group(1).split("|")
            unpacked = re.sub(
                r'\b(\w+)\b',
                lambda mo: words[int(mo.group(0), 36)] if mo.group(0).isdigit()
                           or (len(mo.group(0)) == 1 and mo.group(0).isalpha())
                           else mo.group(0),
                html,
            )
            for pat in _DIRECT:
                m = re.search(pat, unpacked, re.S)
                if m and _ok(m.group(1)):
                    return m.group(1)
        except Exception:
            pass

    return None


# ─── StreamWish ───────────────────────────────────────────────────────────────
# Dominios activos verificados 2025. Ordenados por fiabilidad.
# NOTA FUTURA: cuando un dominio empiece a devolver 522/403 consistentemente,
# moverlo al final de la lista o eliminarlo. Agregar nuevos dominios al principio.
_SW_DOMAINS = [
    "streamwish.com",    # principal — más estable
    "streamwish.to",     # alternativa (a veces 522 por carga)
    "hlswish.com",       # variante activa 2025
    "filelions.online",  # variante activa 2025
    "awish.one",
    "strwish.com",
]


def _r_streamwish(url):
    # Extraer el ID del video de la URL
    m = re.search(r'/(?:e|v|f)/([a-zA-Z0-9]+)', url)
    vid = m.group(1) if m else url.rstrip("/").split("/")[-1].split("?")[0]

    # 1. Intentar la API JSON de cada dominio (timeout corto para no bloquear)
    for dom in _SW_DOMAINS:
        try:
            req = urllib.request.Request(
                f"https://{dom}/api/source/{vid}",
                data=urllib.parse.urlencode({"r": "", "d": dom}).encode(),
                headers={**_BASE_HDR,
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": url},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_FAST, context=_ssl()) as r:
                raw = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                raw = raw.decode("utf-8", errors="ignore")
            if raw and not raw.lstrip().startswith("<"):
                f = _best(json.loads(raw).get("data", []))
                if f:
                    _log(f"  streamwish API OK ({dom})")
                    return f
        except Exception as e:
            _err(f"  SW {dom}: {e}")

    # 2. Fallback HTML — solo los 2 primeros dominios para no desperdiciar tiempo.
    # Si la API falla en todos los dominios, el HTML tampoco suele funcionar en los
    # mismos dominios bloqueados; intentar más de 2 solo añade latencia.
    # NOTA FUTURA: si un dominio nuevo es muy fiable, ponerlo al inicio de _SW_DOMAINS.
    for dom in _SW_DOMAINS[:2]:
        embed = f"https://{dom}/e/{vid}"
        html  = _get(embed, referer=url)
        if not html:
            continue
        found = _extract_hls_mp4(html)
        if found:
            _log(f"  streamwish HTML OK ({dom})")
            return found

    # 3. Último recurso: URL original tal como llegó
    html = _get(url, referer=url)
    if html:
        return _extract_hls_mp4(html)

    return None


# ─── FileLions ────────────────────────────────────────────────────────────────
# Usa la misma API que StreamWish pero con dominios propios.
# NOTA FUTURA: FileLions puede cambiar de API endpoint; verificar si
# /api/source/ deja de funcionar y probar /player/index.php?data= como alternativa.
_FL_DOMAINS = [
    "filelions.com",
    "filelions.to",
    "filelions.online",
    "lionscdn.com",
]


def _r_filelions(url):
    m = re.search(r'/(?:e|v|f)/([a-zA-Z0-9]+)', url)
    vid = m.group(1) if m else url.rstrip("/").split("/")[-1].split("?")[0]

    for dom in _FL_DOMAINS:
        try:
            req = urllib.request.Request(
                f"https://{dom}/api/source/{vid}",
                data=urllib.parse.urlencode({"r": "", "d": dom}).encode(),
                headers={**_BASE_HDR,
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": url},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_FAST, context=_ssl()) as r:
                raw = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                raw = raw.decode("utf-8", errors="ignore")
            if raw and not raw.lstrip().startswith("<"):
                f = _best(json.loads(raw).get("data", []))
                if f:
                    return f
        except Exception as e:
            _err(f"  FileLions {dom}: {e}")

    # Fallback HTML
    html = _get(url, referer=url)
    return _extract_hls_mp4(html) if html else None


# ─── Filemoon ─────────────────────────────────────────────────────────────────
# Player JWPlayer con HLS. El HTML tiene las fuentes en jwplayer("vplayer").setup()
# o dentro de un eval(atob(...)).
# NOTA FUTURA: Filemoon cambia de dominio frecuentemente. Si todos fallan,
# agregar nuevos dominios aquí. Los dominios .in y .st son los más estables.
_FM_DOMAINS = [
    "filemoon.sx",
    "filemoon.in",
    "moonplayer.one",
    "filemoon.to",
    "moonmovies.in",
]


def _r_filemoon(url):
    m = re.search(r'/(?:e|v)/([a-zA-Z0-9]+)', url)
    vid = m.group(1) if m else url.rstrip("/").split("/")[-1].split("?")[0]

    for dom in _FM_DOMAINS:
        embed = f"https://{dom}/e/{vid}"
        html  = _get(embed, referer=url,
                     extra={"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate"})
        if not html:
            continue
        found = _extract_hls_mp4(html)
        if found:
            _log(f"  filemoon OK ({dom})")
            return found

    # Intentar también con la URL original
    html = _get(url, referer=url)
    return _extract_hls_mp4(html) if html else None


# ─── Speedfiles ───────────────────────────────────────────────────────────────
def _r_speedfiles(url):
    vid  = url.rstrip("/").split("/")[-1].split("?")[0]
    html = _get(url, referer=url)
    if not html:
        return None
    found = _extract_hls_mp4(html)
    if found:
        return found
    # Intentar API
    raw = _post(f"https://speedfiles.net/api/source/{vid}",
                {"r": "", "d": "speedfiles.net"}, referer=url)
    if raw and not raw.lstrip().startswith("<"):
        try:
            f = _best(json.loads(raw).get("data", []))
            if f and _ok(f):
                return f
        except Exception:
            pass
    return None


# ─── StreamTape ───────────────────────────────────────────────────────────────

def _r_streamtape(url):
    """
    StreamTape — lazy resolve.

    El token en /get_xcdvideo?...&token=X expira en ~30-60s.
    Si lo resolvemos aquí (en el scraper) y Kodi tarda en abrir
    el plugin URL (playlist lazy), el token ya venció → 404.

    Solución: devolver la embed URL tal cual (streamtape.com/e/ID).
    player.py la detecta por _LAZY_STAPE y resuelve el token fresco
    justo antes de setResolvedUrl.
    """
    # Normalizar a URL embed /e/ID
    m = re.search(r'streamtape\.com/(?:e|v)/([\w\-]+)', url)
    if m:
        embed = f"https://streamtape.com/e/{m.group(1)}/"
        _log(f"  streamtape lazy embed → {embed}")
        return embed
    # Si ya es una URL de descarga directa (raro), devolverla igual
    if "streamtape.com" in url and ("get_" in url or "?id=" in url):
        return url
    return None

# ─── DoodStream ───────────────────────────────────────────────────────────────

def _r_doodstream(url):
    html = _get(url, referer=url)
    if not html:
        return None
    m = re.search(r"(/pass_md5/[^\s'\"]+)", html)
    if not m:
        return None
    parts  = urllib.parse.urlparse(url)
    domain = f"{parts.scheme}://{parts.netloc}"
    raw    = _get(domain + m.group(1), referer=url)
    if not raw:
        return None
    tok_m  = re.search(r"token=([a-zA-Z0-9]+)", html)
    result = raw.strip() + (tok_m.group(1) if tok_m else "")
    return result if _ok(result) else None


# ─── VOE ──────────────────────────────────────────────────────────────────────

def _r_voe(url):
    html = _get(url, referer=url)
    if not html:
        return None
    for pat in [
        r"'hls'\s*:\s*'([^']+)'",
        r'"hls"\s*:\s*"([^"]+)"',
        r'sources\s*=\s*\[.*?src:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
    ]:
        m = re.search(pat, html, re.S)
        if m and _ok(m.group(1)):
            return m.group(1)
    return None


# ─── VidGuard ─────────────────────────────────────────────────────────────────

def _r_vidguard(url):
    html = _get(url, referer=url)
    if not html:
        return None
    for pat in [
        r'sources\s*:\s*\[.*?file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'"file"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
    ]:
        m = re.search(pat, html, re.S)
        if m and _ok(m.group(1)):
            return m.group(1)
    return None


# ─── MP4Upload ────────────────────────────────────────────────────────────────

def _r_mp4upload(url):
    html = _get(url, referer=url)
    if not html:
        return None
    for pat in [
        r'"src"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r'player\.src\s*\(\s*["\']?(https?://[^"\'<>\s]+\.mp4[^"\'<>\s]*)',
        r'src=["\']?(https?://[^"\'<>\s]+\.mp4[^"\'<>\s]*)',
    ]:
        m = re.search(pat, html)
        if m and _ok(m.group(1)):
            return m.group(1)
    return None


# ─── YourupLoad ───────────────────────────────────────────────────────────────

def _r_yourupload(url, referer=None):
    """
    Yourupload embed. El CDN vidcache.net requiere Referer = URL del embed
    (yourupload.com), NO la página origen (animflv). El embed_referer que
    devuelve el scraper ya es el URL del embed, que player.py usará como
    Referer al reproducir.
    """
    html = _get(url, referer=referer or url,
                extra={"Referer": url})   # forzar Referer al embed
    if not html:
        return None
    for pat in [
        r'(https?://[^\s"\'<>]+vidcache[^\s"\'<>]+)',
        r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
        r'"url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)',
        r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
    ]:
        m = re.search(pat, html)
        if m and _ok(m.group(1)):
            return m.group(1)
    return None


# ─── Netu / NetuPlayer ────────────────────────────────────────────────────────

def _netu_resolve_cdn(cdn_url, embed_url):
    """
    El endpoint CDN de netu (/secip/…) NO es el m3u8 directamente:
    puede devolver JSON con la URL real o redirigir a ella.
    Kodi no puede parsear JSON como m3u8, de ahí el "Error creating demuxer".

    Esta función hace una petición GET al CDN y extrae la URL del stream real:
      1. Si la respuesta empieza con #EXTM3U → el CDN devuelve m3u8 directo.
      2. Si la URL final (tras redirects) tiene .m3u8 → usar esa URL.
      3. Si el cuerpo contiene una URL .m3u8 → extraerla.
      4. Si el cuerpo contiene una URL CDN de stream → extraerla.
      5. En cualquier otro caso → devolver None (caller usará la URL original).

    NOTA FUTURA: si el CDN cambia formato de respuesta, añadir el nuevo
    patrón aquí. No tocar el resto de _r_netu().
    """
    h = dict(_BASE_HDR)
    h["Referer"] = embed_url
    h["Origin"]  = "https://" + urllib.parse.urlparse(embed_url).netloc
    try:
        req = urllib.request.Request(cdn_url, headers=h)
        with urllib.request.urlopen(req, timeout=TIMEOUT_FAST, context=_ssl()) as r:
            final_url    = r.url
            content_type = r.headers.get("Content-Type", "")
            body         = r.read(2048).decode("utf-8", errors="ignore")

        # Caso 1: respuesta directa m3u8
        if body.lstrip().startswith("#EXTM3U") or "mpegurl" in content_type.lower():
            _log(f"  netu CDN → m3u8 directo: {final_url[:60]}")
            return final_url

        # Caso 2: redirect a URL con .m3u8
        if ".m3u8" in final_url.lower() and final_url != cdn_url:
            _log(f"  netu CDN redirect → {final_url[:60]}")
            return final_url

        # Caso 3: JSON o HTML con URL .m3u8 dentro
        m = re.search(r'["\'](https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)["\']', body)
        if m and _ok(m.group(1)):
            _log(f"  netu CDN JSON m3u8 → {m.group(1)[:60]}")
            return m.group(1)

        # Caso 4: JSON o HTML con URL CDN de stream (sin extensión pero reconocible)
        m = re.search(r'["\'](https?://[^\s"\'<>]{40,}/(?:stream|hls|video|secip)/[^\s"\'<>]+)["\']', body)
        if m and _ok(m.group(1)) and m.group(1) != cdn_url:
            _log(f"  netu CDN JSON stream → {m.group(1)[:60]}")
            return m.group(1)

        _log(f"  netu CDN: no se pudo extraer stream del cuerpo (type={content_type[:30]})")
    except Exception as e:
        _err(f"  netu CDN resolve: {e}")
    return None


def _r_netu(url, referer=None):
    """
    Netu/netuplayer: devuelve la URL CDN tokenizada extraída del HTML del embed.

    CAMBIO v2.9.1: ya NO llamamos a _netu_resolve_cdn() aquí.
    El token CDN expira en ~30-60s. Si resolvemos en el scraper y Kodi
    tarda en llamar a play(), el token ya venció → "Remote end closed connection".
    En cambio, devolvemos la URL CDN tokenizada tal cual; player.py la resuelve
    justo antes de reproducir (latencia ~0s entre resolve y uso).

    _netu_resolve_cdn se mantiene en el código para referencia pero ya no se usa
    desde aquí. Si en el futuro se necesita pre-resolución, re-añadirla aquí.
    """
    html = _get(url, referer=referer or url)
    if not html:
        _err(f"  netu: no se obtuvo HTML de {url[:60]}")
        return None

    # Fase 1: URL .m3u8 / .mp4 directa
    for pat in [
        r'(https?://[^\s"\'<>]{30,}\.m3u8[^\s"\'<>]*)',
        r'file\s*:\s*["\']([^"\']{20,}\.m3u8[^"\']*)["\']',
        r'"file"\s*:\s*"(https?://[^"]{20,}\.m3u8[^"]*)"',
        r'hls\s*:\s*["\']([^"\']{20,}\.m3u8[^"\']*)["\']',
        r'(https?://[^\s"\'<>]{30,}\.mp4[^\s"\'<>]*)',
    ]:
        m = re.search(pat, html)
        if m and _ok(m.group(1)):
            _log(f"  netu directo → {m.group(1)[:70]}")
            return m.group(1)

    # Fase 2: URL CDN tokenizada — devolverla SIN resolver (player.py lo hará)
    for pat in [
        r'file\s*:\s*["\']([^"\']{30,})["\']',
        r'"file"\s*:\s*"(https?://[^"]{30,})"',
        r'hls\s*:\s*["\']([^"\']{30,})["\']',
        r'(https?://[^\s"\'<>]{30,}/(?:secip|hls|stream|video)/[^\s"\'<>]{10,})',
    ]:
        m = re.search(pat, html)
        if m and _ok(m.group(1)):
            _log(f"  netu CDN tokenizado → {m.group(1)[:70]}")
            return m.group(1)

    _err(f"  netu: no se encontró stream ({url[:60]})")
    return None


# ─── OK.ru ────────────────────────────────────────────────────────────────────

def _r_okru(url):
    html = _get(url, referer=url)
    if not html:
        return None
    for pat in [
        r'"hlsMasterPlaylistUrl"\s*:\s*"([^"]+)"',
        r'"videoSrc"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r'"url"\s*:\s*"(https?://[^"]+\.(?:m3u8|mp4)[^"]*)"',
    ]:
        m = re.search(pat, html)
        if m:
            val = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
            if _ok(val):
                return val
    return None


# ─── Fembed / Femax ───────────────────────────────────────────────────────────

def _r_fembed(url):
    vid = url.rstrip("/").split("/")[-1]
    for dom in ("fembed-hd.com", "femax20.com", "fembed.com"):
        raw = _post(f"https://{dom}/api/source/{vid}",
                    {"r": "", "d": dom}, referer=url)
        if raw:
            try:
                f = _best(json.loads(raw).get("data", []))
                if f and _ok(f): return f
            except Exception:
                pass
    return None


# ─── EmbedSito ────────────────────────────────────────────────────────────────

def _r_embedsito(url, referer=None):
    vid = url.rstrip("/").split("/")[-1]
    ref = referer or url
    for dom in ("embedsito.com", "www.embedsito.com"):
        raw = _post(f"https://{dom}/api/source/{vid}",
                    {"r": ref, "d": "embedsito.com"}, referer=url)
        if raw and not raw.lstrip().startswith("<"):
            try:
                f = _best(json.loads(raw).get("data", []))
                if f and _ok(f): return f
            except Exception:
                pass
    return _r_generic(url, ref)


# ─── Genérico ─────────────────────────────────────────────────────────────────

def _r_generic(url, referer=None):
    """
    Última opción: extrae la primera URL de vídeo absoluta y válida del HTML.
    NO fuerza extensión — si el CDN devuelve URL sin .mp4 / .m3u8, la
    detección de MIME queda en manos de Kodi (Content-Type del servidor).
    """
    html = _get(url, referer=referer or url)
    if not html:
        return None
    for pat in [
        r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
        r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'"url"\s*:\s*"(https?://[^"]+\.m3u8[^"]*)"',
        r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)',
        r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
        r'"url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r'<source[^>]+src="(https?://[^"]+)"',
        r'"src"\s*:\s*"(https?://[^"]+\.(?:m3u8|mp4)[^"]*)"',
    ]:
        m = re.search(pat, html)
        if m and _ok(m.group(1)):
            return m.group(1)
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

# 720p = prioridad 0: máxima calidad dentro del límite de recursos.
# 1080p = prioridad 1: se usa solo si no hay 720p disponible.
_RES_PRIO = {"720": 0, "1080": 1, "480": 2, "360": 3}


def _best(files):
    valid  = [f for f in files if f.get("file") and str(f["file"]).startswith("http")]
    ranked = sorted(valid, key=lambda x: _RES_PRIO.get(str(x.get("label", "")), 50))
    return ranked[0]["file"] if ranked else None


# ── Caché de episodios en memoria ─────────────────────────────────────────────
# Evita re-descargar la lista de episodios cuando la UI la pide y luego
# play_all la vuelve a pedir en la misma sesión.
# Clave: "slug|source", valor: dict devuelto por afl_episodes / jka_episodes.
#
# NOTA FUTURA: si se quiere caché persistente en disco (entre sesiones),
# almacenar en xbmcvfs translatePath("profile/ep_cache.json") con TTL de 1h.
_EP_CACHE: dict = {}


# ── API pública ───────────────────────────────────────────────────────────────

def search(query):
    afl = afl_search(query)
    jka = jka_search(query)
    seen = {r["title"].lower() for r in afl}
    for r in jka:
        if r["title"].lower() not in seen:
            afl.append(r)
    return afl


def get_episodes(slug, source):
    """
    Devuelve la lista de episodios para un slug/source.
    Cachea el resultado en memoria para evitar peticiones duplicadas
    (la UI carga la lista y luego play_all la vuelve a necesitar).
    """
    key = f"{slug}|{source}"
    if key in _EP_CACHE:
        _log(f"get_episodes: caché hit para {key}")
        return _EP_CACHE[key]

    if source == "animeflv":
        result = afl_episodes(slug)
    elif source == "jkanime":
        result = jka_episodes(slug)
    else:
        return None

    if result:
        _EP_CACHE[key] = result
    return result


def _infer_mime(url):
    """
    Infiere el MIME type de una URL de stream resuelta.
    Devuelve la cadena MIME o None si no se puede determinar.

    Kodi NECESITA el MIME type para streams sin extensión (CDN tokenizados).
    Sin él, el demuxer falla con "OpenDemuxStream - Error creating demuxer".

    Casos cubiertos:
      - .m3u8 en URL           → HLS (caso normal)
      - .mp4 / .mkv en URL     → MP4 / MKV
      - cfglobalcdn.com        → netu CDN, siempre HLS tokenizado
      - /secip/ en el path     → netu tokenized HLS (sin extensión)
      - /hls/ en el path       → convención estándar HLS de CDNs de anime
      - /master en el path     → típico de master.m3u8 sin extensión explícita

    NOTA FUTURA: si un CDN nuevo devuelve URLs sin extensión, añadir su
    patrón aquí. No cambiar nada en player.py ni en los resolvers.
    """
    lo = url.lower()
    if ".m3u8" in lo:
        return "application/x-mpegURL"
    if ".mp4" in lo:
        return "video/mp4"
    if ".mkv" in lo:
        return "video/x-matroska"
    # Netu/NetuPlayer CDN — HLS tokenizado
    if "cfglobalcdn.com" in lo or "globalcdn.com" in lo:
        return "application/x-mpegURL"
    # Path patterns de HLS tokenizado
    if any(p in lo for p in ("/secip/", "/hls/", "/master")):
        return "application/x-mpegURL"
    # StreamTape embed URL (lazy resolve en player.py)
    if "streamtape.com/e/" in lo or "streamtape.com/v/" in lo:
        return "video/stape_lazy"
    return None


def get_stream(slug, ep_num, source):
    """
    Devuelve (stream_url, embed_referer, mime_type) o None.

    mime_type es el MIME string necesario para que Kodi pueda seleccionar
    el demuxer correcto. Para streams cuya URL no tiene extensión conocida
    (CDN tokenizados como netu) el MIME se infiere con _infer_mime().
    player.py SIEMPRE debe usar el mime devuelto aquí en lugar de intentar
    inferirlo solo desde la extensión.
    """
    _log(f"get_stream slug={slug} ep={ep_num} src={source}")
    if source == "animeflv":
        r = afl_stream(slug, ep_num)
        if not r:
            _log("afl falló → jka")
            r = jka_stream(slug, ep_num)
    else:
        r = jka_stream(slug, ep_num)
        if not r:
            _log("jka falló → afl")
            r = afl_stream(slug, ep_num)
    if r is None:
        return None
    stream_url, embed_referer = r
    mime = _infer_mime(stream_url)
    _log(f"get_stream mime={mime or 'auto'} url={stream_url[:60]}")
    return stream_url, embed_referer, mime
