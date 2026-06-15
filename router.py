"""
AnimeSubES — Router v2.5
========================
Despacha acciones al módulo correcto.

Regla de handle:
  - "play"     → setResolvedUrl cierra el handle internamente.
  - "play_all" → player.play_all() llama endOfDirectory(False) internamente.
  - Resto      → este router llama endOfDirectory al final.

NOTAS PARA ACTUALIZACIÓN FUTURA:
  - Para agregar Favoritos, añadir acciones "favorites", "fav_add", "fav_remove"
    y el módulo resources/lib/favorites.py (estructura idéntica a history.py).
  - Para agregar búsquedas recientes, guardar cada query en un JSON al buscar
    y mostrarlas como sugerencias en la acción "search" si no hay query param.
  - Si se agregan más fuentes (ej. AnimeSama, MonosChinos), el router
    puede despacharlas aquí sin tocar el resto del código.
"""
import xbmcgui
import xbmcplugin

from . import history, player, ui


def dispatch(handle, base_url, action, params):
    def p(k):
        v = params.get(k)
        return v[0] if v else None

    # ── Búsqueda ──────────────────────────────────────────────────────────────
    if action == "search":
        query = p("query") or xbmcgui.Dialog().input(
            "Buscar anime…", type=xbmcgui.INPUT_ALPHANUM) or None
        if query:
            ui.search_results(handle, base_url, query)
        xbmcplugin.endOfDirectory(handle)

    # ── Lista de episodios ─────────────────────────────────────────────────────
    elif action == "episodes":
        ui.episode_list(handle, base_url, p("slug"), p("source"))
        xbmcplugin.endOfDirectory(handle)

    # ── Reproducir un episodio (setResolvedUrl cierra el handle) ──────────────
    elif action == "play":
        player.play(
            handle=handle, base_url=base_url,
            slug=p("slug"), ep_num=p("ep"), source=p("source"),
            anime_title=p("anime_title") or "",
            cover=p("cover") or "",
            synopsis=p("synopsis") or "",
        )

    # ── Playlist completa (endOfDirectory lo cierra internamente) ─────────────
    elif action == "play_all":
        player.play_all(
            handle=handle, base_url=base_url,
            slug=p("slug"), source=p("source"),
            start_ep=p("start_ep"),
        )

    # ── Historial por serie ────────────────────────────────────────────────────
    elif action == "history":
        ui.history_list(handle, base_url)
        xbmcplugin.endOfDirectory(handle)

    # ── Eliminar una serie del historial ──────────────────────────────────────
    elif action == "history_remove":
        slug   = p("slug")
        source = p("source")
        if slug and source:
            history.remove(slug, source)
            xbmcgui.Dialog().notification(
                "AnimeSubES", "Serie eliminada del historial",
                xbmcgui.NOTIFICATION_INFO, 2000)
        # No se abre directorio: esta acción viene de RunPlugin (menú contextual)

    # ── Borrar historial completo ──────────────────────────────────────────────
    elif action == "clear_history":
        history.clear()
        xbmcgui.Dialog().notification(
            "AnimeSubES", "Historial borrado",
            xbmcgui.NOTIFICATION_INFO, 2000)
        ui.history_list(handle, base_url)
        xbmcplugin.endOfDirectory(handle)

    # ── Menú principal (fallback) ──────────────────────────────────────────────
    else:
        ui.main_menu(handle, base_url)
        xbmcplugin.endOfDirectory(handle)
