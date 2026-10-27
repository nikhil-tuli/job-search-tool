"""Which Fit result to show for a role.

A role that has been considered for scoring keeps the Fit it had then, whatever happens to its
status, its age or the Fit settings afterwards. Scoring it again replaces that Fit. A role that
has never been considered is checked live. A role scored before Fit was saved gets its Fit
worked out once, ignoring recency, and saved.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .fit_filters import FitResult, FitRules, evaluate
from .models import Role
from .store import Store
from .timeutil import to_iso


class FitBook:
    def __init__(self, store: Store, rules: FitRules, now: datetime) -> None:
        self._store = store
        self._rules = rules
        self._now = now
        self._saved = {key: result for key, (result, _) in store.get_fits().items()}
        self._scored = set(store.get_scores())

    def get(self, role: Role) -> FitResult:
        saved = self._saved.get(role.key)
        if saved is not None:
            return saved
        if role.key in self._scored:
            result = evaluate(role, self._rules, self._now, ignore_recency=True)
            self._store.save_fits([(role.portal, role.job_id, result, "")], when=to_iso(self._now))
            self._saved[role.key] = result
            return result
        return evaluate(role, self._rules, self._now)

    def for_roles(self, roles: Iterable[Role]) -> dict[str, FitResult]:
        with self._store.transaction():
            return {role.key: self.get(role) for role in roles}
