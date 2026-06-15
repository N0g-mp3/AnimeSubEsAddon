"""
AnimeSubES — Player v2.9.5 (Kodi 21)
======================================
Lazy-resolve para servidores con tokens efímeros:

  StreamTape (stape_lazy):
    El scraper devuelve la embed URL (streamtape.com/e/ID).
    _resolve_stape() hace el GET al embed y extrae el token fresco
    justo antes de setResolvedUrl. El token dura ~60s; entre que
    el scraper lo obtiene y Kodi lo usa puede pasar más tiempo si
    el usuario tiene varios episodios en playlist → 404.

  Netu / cfglobalcdn (application/x-mpegURL, dominio lazy):
    El scraper devuelve la URL CDN tokenizada.
    _resolve_cdn_with_kodi() usa xbmcvfs.copy() — el curl de Kodi —
    para descargar el m3u8. Kodi acepta certificados auto-firmados en
    modo verifypeer=false; Python/urllib no puede falsificar el TLS
    fingerprint (JA3) que cfglobalcdn exige.
"""
import re
import ssl
import gzip
import urllib.parse
import urllib.request

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

from . import history, metadata, scraper

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")

_MAX_SYN = 200

# Dominios que requieren lazy-resolve por token efímero + SSL problemático.
_LAZY_CDN = ("cfglobalcdn.com", "globalcdn.com")


def _log(msg): xbmc.log(f"[AnimeSubES] {msg}", xbmc.LOGINFO)
def _err(msg): xbmc.log(f"[AnimeSubES] ERR: {msg}", xbmc.LOGERROR)


def _safe_int(val):
    try:
        return int(float(val))
    except Exception:
        return 0


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http_get(url, referer, timeout=10):
    """GET con ssl.CERT_NONE, sigue redirects. Devuelve (final_url, content_type, body_str)."""
    parts  = urllib.parse.urlparse(referer)
    origin = f"{parts.scheme}://{parts.netloc}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Referer": referer,
        "Origin": origin, "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        raw = r.read()
        if r.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return r.url, r.headers.get("Content-Type", ""), raw.decode("utf-8", errors="ignore")


# ── StreamTape lazy-resolve ───────────────────────────────────────────────────

