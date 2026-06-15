"""
AnimeSubES — UI v2.6
=====================
CAMBIO CLAVE respecto a v2.5:
  Hacer click en cualquier episodio de la lista lanza play_all(start_ep=N),
  igual que el addon Otaku. Así la playlist continúa automáticamente con el
  siguiente episodio sin necesitar un botón separado "Reproducir todo".

Flujo:
  Usuario en lista de episodios → click ep 5
    → action=play_all&start_ep=5
    → player.play_all() crea playlist completa, arranca desde ep 5
    → ep 6, 7, 8... se reproducen solos
    → cada ep llama action=play (lazy) → guarda historial

Historial:
  Siempre por SERIE. Click en historial → abre lista de episodios → botón
  "Continuar viendo Ep X" marca el último visto. Igual que Otaku.

NOTAS PARA ACTUALIZACIÓN FUTURA:
  - Para agregar Favoritos, duplicar la lógica de historial con favorites.py.
  - Para búsquedas recientes, guardar queries en search_history.json.
  - El marcador ▶ en el último episodio visto depende de history.get_last_ep().
  - Si AnimeFLV añade filtros (género, estado), agregar action="browse" con params.
"""
import urllib.parse

import xbmcgui
import xbmcplugin

from . import history, metadata, scraper


def _url(base, **kw):
    return "{}?{}".format(base, urllib.parse.urlencode(kw))


def _set_info(li, info):
    """Compatible Kodi 19 (setInfo) y 20/21 (InfoTagVideo)."""
    try:
        tag = li.getVideoInfoTag()
        tag.setTitle(str(info.get("title") or ""))
        tag.setTvShowTitle(str(info.get("tvshowtitle") or ""))
        tag.setPlot(str(info.get("plot") or ""))
        tag.setMediaType(str(info.get("mediatype") or "video"))
        ep = info.get("episode")
        if ep is not None:
            tag.setEpisode(int(ep))
        sn = info.get("season")
        if sn is not None:
            tag.setSeason(int(sn))
    except Exception:
        li.setInfo("video", info)


def _add(handle, url, label, is_folder, art=None, info=None,
         playable=False, cm=None):
    li = xbmcgui.ListItem(label)
    if art:
        li.setArt(art)
    if info:
        _set_info(li, info)
    if playable:
        li.setProperty("IsPlayable", "true")
    if cm:
        li.addContextMenuItems(cm)
    xbmcplugin.addDirectoryItem(handle, url, li, is_folder)


def _safe_int(val):
    try:
        return int(float(val))
    except Exception:
        return 0


# ── Menú principal ─────────────────────────────────────────────────────────────

def main_menu(handle, base_url):
    xbmcplugin.setPluginCategory(handle, "AnimeSubES")
    xbmcplugin.setContent(handle, "tvshows")

    recent = history.get_recent(limit=1)
    if recent:
        _add(handle, _url(base_url, action="history"),
             "▶  Continuar viendo", True)
    else:
        _add(handle, _url(base_url, action="history"),
             "📋  Últimos vistos", True)

    _add(handle, _url(base_url, action="search"), "🔍  Buscar anime", True)


# ── Búsqueda ──────────────────────────────────────────────────────────────────

def search_results(handle, base_url, query):
    xbmcplugin.setPluginCategory(handle, f"Búsqueda: {query}")
    xbmcplugin.setContent(handle, "tvshows")

    results = scraper.search(query)
    if not results:
        xbmcgui.Dialog().notification(
            "AnimeSubES", "Sin resultados", xbmcgui.NOTIFICATION_INFO, 3000)
        return

    for item in results:
        art  = {"thumb": item.get("cover", ""), "poster": item.get("cover", "")}
        info = {"title": item["title"], "mediatype": "tvshow"}
        url  = _url(base_url, action="episodes",
                    slug=item["slug"], source=item["source"])
        _add(handle, url, item["title"], True, art=art, info=info)


# ── Lista de episodios ─────────────────────────────────────────────────────────

