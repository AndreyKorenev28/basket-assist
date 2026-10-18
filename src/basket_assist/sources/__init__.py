"""Адаптеры платформ лиг."""

import httpx

from basket_assist.domain.models import Platform
from basket_assist.sources.base import PlatformAdapter
from basket_assist.sources.infobasket import InfobasketAdapter
from basket_assist.sources.mtgame import MtgameAdapter


def make_adapter(platform: str, client: httpx.AsyncClient, store_birth_date: bool = False) -> PlatformAdapter:
    match Platform(platform):
        case Platform.MTGAME:
            return MtgameAdapter(client, store_birth_date)
        case Platform.INFOBASKET:
            return InfobasketAdapter(client, store_birth_date)