def _resolve_stape(embed_url):
    """
    Obtiene la URL de descarga directa de StreamTape justo antes de reproducir.
    El HTML de la página embed contiene el token en JS; lo extraemos aquí
    (token fresco, expira en ~60s).

    Patrones JS que cubre:
      A) var ideoooo = '/get_xcdvideo?id=...&token=';
         var ideooooLink = ideoooo.substring(N) + 'TOKEN';
         → URL real = to_abs(ideoooo[N:] + TOKEN)

      B) innerHTML = "//streamtape.com/..." + "TOKEN";
         → URL real = to_abs(concat de strings)

      C) getElementById('norobotlink').innerHTML = expr;
         → URL real = to_abs(expr evaluada)

    El truco anti-scraper de StreamTape: var ideoooo tiene un prefijo
    falso de N chars; .substring(N) lo quita. Si ignoramos el substring
    obtenemos una URL con endpoint falso (ej. /gxcdet_video) → 404.
    """
    try:
        _, _, html = _http_get(embed_url, embed_url, timeout=10)
    except Exception as e:
        _err(f"stape fetch embed: {e}")
        return None

    def _to_abs(s):
        s = s.strip()
        if s.startswith("//"):   return "https:" + s
        if s.startswith("/"):    return "https://streamtape.com" + s
        if not s.startswith("http"): return "https://" + s
        return s

    def _join_str_literals(expr):
        """Concatena todos los string literals JS de una expresión."""
        return "".join(m.group(1) for m in re.finditer(r"""['"]((?:(?!['"])[^\\]|\\.)*?)['"]""", expr))

    # ── Patrón A: var base + var_link = base.substring(N) + 'TOKEN' ──────────
    m_base = re.search(r"""var\s+(\w+)\s*=\s*['"](/[^'"]{10,})['"]""", html)
    if m_base:
        var_name  = m_base.group(1)
        full_path = m_base.group(2)
        # ¿Cuántos chars elimina el substring?
        m_sub = re.search(re.escape(var_name) + r'\.substring\((\d+)\)', html)
        sub_n = int(m_sub.group(1)) if m_sub else 0
        real_path = full_path[sub_n:]  # quitar el prefijo fake

        # Buscar token: var xxxLink = xxx.substring(N) + 'TOKEN'
        m_tok = re.search(
            re.escape(var_name) + r"""\w*\s*=\s*""" + re.escape(var_name)
            + r"""(?:\.substring\(\d+\))?\s*\+\s*['"](\w[\w\-]*)['"]""",
            html)
        if not m_tok:
            # Alternativa: innerHTML = varname.substring(N) + 'TOKEN'
            m_tok = re.search(
                r"""innerHTML\s*=\s*""" + re.escape(var_name)
                + r"""(?:\.substring\(\d+\))?\s*\+\s*['"](\w[\w\-]*)['"]""",
                html)
        if m_tok:
            lnk = _to_abs(real_path + m_tok.group(1))
            if lnk and "streamtape" in lnk:
                _log(f"stape var-sub ok → {lnk[:70]}")
                return lnk

    # ── Patrón B: innerHTML con string literals concatenados ──────────────────
    m = re.search(r"""innerHTML\s*=\s*((?:['"][^'"]*['"]\s*\+?\s*)+)\s*;""", html)
    if m:
        lnk = _to_abs(_join_str_literals(m.group(1)))
        if lnk and "streamtape" in lnk and ("get_" in lnk or "?id=" in lnk):
            _log(f"stape innerHTML-concat ok → {lnk[:70]}")
            return lnk

    # ── Patrón C: norobotlink (más fiable que robotlink — tiene URL real) ─────
    m = re.search(
        r"""getElementById\(['"]norobotlink['"]\)\s*\.innerHTML\s*=\s*([^;]+);""",
        html)
    if m:
        lnk = _to_abs(_join_str_literals(m.group(1)))
        if lnk and "streamtape" in lnk:
            _log(f"stape norobotlink ok → {lnk[:70]}")
            return lnk

    # ── Fallback: URL get_xcdvideo / get_video directa en el HTML ────────────
    for pat in [
        r'(https?://(?:www\.)?streamtape\.com/get_xcdvideo\?[^\s"\'<>]+)',
        r'(https?://(?:www\.)?streamtape\.com/get_video\?[^\s"\'<>]+)',
        r'(//(?:www\.)?streamtape\.com/get_xcdvideo\?[^\s"\'<>]+)',
    ]:
        fm = re.search(pat, html)
        if fm:
            lnk = _to_abs(fm.group(1))
            _log(f"stape fallback-direct ok → {lnk[:70]}")
            return lnk

    _err("stape: no se pudo extraer el link del embed")
    return None


# ── Netu / cfglobalcdn lazy-resolve con curl de Kodi ─────────────────────────

def _resolve_cdn_with_kodi(cdn_url, embed_referer):
    """
    Descarga el m3u8 de cfglobalcdn usando el curl interno de Kodi (xbmcvfs.copy).

    Python/urllib no puede conectar a cfglobalcdn porque bloquea por TLS
    fingerprint (JA3). El curl de Kodi sí conecta y acepta verifypeer=false.

    Flujo:
      1. Construir URL con pipe de headers (verifypeer=false + User-Agent + Referer)
      2. xbmcvfs.copy(url_con_headers, special://temp/...) → Kodi descarga el m3u8
      3. Leer el archivo descargado y reescribir rutas relativas → absolutas
      4. Escribir m3u8 final en special://temp/animesubes.m3u8
      5. Devolver la ruta local

    Si el CDN redirige a la URL real del m3u8 (302), Kodi sigue el redirect.
    """
    parts  = urllib.parse.urlparse(embed_referer)
    origin = f"{parts.scheme}://{parts.netloc}"
    pipe_hdrs = urllib.parse.urlencode({
        "verifypeer":  "false",
        "User-Agent":  _UA,
        "Referer":     embed_referer,
        "Origin":      origin,
        "Accept":      "*/*",
    })
    src_url  = cdn_url + "|" + pipe_hdrs
    tmp_raw  = "special://temp/animesubes_raw.m3u8"
    tmp_out  = "special://temp/animesubes.m3u8"

    # Descargar con el curl de Kodi
    ok = xbmcvfs.copy(src_url, tmp_raw)
    if not ok:
        _err("cdn kodi-fetch: xbmcvfs.copy falló")
        return None

    # Leer el archivo descargado
    try:
        f = xbmcvfs.File(tmp_raw, "r")
        content = f.read()
        f.close()
    except Exception as e:
        _err(f"cdn kodi-fetch: error leyendo tmp: {e}")
        return None

    if not content or not content.lstrip().startswith("#EXTM3U"):
        _err(f"cdn kodi-fetch: contenido no es m3u8 ({content[:50]!r})")
        return None

    # Reescribir rutas relativas → absolutas
    # La URL base es la URL del m3u8 (después de posibles redirects).
    # Como Kodi siguió los redirects, usamos cdn_url como base aproximada.
    base = cdn_url.rsplit("/", 1)[0] + "/"
    lines = []
    for line in content.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("http"):
            lines.append(base + s)
        else:
            lines.append(line)

    # Escribir m3u8 final
    try:
        f = xbmcvfs.File(tmp_out, "w")
        f.write("\n".join(lines))
        f.close()
    except Exception as e:
        _err(f"cdn kodi-fetch: error escribiendo m3u8: {e}")
        return None

    _log(f"cdn kodi-fetch: m3u8 listo ({len(lines)} líneas) → {tmp_out}")
    return tmp_out