def episode_list(handle, base_url, slug, source):
    """
    Lista de episodios.

    IMPORTANTE: cada episodio llama action=play_all con start_ep=N (NO action=play).
    Esto hace que al hacer click en cualquier episodio se arranque la playlist
    completa desde ese punto, exactamente como Otaku.

    Los ítems de episodio NO tienen IsPlayable=true porque no resuelven un stream
    directamente — son "scripts" que construyen la playlist y la inician.
    """
    dlg = xbmcgui.DialogProgress()
    dlg.create("AnimeSubES", "Cargando…")

    dlg.update(10, "Cargando lista de episodios…")
    data = scraper.get_episodes(slug, source)
    if not data:
        dlg.close()
        xbmcgui.Dialog().notification(
            "AnimeSubES", "Error al cargar episodios",
            xbmcgui.NOTIFICATION_ERROR, 3000)
        return

    title      = data["title"]
    show_cover = data.get("cover", "")
    episodes   = data["episodes"]

    dlg.update(40, "Cargando metadata TMDB…")
    bundle        = metadata.get_show_metadata(title)
    show_overview = bundle.get("overview") or ""
    dlg.close()

    xbmcplugin.setPluginCategory(handle, title)
    xbmcplugin.setContent(handle, "episodes")

    # Último episodio visto para esta serie
    last_ep   = history.get_last_ep(slug, source)
    last_ep_i = _safe_int(last_ep) if last_ep else 0
    total     = len(episodes)

    # ── Botón "Continuar viendo" (solo si hay historial) ──────────────────────
    if last_ep and last_ep_i > 0:
        lbl = f"▶  Continuar viendo — Ep {last_ep} / {total}"
        continue_li = xbmcgui.ListItem(lbl)
        continue_li.setArt({"thumb": show_cover, "poster": show_cover})
        _set_info(continue_li, {
            "title":       lbl,
            "tvshowtitle": title,
            "plot":        show_overview,
            "mediatype":   "tvshow",
        })
        continue_url = _url(base_url, action="play_all",
                            slug=slug, source=source, start_ep=last_ep)
        xbmcplugin.addDirectoryItem(handle, continue_url, continue_li, False)

    # ── Botón "Reproducir desde el principio" ─────────────────────────────────
    play_all_li = xbmcgui.ListItem("⏭  Reproducir desde el principio")
    play_all_li.setArt({"thumb": show_cover, "poster": show_cover})
    _set_info(play_all_li, {
        "title":       f"Reproducir todo — {title}",
        "tvshowtitle": title,
        "plot":        show_overview,
        "mediatype":   "tvshow",
    })
    play_all_url = _url(base_url, action="play_all",
                        slug=slug, source=source, start_ep="1")
    xbmcplugin.addDirectoryItem(handle, play_all_url, play_all_li, False)

    # ── Episodios ──────────────────────────────────────────────────────────────
    # Cada episodio llama play_all(start_ep=N) → reproduce desde ese episodio
    # en adelante como playlist completa. Igual que Otaku.
    for ep in episodes:
        ep_cover = ep.get("cover") or show_cover
        ep_meta  = metadata.episode_meta(bundle, ep["num"],
                                         fallback_cover=ep_cover)

        ep_name  = ep_meta["title"] or ep["title"]
        ep_num_i = _safe_int(ep["num"])

        # Marcar con ▶ el último episodio visto
        marker = "▶ " if ep_num_i == last_ep_i else ""
        label  = f"{marker}{ep['num']}. {ep_name}"

        art  = {"thumb":  ep_meta["cover"],
                "poster": show_cover,
                "fanart": ep_meta["cover"]}
        info = {"title":       label,
                "tvshowtitle": title,
                "plot":        ep_meta["synopsis"],
                "episode":     ep_num_i,
                "season":      1,
                "mediatype":   "episode"}

        # CLAVE: action=play_all con start_ep=N, NO action=play
        # Esto hace que click en un episodio arranque la playlist desde ahí.
        url = _url(base_url, action="play_all",
                   slug=slug, source=source, start_ep=ep["num"])

        # playable=False: es un script que inicia la playlist, no una URL de stream.
        _add(handle, url, label, False, art=art, info=info, playable=False)


# ── Historial (por serie, estilo Otaku) ───────────────────────────────────────

def history_list(handle, base_url):
    """
    Historial POR SERIE. Cada entrada muestra el anime con el último ep visto.
    Click → lista de episodios (con botón "Continuar viendo Ep X").
    Menú contextual → eliminar serie del historial.
    """
    xbmcplugin.setPluginCategory(handle, "Continuar viendo")
    xbmcplugin.setContent(handle, "tvshows")

    entries = history.get_recent()
    if not entries:
        xbmcgui.Dialog().notification(
            "AnimeSubES", "El historial está vacío",
            xbmcgui.NOTIFICATION_INFO, 2500)
        _add(handle, _url(base_url, action="search"),
             "🔍  Buscar anime para empezar", True)
        return

    for e in entries:
        slug        = e["slug"]
        source      = e["source"]
        anime_title = e["anime_title"]
        last_ep     = e["ep"]
        cover       = e.get("cover", "")

        label = anime_title
        if last_ep:
            label += f" — Ep {last_ep}"
            total = e.get("total")
            if total:
                label += f" / {total}"

        art  = {"thumb": cover, "poster": cover}
        info = {"title": anime_title, "mediatype": "tvshow"}

        # Click → lista de episodios (tiene el botón "Continuar viendo")
        url = _url(base_url, action="episodes", slug=slug, source=source)

        # Menú contextual: eliminar del historial
        remove_url = _url(base_url, action="history_remove",
                          slug=slug, source=source)
        cm = [("Eliminar del historial", f"RunPlugin({remove_url})")]

        _add(handle, url, label, True, art=art, info=info, cm=cm)

    _add(handle, _url(base_url, action="clear_history"),
         "🗑  Borrar historial completo", False)
