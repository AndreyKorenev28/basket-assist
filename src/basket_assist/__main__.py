"""Точка входа.

python -m basket_assist                 — запустить бота (VK Long Poll)
python -m basket_assist --check         — проверить конфиг и БД, загрузить команду
python -m basket_assist --sync --show   — синхронизировать лиги и показать ближайшие матчи
python -m basket_assist --post-poll ID  — вручную опубликовать анонс-голосование по матчу
python -m basket_assist --import-chat   — загрузить участников беседы и подтверждения сопоставления (data/)
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import func, select

from basket_assist import app
from basket_assist.config import Settings, load_tenant_config
from basket_assist.services.roster import import_chat_members
from basket_assist.services.schedule import format_when, teams, upcoming_matches
from basket_assist.services.sync import sync_team_entry
from basket_assist.sources import make_adapter
from basket_assist.sources.base import SourceError, make_client
from basket_assist.storage import db
from basket_assist.storage.bootstrap import apply_tenant_config
from basket_assist.storage.tables import League, LeagueTeam, SeasonRosterEntry, Standing, TeamEntry

log = logging.getLogger("basket_assist")


async def _prepare(settings: Settings, do_sync: bool, do_show: bool, do_import: bool = False) -> int:
    cfg = load_tenant_config(settings.tenant_config)
    engine = db.make_engine(settings.db_url)
    code = 0
    try:
        async with db.make_sessionmaker(engine)() as session:
            tenant = await apply_tenant_config(session, cfg)
            entries = (await session.scalars(select(TeamEntry).filter_by(tenant_id=tenant.id))).all()
            print(
                f"Команда «{tenant.name}»: выступлений — {len(entries)}; "
                f"лиги: {', '.join(lc.name for lc in cfg.leagues)}"
            )
            if do_sync:
                code = await _sync_all(session, entries)
            if do_import:
                await _import(session, tenant.id)
            if do_show:
                await _show(session, tenant.id, entries)
    finally:
        await engine.dispose()
    return code


async def _sync_all(session, entries) -> int:
    code = 0
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
    return code


async def _import(session, tenant_id: int) -> None:
    members_file, confirmed_file = Path("data/team_chat_members.json"), Path("data/matching_confirmed.json")
    if not members_file.exists():
        print(f"Нет {members_file}: список участников беседы (vk_id, first_name, last_name)")
        return
    members = json.loads(members_file.read_text(encoding="utf-8"))
    confirmed = json.loads(confirmed_file.read_text(encoding="utf-8")) if confirmed_file.exists() else {}
    stats = await import_chat_members(
        session, tenant_id, members, confirmed.get("links", []), confirmed.get("confirmed_by_vk_id")
    )
    print(
        f"Импорт беседы: новых игроков {stats['players']}, подтверждённых связей {stats['confirmed']}, "
        f"предложенных (однозначных) {stats['suggested']}"
    )


async def _show(session, tenant_id: int, entries) -> None:
    for entry in entries:
        team = await session.get(LeagueTeam, entry.league_team_id)
        league = await session.get(League, team.league_id)
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
        print(f"{team.name} ({league.name}): {place}; в заявке на сезон {roster}")
    items = await upcoming_matches(session, tenant_id)
    print("\nБлижайшие матчи (ID — для --post-poll):" if items else "\nБлижайших матчей на сайтах лиг нет")
    for league, ours, rival, m in items:
        print(
            f"  [{m.id}] {format_when(m, league)} — {league.name}: {teams(m, ours, rival)}; {m.venue_name or ''}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="basket_assist")
    parser.add_argument("--check", action="store_true", help="проверить конфиг и БД, загрузить команду")
    parser.add_argument("--sync", action="store_true", help="синхронизировать данные лиг")
    parser.add_argument("--show", action="store_true", help="показать таблицу и ближайшие матчи")
    parser.add_argument("--post-poll", type=int, metavar="ID", help="опубликовать анонс-голосование по матчу")
    parser.add_argument("--import-chat", action="store_true", help="импорт участников беседы и сопоставления")
    args = parser.parse_args(argv)

    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пишет URL запросов, а в URL Long Poll есть ключ — секреты в лог не пускаем
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.tenant_config.exists():
        print(f"Нет файла конфигурации команды: {settings.tenant_config} (см. config/tenant.example.toml)")
        return 1

    db.upgrade(settings.db_url, settings.db_path)
    code = asyncio.run(_prepare(settings, args.sync, args.show, args.import_chat))
    if args.post_poll is not None:
        print(asyncio.run(app.post_poll(settings, args.post_poll)))
    elif not (args.check or args.sync or args.show or args.import_chat):
        try:
            asyncio.run(app.run_bot(settings))
        except KeyboardInterrupt:
            print("Остановлено.")
    return code


if __name__ == "__main__":
    sys.exit(main())
