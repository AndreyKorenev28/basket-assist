"""Доменные перечисления. Значения — те же, что в docs/domain.md и в БД."""

from enum import StrEnum


class Platform(StrEnum):
    """Платформа сайта лиги — определяет адаптер (PlatformAdapter)."""

    MTGAME = "mtgame"  # АБЛ
    INFOBASKET = "infobasket"  # МЛБЛ


class Channel(StrEnum):
    VK = "vk"
    TELEGRAM = "telegram"
    MAX = "max"


class Role(StrEnum):
    PLAYER = "player"
    CAPTAIN = "captain"
    VICE_CAPTAIN = "vice_captain"


class Permission(StrEnum):
    """Делегируемые права. И. о. капитана получает все права капитана."""

    ACTING_CAPTAIN = "acting_captain"


class Vote(StrEnum):
    """Отметка участия. Варианта «под вопросом» нет (решение владельца)."""

    GOING = "going"
    NOT_GOING = "not_going"


class MatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    RESCHEDULED = "rescheduled"
    PLAYED = "played"
    CANCELLED = "cancelled"
    FORFEIT = "forfeit"


class TimeStatus(StrEnum):
    DATE_ONLY = "date_only"
    TIME_SET = "time_set"
    TIME_CHANGED = "time_changed"


class RosterLinkStatus(StrEnum):
    """Связь игрока с заявкой на сезон выступления."""

    SUGGESTED = "suggested"  # бот предложил, руководитель ещё не подтвердил
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    NOT_IN_ROSTER = "not_in_roster"  # руководитель отметил «нет в заявке на сезон»


class ApplicationSource(StrEnum):
    POLL = "poll"
    ADDED_BY_CAPTAIN = "added_by_captain"


class NotificationKind(StrEnum):
    POLL_STARTED = "poll_started"
    NO_TIME_WARNING = "no_time_warning"
    REMINDER_30H = "reminder_30h"
    DIGEST_24H = "digest_24h"
    DIGEST_2H = "digest_2h"
    VOTE_CHANGED = "vote_changed"
    NOT_IN_ROSTER = "not_in_roster"
    MATCH_RESCHEDULED = "match_rescheduled"
    TIME_CHANGED = "time_changed"
    ACTING_CAPTAIN_GRANTED = "acting_captain_granted"
    ACTING_CAPTAIN_EXPIRED = "acting_captain_expired"


class ChangeKind(StrEnum):
    """Изменения, обнаруженные при синхронизации с сайтом лиги."""

    MATCH_ADDED = "match_added"
    RESCHEDULED = "rescheduled"
    TIME_CHANGED = "time_changed"
    TIME_SET = "time_set"
    ROSTER_ADDED = "roster_added"
    ROSTER_REMOVED = "roster_removed"
