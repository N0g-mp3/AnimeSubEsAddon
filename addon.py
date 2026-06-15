"""
AnimeSubES - Kodi 19/20/21 Plugin
Fuentes: AnimeFLV + JKAnime
Sinopsis: TMDB (exclusivo)
Playlist nativa de episodios
"""
import sys
from urllib.parse import parse_qs, urlparse

from resources.lib import router

_params  = parse_qs(urlparse(sys.argv[2]).query)
_action  = (_params.get("action") or ["main_menu"])[0]

router.dispatch(
    handle=int(sys.argv[1]),
    base_url=sys.argv[0],
    action=_action,
    params=_params,
)