# ── ListItem ──────────────────────────────────────────────────────────────────

def _set_info(li, info):
    try:
        tag = li.getVideoInfoTag()
        tag.setTitle(str(info.get("title") or ""))
        tag.setTvShowTitle(str(info.get("tvshowtitle") or ""))
        tag.setPlot(str(info.get("plot") or ""))
        tag.setMediaType("episode")
        if info.get("episode") is not None:
            tag.setEpisode(int(info["episode"]))
        tag.setSeason(1)
    except Exception:
        li.setInfo("video", info)


def _make_listitem(label, stream_url, embed_referer,
                   synopsis, cover, ep_num_i, show_title, mime=None):
    """
    Construye el ListItem para setResolvedUrl.

      mime="video/stape_lazy"     → StreamTape: resolver token ahora con _resolve_stape()
      mime="application/x-mpegURL" + dominio lazy → cfglobalcdn: xbmcvfs.copy()
      mime="application/x-mpegURL" normal → inputstream.adaptive directo
      mime=None / mp4 / otro → demuxer interno Kodi (menos CPU que ISA)
    """
    li = xbmcgui.ListItem(label)
    _set_info(li, {"title": label, "tvshowtitle": show_title,
                   "plot": synopsis, "episode": ep_num_i})
    li.setArt({"thumb": cover, "poster": cover, "fanart": cover})
    li.setProperty("IsPlayable", "true")
    li.setContentLookup(False)

    # ── StreamTape lazy ───────────────────────────────────────────────────────
    if mime == "video/stape_lazy":
        direct = _resolve_stape(stream_url)
        if direct:
            parts  = urllib.parse.urlparse(stream_url)
            origin = f"{parts.scheme}://{parts.netloc}"
            hdrs   = urllib.parse.urlencode({
                "User-Agent": _UA,
                "Referer":    stream_url,   # referer = embed URL
                "Origin":     origin,
                "Accept":     "*/*",
            })
            li.setPath(direct + "|" + hdrs)
            li.setMimeType("video/mp4")     # StreamTape siempre sirve MP4
            _log(f"stape lazy: url resuelta → {direct[:70]}")
            return li
        _err("stape lazy: no se pudo resolver, episodio no disponible")
        li.setPath("")
        return li

    # ── Netu / cfglobalcdn lazy ───────────────────────────────────────────────
    is_lazy_cdn = any(d in stream_url.lower() for d in _LAZY_CDN)
    if mime == "application/x-mpegURL" and is_lazy_cdn:
        local = _resolve_cdn_with_kodi(stream_url, embed_referer)
        if local:
            li.setPath(local)
            li.setMimeType("application/x-mpegURL")
            li.setProperty("inputstream", "inputstream.adaptive")
            _log("cdn lazy: reproduciendo desde m3u8 local")
            return li
        _err("cdn lazy: xbmcvfs.copy falló, episodio no disponible")
        li.setPath("")
        return li

    # ── HLS normal ────────────────────────────────────────────────────────────
    parts  = urllib.parse.urlparse(embed_referer)
    origin = f"{parts.scheme}://{parts.netloc}"
    hdrs   = urllib.parse.urlencode({
        "User-Agent": _UA,
        "Referer":    embed_referer,
        "Origin":     origin,
        "Accept":     "*/*",
    })

    if mime == "application/x-mpegURL":
        li.setPath(stream_url + "|" + hdrs)
        li.setMimeType(mime)
        li.setProperty("inputstream", "inputstream.adaptive")
        li.setProperty("inputstream.adaptive.stream_headers",   hdrs)
        li.setProperty("inputstream.adaptive.manifest_headers", hdrs)
    else:
        # MP4 / sin MIME: demuxer interno (menos CPU que ISA)
        li.setPath(stream_url + "|" + hdrs)
        if mime and mime not in ("video/stape_lazy",):
            li.setMimeType(mime)

    return li


