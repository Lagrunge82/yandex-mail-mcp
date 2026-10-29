"""Changes waiting for a person to approve them in the browser.

MCP clients without elicitation cannot ask the user anything, so a changing tool
parks the change here and returns a link. The page behind the link shows the
same question an elicitation would, and only the signed-in owner of the mailbox
can approve it. Everything lives in memory and expires in ten minutes: a restart
forgets pending changes, which then simply never happen.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CONFIRM_TTL = 600.0
MAX_PENDING_PER_USER = 20


class ConfirmationFailed(Exception):
    """An approved change did not happen; the message is safe to show to the user."""


@dataclass
class PendingAction:
    id: str
    login: str
    email: str
    name: str
    question: str
    action: Callable[[], dict[str, Any]] = field(repr=False)
    expires_at: float
    decision: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def decided(self) -> bool:
        return self.decision is not None


class PendingActions:
    def __init__(self, clock: Callable[[], float] = time.time, ttl: float = CONFIRM_TTL) -> None:
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._actions: dict[str, PendingAction] = {}

    def _prune(self) -> None:
        now = self._clock()
        for key in [key for key, item in self._actions.items() if item.expires_at <= now]:
            del self._actions[key]

    def create(
        self,
        *,
        login: str,
        email: str,
        name: str,
        question: str,
        action: Callable[[], dict[str, Any]],
    ) -> PendingAction:
        """Park a change for the user; the newest ones replace the oldest past the cap."""
        with self._lock:
            self._prune()
            mine = sorted(
                (
                    item
                    for item in self._actions.values()
                    if item.login == login and not item.decided
                ),
                key=lambda item: item.expires_at,
            )
            for stale in mine[: max(0, len(mine) + 1 - MAX_PENDING_PER_USER)]:
                del self._actions[stale.id]
            pending = PendingAction(
                id=secrets.token_urlsafe(32),
                login=login,
                email=email,
                name=name,
                question=question,
                action=action,
                expires_at=self._clock() + self._ttl,
            )
            self._actions[pending.id] = pending
            return pending

    def get(self, action_id: str) -> PendingAction | None:
        with self._lock:
            self._prune()
            return self._actions.get(action_id)

    def claim(self, action_id: str, login: str, decision: str) -> PendingAction | None:
        """Record the decision of the user who owns the change; None if it is not theirs.

        A change can be decided once. The caller runs the action of an approved
        change outside the lock and stores the outcome with ``finish``.
        """
        with self._lock:
            self._prune()
            pending = self._actions.get(action_id)
            if pending is None or pending.login != login or pending.decided:
                return None
            pending.decision = decision
            return pending

    @staticmethod
    def finish(pending: PendingAction, *, result: dict[str, Any] | None, error: str | None) -> None:
        pending.result = result
        pending.error = error

    def seconds_left(self, pending: PendingAction) -> int:
        return max(0, int(pending.expires_at - self._clock()))
