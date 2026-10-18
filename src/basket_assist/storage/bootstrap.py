"""Загрузка config/tenant.toml в БД при старте (идемпотентно).

В целевом решении это заменит подключение команды через бота (сценарий 0 в vision.md).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basket_assist.config import PersonConfig, TenantConfig
from basket_assist.domain.models import Channel, Role
from basket_assist.storage.tables import (
    Chat,
    League,
    LeagueTeam,
    MessengerAccount,
    Player,
    TeamEntry,
    Tenant,
    Tournament,
)


async def _get_or_create(session: AsyncSession, model, defaults: dict | None = None, **keys):
    obj = (await session.execute(select(model).filter_by(**keys))).scalar_one_or_none()
    if obj is None:
        obj = model(**keys, **(defaults or {}))
        session.add(obj)
        await session.flush()
    elif defaults:
        for name, value in defaults.items():
            setattr(obj, name, value)
    return obj


async def _ensure_leader(session: AsyncSession, tenant: Tenant, person: PersonConfig, role: Role) -> None:
    account = (
        await session.execute(
            select(MessengerAccount).filter_by(
                tenant_id=tenant.id, channel=Channel.VK, external_user_id=str(person.vk_user_id)
            )
        )
    ).scalar_one_or_none()
    if account is None:
        player = Player(tenant_id=tenant.id, display_name=person.display_name, role=role)
        session.add(player)
        await session.flush()
        session.add(
            MessengerAccount(
                tenant_id=tenant.id,
                player_id=player.id,
                channel=Channel.VK,
                external_user_id=str(person.vk_user_id),
            )
        )
    else:
        player = await session.get(Player, account.player_id)
        player.role = role
        player.display_name = person.display_name


async def apply_tenant_config(session: AsyncSession, cfg: TenantConfig) -> Tenant:
    tenant = await _get_or_create(session, Tenant, defaults={"name": cfg.name}, key=cfg.key)

    for lc in cfg.leagues:
        league = await _get_or_create(
            session,
            League,
            defaults={
                "name": lc.name,
                "platform": lc.platform,
                "base_url": lc.base_url,
                "timezone": lc.timezone,
                "min_players": lc.min_players,
                "max_players": lc.max_players,
                "poll_weekday": lc.poll_weekday,
                "poll_time": lc.poll_time.strftime("%H:%M"),
                "store_birth_date": lc.store_birth_date,
                "default_venue_address": lc.default_venue_address or None,
            },
            key=lc.key,
        )
        tournament = await _get_or_create(
            session,
            Tournament,
            defaults={"season": lc.season_external_id},
            league_id=league.id,
            external_id=lc.tournament_external_id,
        )
        team = await _get_or_create(
            session,
            LeagueTeam,
            defaults={"name": lc.team_name},
            league_id=league.id,
            external_id=lc.team_external_id,
        )
        await _get_or_create(
            session,
            TeamEntry,
            defaults={"source_url": lc.team_url},
            tenant_id=tenant.id,
            league_team_id=team.id,
            tournament_id=tournament.id,
        )

    if cfg.vk_chat_peer_id is not None:
        await _get_or_create(
            session,
            Chat,
            defaults={"tenant_id": tenant.id},
            channel=Channel.VK,
            peer_id=str(cfg.vk_chat_peer_id),
        )
    if cfg.captain:
        await _ensure_leader(session, tenant, cfg.captain, Role.CAPTAIN)
    for vice in cfg.vice_captains:
        await _ensure_leader(session, tenant, vice, Role.VICE_CAPTAIN)

    await session.commit()
    return tenant
