"""Черновик первичного сопоставления: участники беседы → заявки на сезон всех выступлений.

Вход: data/team_chat_members.json ([{vk_id, first_name, last_name, deactivated}]) и БД после --sync.
Выход: data/matching_draft.md (вне git). Это только предложение — подтверждает руководитель команды.
"""

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from basket_assist.config import Settings
from basket_assist.domain.matching import Confidence, RosterName, norm, suggest
from basket_assist.storage import db
from basket_assist.storage.tables import League, LeagueTeam, SeasonRosterEntry, TeamEntry

MEMBERS = Path("data/team_chat_members.json")
CONFIRMED = Path("data/matching_confirmed.json")  # необязательный: подтверждения руководителя
OUT = Path("data/matching_draft.md")
MARK = {Confidence.EXACT: "✅", Confidence.PROBABLE: "❓"}


def fio(r: SeasonRosterEntry) -> str:
    num = f", №{r.number}" if r.number else ""
    return f"{r.last_name} {r.first_name}{num}"


async def main() -> None:
    members = json.loads(MEMBERS.read_text(encoding="utf-8"))
    confirmed = {}
    if CONFIRMED.exists():
        for link in json.loads(CONFIRMED.read_text(encoding="utf-8"))["links"]:
            confirmed[link["vk_id"]] = (norm(link["last_name"]), norm(link["first_name"]))
    engine = db.make_engine(Settings().db_url)
    async with db.make_sessionmaker(engine)() as s:
        leagues = []
        for entry in (await s.scalars(select(TeamEntry))).all():
            team = await s.get(LeagueTeam, entry.league_team_id)
            league = await s.get(League, team.league_id)
            rows = (
                await s.scalars(
                    select(SeasonRosterEntry).filter_by(
                        league_team_id=team.id, tournament_id=entry.tournament_id, active=True
                    )
                )
            ).all()
            leagues.append((f"{league.name} ({team.name})", {r.id: r for r in rows}))
    await engine.dispose()

    lines = [
        "# Черновик сопоставления: беседа команды → заявки на сезон",
        "",
        "✅ — совпали фамилия и имя (с учётом уменьшительных и латиницы); ❓ — вероятно: совпала фамилия, часть составной фамилии или единственное в заявке имя; — — нет.",
        "☑ — подтверждено руководителем (data/matching_confirmed.json). Остальное — только предложение.",
        "",
    ]
    header = "| Участник беседы (VK) | VK ID | " + " | ".join(name for name, _ in leagues) + " |"
    lines += [header, "|" + "---|" * (2 + len(leagues))]
    used: dict[int, set[int]] = {i: set() for i in range(len(leagues))}
    # строки заявки, занятые подтверждёнными привязками, другим не предлагаем
    taken = {
        i: {k for k, r in roster.items() if (norm(r.last_name), norm(r.first_name)) in confirmed.values()}
        for i, (_, roster) in enumerate(leagues)
    }
    stats = {"confirmed": 0, "exact": 0, "probable": 0, "none": 0, "deleted": 0}
    for m in members:
        if m.get("deactivated"):
            stats["deleted"] += 1
            lines.append(f"| (удалённый аккаунт) | {m['vk_id']} | " + " | ".join("—" for _ in leagues) + " |")
            continue
        if m["vk_id"] in confirmed:
            cells = []
            for i, (_, roster) in enumerate(leagues):
                hit = [
                    k
                    for k, r in roster.items()
                    if (norm(r.last_name), norm(r.first_name)) == confirmed[m["vk_id"]]
                ]
                used[i].update(hit)
                cells.append("<br>".join(f"☑ {fio(roster[k])}" for k in hit) or "—")
            stats["confirmed"] += 1
            lines.append(f"| {m['first_name']} {m['last_name']} | {m['vk_id']} | " + " | ".join(cells) + " |")
            continue
        cells, best = [], Confidence.NONE
        for i, (_, roster) in enumerate(leagues):
            cands = suggest(
                m["first_name"],
                m["last_name"],
                [RosterName(k, r.last_name, r.first_name) for k, r in roster.items() if k not in taken[i]],
            )
            if not cands:
                cells.append("—")
                continue
            used[i].update(c.key for c in cands)
            cells.append("<br>".join(f"{MARK[c.confidence]} {fio(roster[c.key])}" for c in cands))
            if cands[0].confidence == Confidence.EXACT or best == Confidence.NONE:
                best = cands[0].confidence
        stats[best.value] += 1
        lines.append(f"| {m['first_name']} {m['last_name']} | {m['vk_id']} | " + " | ".join(cells) + " |")

    lines += ["", "## В заявке на сезон, но не найдены в беседе", ""]
    for i, (name, roster) in enumerate(leagues):
        missing = [fio(r) for k, r in roster.items() if k not in used[i]]
        lines.append(f"**{name}** ({len(missing)}): " + ("; ".join(missing) if missing else "—"))
    lines += [
        "",
        f"Итого участников: {len(members)}; ☑ {stats['confirmed']}, ✅ {stats['exact']}, ❓ {stats['probable']}, "
        f"не найдено {stats['none']}, удалённых аккаунтов {stats['deleted']}.",
    ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{OUT}: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
