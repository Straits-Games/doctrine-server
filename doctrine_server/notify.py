"""Notifications (Discord or anything else) are fire-and-forget side effects.

The API calls a Notifier after the database change is committed; `safe_call` makes
sure a failing or slow notifier can never fail the player's request. A Notifier must
return quickly (schedule the real work elsewhere, as the Discord plugin does).

The `game` argument is the plain dict from `games.notify_view` (no secrets).
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("doctrine_server.notify")


@runtime_checkable
class Notifier(Protocol):
    def game_created(self, game: dict) -> None: ...

    def turn_posted(self, game: dict, recipient_side: int, summary: str) -> None: ...

    def game_ended(self, game: dict) -> None: ...

    def status(self) -> str:
        """'off' | 'connected' | 'disconnected' (shown by /healthz)."""
        ...


class NullNotifier:
    def game_created(self, game: dict) -> None:
        pass

    def turn_posted(self, game: dict, recipient_side: int, summary: str) -> None:
        pass

    def game_ended(self, game: dict) -> None:
        pass

    def status(self) -> str:
        return "off"


def safe_call(notifier: Notifier, method: str, *args) -> None:
    try:
        getattr(notifier, method)(*args)
    except Exception:  # noqa: BLE001 — a notifier must never break a request
        log.exception("notifier %s failed", method)


def safe_status(notifier: Notifier) -> str:
    try:
        return str(notifier.status())
    except Exception:  # noqa: BLE001
        log.exception("notifier status failed")
        return "disconnected"
