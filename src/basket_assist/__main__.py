"""Точка входа: python -m basket_assist [--check | --sync | --show]."""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from basket_assist.config import Settings, load_tenant_config
from basket_assist.services.sync import sync_team_entry
from basket_assist.sources import make_adapter
from basket_assist.sources.base import SourceError, make_client
from basket_assist.storage import db
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import League, LeagueTeam, Match, SeasonRosterEntry, Standing, TeamEntry

log = logging.getLogger("basket_assist")
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


async def _run(settings: Settings, do_sync: bool, do_show: bool) -> int:
    cfg = load_tenant_config(settings.tenant_config)
    engine = db.make_engine(settings.db_url)
    code = 0
    try:
        async with db.make_sessionmaker(engine)() as session:
            tenant = await apply_tenant_config(session, cfg)
            entries = (await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant.id))).all()
            print(
                f"Команда «{tenant.name}»: выступлений — {len(entries)}; лиги: "
                f"{', '.join(lc.name for lc in cfg.leagues)}"
            )
            if do_sync:
                async with make_client() as client:
                    for entry in entries:
                        team = await session.get(LeagueTeam, entry.league_team_id)
                        league = await session.get(League, team.league_id)
                        adapter = make_adapter(league.platform, client, league.store_birth_date)
                        try:
                            r = await sync_team_entry(session, entry, adapter)
                        except SourceError as e:
                            print(f"  ✗ {league.name}: {e}")
                            code = 2
                            continue
                        print(
                            f"  ✓ {league.name}: матчей новых {r.matches_new}, изменено {r.matches_updated}, "
                            f"заявка на сезон {r.roster_size}, таблица {r.standings}, изменений {len(r.changes)}"
                        )
            if do_show:
                for entry in entries:
                    await _show_entry(session, entry)
    finally:
        await engine.dispose()
    return code


async def _show_entry(session, entry: TeamEntry) -> None:
    team = await session.get(LeagueTeam, entry.league_team_id)
    league = await session.get(League, team.league_id)
    tz = ZoneInfo(league.timezone)
    standing = (
        await session.execute(
            select(Standing).filter_by(tournament_id=entry.tournament_id, league_team_id=team.id)
        )
    ).scalar_one_or_none()
    roster = await session.scalar(
        select(func.count())
        .select_from(SeasonRosterEntry)
        .filter_by(league_team_id=team.id, tournament_id=entry.tournament_id, active=True)
    )
    place = f"{standing.place}-е место, {standing.wins}–{standing.losses}" if standing else "нет таблицы"
    print(f"\n{team.name} ({league.name}): {place}; в заявке на сезон {roster}")
    today = datetime.now(UTC).astimezone(tz).date()
    upcoming = (
        await session.scalars(
            select(Match)
            .where(Match.tournament_id == entry.tournament_id)
            .where(or_(Match.home_team_id == team.id, Match.away_team_id == team.id))
            .where(Match.match_date >= today)
            .order_by(Match.match_date)
            .limit(3)
        )
    ).all()
    if not upcoming:
        print("  ближайших матчей на сайте лиги нет")
    for m in upcoming:
        rival_id = m.away_team_id if m.home_team_id == team.id else m.home_team_id
        rival = await session.get(LeagueTeam, rival_id)
        local = m.start_at.replace(tzinfo=UTC).astimezone(tz) if m.start_at else None
        when = (
            f"{local:%d.%m} {WEEKDAYS[local.weekday()]} {local:%H:%M}"
            if local
            else f"{m.match_date:%d.%m} (время не назначено)"
        )
        print(f"  {when} — {rival.name}; {m.venue_name or 'зал не указан'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="basket_assist")
    parser.add_argument("--check", action="store_true", help="проверить конфиг и БД, загрузить команду")
    parser.add_argument("--sync", action="store_true", help="синхронизировать данные лиг")
    parser.add_argument("--show", action="store_true", help="показать ближайшие матчи и таблицу")
    args = parser.parse_args(argv)

    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.tenant_config.exists():
        print(f"Нет файла конфигурации команды: {settings.tenant_config} (см. config/tenant.example.toml)")
        return 1

    db.upgrade(settings.db_url, settings.db_path)
    code = asyncio.run(_run(settings, args.sync, args.show))
    if not (args.check or args.sync or args.show):
        print("Бот пока не реализован (этап M2). Используйте --check, --sync, --show.")
    return code


if __name__ == "__main__":
    sys.exit(main())