# ── Plugin URL / lazy ListItem ────────────────────────────────────────────────

def _plugin_url(base_url, slug, ep_num, source, anime_title, cover, synopsis):
    return "{}?{}".format(base_url, urllib.parse.urlencode({
        "action": "play", "slug": slug, "ep": ep_num, "source": source,
        "anime_title": anime_title, "cover": cover,
        "synopsis": (synopsis or "")[:_MAX_SYN],
    }))


def _ep_listitem_lazy(label, ep_num_i, show_title, cover,
                      synopsis, plugin_url, show_cover):
    li = xbmcgui.ListItem(label, path=plugin_url)
    _set_info(li, {"title": label, "tvshowtitle": show_title,
                   "plot": synopsis, "episode": ep_num_i})
    li.setArt({"thumb": cover, "poster": show_cover, "fanart": cover})
    li.setProperty("IsPlayable", "true")
    li.setContentLookup(False)
    return li


# ── play() ────────────────────────────────────────────────────────────────────

def play(handle, base_url, slug, ep_num, source,
         anime_title="", cover="", synopsis=""):
    dlg = xbmcgui.DialogProgress()
    dlg.create("AnimeSubES", "Obteniendo enlace…")
    result = scraper.get_stream(slug, ep_num, source)
    dlg.close()

    if not result:
        xbmcgui.Dialog().ok("AnimeSubES",
            "No se pudo obtener el enlace.\n"
            "El servidor puede estar caído o el episodio no está disponible.")
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    stream_url, embed_referer, mime = result
    show_title = anime_title or slug
    ep_num_i   = _safe_int(ep_num)
    label      = f"{show_title} — Ep {ep_num}"
    _log(f"play: ep={ep_num} mime={mime or 'auto'} url={stream_url[:80]}")

    li = _make_listitem(label, stream_url, embed_referer,
                        synopsis, cover, ep_num_i, show_title, mime=mime)
    xbmcplugin.setResolvedUrl(handle, True, li)

    try:
        history.add(slug=slug, ep=ep_num, source=source,
                    anime_title=show_title, cover=cover)
    except Exception as e:
        _err(f"history.add: {e}")


# ── play_all() ────────────────────────────────────────────────────────────────

def play_all(handle, base_url, slug, source, start_ep=None):
    dlg = xbmcgui.DialogProgress()
    dlg.create("AnimeSubES", "Cargando episodios…")

    data = scraper.get_episodes(slug, source)
    if not data:
        dlg.close()
        xbmcgui.Dialog().notification("AnimeSubES", "Error cargando episodios",
                                      xbmcgui.NOTIFICATION_ERROR, 3000)
        xbmcplugin.endOfDirectory(handle, succeeded=False,
                                  updateListing=False, cacheToDisc=False)
        return

    title      = data["title"]
    episodes   = data["episodes"]
    show_cover = data.get("cover", "")

    dlg.update(50, "Cargando metadata…")
    bundle = metadata.get_show_metadata(title)
    dlg.close()

    if not episodes:
        xbmcgui.Dialog().notification("AnimeSubES", "Sin episodios",
                                      xbmcgui.NOTIFICATION_WARNING, 3000)
        xbmcplugin.endOfDirectory(handle, succeeded=False,
                                  updateListing=False, cacheToDisc=False)
        return

    start_num = _safe_int(start_ep) if start_ep else 1
    try:
        start_idx = next(i for i, ep in enumerate(episodes)
                         if _safe_int(ep["num"]) == start_num)
    except StopIteration:
        start_idx = 0

    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    playlist.clear()

    for ep in episodes:
        ep_meta  = metadata.episode_meta(bundle, ep["num"],
                                         fallback_cover=ep.get("cover") or show_cover)
        ep_label = f"{ep['num']}. {ep_meta['title'] or ep['title']}"
        purl = _plugin_url(base_url, slug, ep["num"], source,
                           title, ep_meta["cover"], ep_meta["synopsis"])
        li = _ep_listitem_lazy(ep_label, _safe_int(ep["num"]), title,
                               ep_meta["cover"], ep_meta["synopsis"], purl, show_cover)
        playlist.add(purl, li)

    _log(f"play_all: {len(episodes)} eps, start_idx={start_idx} "
         f"(ep {episodes[start_idx]['num']})")

    xbmcplugin.endOfDirectory(handle, succeeded=False,
                              updateListing=False, cacheToDisc=False)
    xbmc.sleep(300)
    xbmc.Player().play(playlist, startpos=start_idx)
