"""Per-user weekly spending limits.

Two budgets per account, because the two ways of spending money here are nothing
alike:

* **ask** — questions in the Ask tab, and the verifier that re-checks their
  citations. Small, frequent, and driven by whoever is signed in.
* **index** — the sweep that writes one catalogue card per document. One large
  deliberate act, usually run once per data room.

A single pooled figure would have to be either too small to sweep with or too
large to be a limit on questions, so they are counted and capped separately.

The week runs Monday 00:00 to Monday 00:00 in the server's local time. A rolling
window is fairer but has no reset anyone can name, and "when do I get my budget
back" is the first question someone asks after being stopped.

One softener: an answer that is abandoned halfway has already spent money and
produced nothing. So a user may overrun the ask budget by
``DD_BUDGET_GRACE_PCT`` to finish a single answer — once per week. The grace is
claimed the moment it is first needed and stays claimed until Monday, so the next
answer that runs out stops where it stands. Sweeps get no grace and need none:
they stop between documents, cards already written are kept, and re-running
continues from there, so nothing is wasted.
"""

from __future__ import annotations

import datetime as dt
import time

from . import db
from .config import settings
from .events import broker

# Stored budget sentinels. NULL/None in the table means "inherit the instance
# default"; these two are the values a resolved budget can take besides a cap.
UNLIMITED = -1.0

BUDGETS = ("ask", "index")


# --- the week ------------------------------------------------------------


def week_start(now: float | None = None) -> float:
    """Timestamp of Monday 00:00 local time for the week containing `now`."""
    when = dt.datetime.fromtimestamp(now if now is not None else time.time())
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - dt.timedelta(days=midnight.weekday())).timestamp()


def week_end(now: float | None = None) -> float:
    """When the current allowance resets."""
    return week_start(now) + 7 * 24 * 3600


def week_key(now: float | None = None) -> str:
    """``2026-W33`` — stable label for the week, used to expire a grace claim."""
    when = dt.datetime.fromtimestamp(now if now is not None else time.time())
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


# --- stored settings -----------------------------------------------------


def _row(username: str) -> dict:
    r = db.one("SELECT * FROM user_budgets WHERE username=?", (username,))
    return dict(r) if r else {}


def default_for(budget: str) -> float:
    return (
        settings.weekly_budget_ask_usd if budget == "ask"
        else settings.weekly_budget_index_usd
    )


def parse_setting(value) -> float | None:
    """Turn what the API accepts into what the column stores.

    ``None``/``"default"`` inherit the instance default, ``"unlimited"`` lifts the
    cap, a number is a cap. Strings rather than magic numbers on the wire so that
    nobody has to remember that -1 means unlimited, and so a typed 0 unambiguously
    means "no spending" instead of the opposite.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "default", "inherit"):
            return None
        if text in ("unlimited", "none", "off"):
            return UNLIMITED
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(
                f"budget must be a number, 'unlimited' or 'default' — got {value!r}"
            ) from exc
    value = float(value)
    if value < 0 and value != UNLIMITED:
        raise ValueError("a budget cannot be negative")
    return value


def set_budgets(
    username: str, *, ask=..., index=..., actor: str | None = None
) -> dict:
    """Upsert one account's caps. Omitted arguments are left alone."""
    username = username.strip().lower()
    row = _row(username)
    ask_usd = row.get("ask_usd") if ask is ... else parse_setting(ask)
    index_usd = row.get("index_usd") if index is ... else parse_setting(index)
    db.execute(
        "INSERT INTO user_budgets(username, ask_usd, index_usd, grace_week, updated_at,"
        " updated_by) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(username) DO UPDATE SET ask_usd=excluded.ask_usd,"
        " index_usd=excluded.index_usd, updated_at=excluded.updated_at,"
        " updated_by=excluded.updated_by",
        (username, ask_usd, index_usd, row.get("grace_week"), time.time(), actor),
    )
    db.audit("budget.set", actor=actor,
             detail=f"{username} ask={describe(ask_usd, 'ask')} "
                    f"index={describe(index_usd, 'index')}")
    return status(username)


def forget(username: str) -> None:
    """Drop an account's row — called when the account is deleted."""
    db.execute("DELETE FROM user_budgets WHERE username=?", (username.strip().lower(),))


def describe(stored: float | None, budget: str) -> str:
    if stored is None:
        return f"default ({describe(default_for(budget), budget)})"
    if stored == UNLIMITED:
        return "unlimited"
    return f"${stored:,.2f}"


# --- the decision --------------------------------------------------------


def effective(username: str | None, budget: str) -> float:
    """The cap that applies, after inheritance. UNLIMITED means no cap."""
    if not username:
        return UNLIMITED  # nothing initiated it, so nothing to charge it to
    stored = _row(username).get(f"{'ask' if budget == 'ask' else 'index'}_usd")
    return default_for(budget) if stored is None else float(stored)


def spent(username: str | None, budget: str, now: float | None = None) -> float:
    if not username:
        return 0.0
    return db.spend_since(week_start(now), username=username, budget=budget)


def grace_available(username: str | None, now: float | None = None) -> bool:
    """True if this account has not yet used its overrun this week."""
    if not username:
        return False
    return _row(username).get("grace_week") != week_key(now)


def claim_grace(username: str, *, ref: str | None = None) -> bool:
    """Consume the weekly overrun. False if it was already used this week.

    Written before the extra spend happens, so two answers racing to overrun
    cannot both be allowed through.
    """
    key = week_key()
    cur = db.execute(
        "INSERT INTO user_budgets(username, grace_week, updated_at) VALUES(?,?,?)"
        " ON CONFLICT(username) DO UPDATE SET grace_week=excluded.grace_week,"
        " updated_at=excluded.updated_at WHERE user_budgets.grace_week IS NOT ?",
        (username, key, time.time(), key),
    )
    claimed = bool(cur.rowcount)
    if claimed:
        db.audit("budget.grace", actor=username, detail=f"{key} ref={ref or '-'}")
    return claimed


def graced_cap(cap: float) -> float:
    """The cap plus this week's one-time overrun."""
    return cap * (1.0 + max(settings.budget_grace_pct, 0.0) / 100.0)


def status(username: str | None, now: float | None = None) -> dict:
    """Everything the UI needs to render one account's position."""
    out: dict = {
        "username": username,
        "week_start": week_start(now),
        "resets_at": week_end(now),
        "grace_pct": settings.budget_grace_pct,
        "grace_available": grace_available(username, now),
    }
    for b in BUDGETS:
        cap = effective(username, b)
        used = spent(username, b, now)
        stored = _row(username).get(f"{b}_usd") if username else None
        out[b] = {
            "limit_usd": None if cap == UNLIMITED else round(cap, 2),
            "unlimited": cap == UNLIMITED,
            "inherited": stored is None,
            "spent_usd": round(used, 4),
            "remaining_usd": None if cap == UNLIMITED else round(max(cap - used, 0.0), 4),
            "exhausted": cap != UNLIMITED and used >= cap,
        }
    return out


class BudgetExceeded(Exception):
    """Raised when work cannot start. The message is shown to the user as-is."""


def _reset_phrase(now: float | None = None) -> str:
    when = dt.datetime.fromtimestamp(week_end(now))
    return when.strftime("%a %d %b at %H:%M")


def require(username: str | None, budget: str, now: float | None = None) -> None:
    """Refuse to start work when the week's allowance is already gone.

    Deliberately checked against the cap and not the grace ceiling: grace is for
    finishing something already in flight, not for starting something new on an
    empty budget.
    """
    cap = effective(username, budget)
    if cap == UNLIMITED:
        return
    used = spent(username, budget, now)
    if used < cap:
        return
    what = "questions" if budget == "ask" else "indexing"
    if cap == 0:
        # Not "spent $0.00 of $0.00" — nothing was spent, the allowance is nil.
        raise BudgetExceeded(
            f"This account has no budget for {what}. An administrator can grant one "
            f"under Admin → Accounts."
        )
    raise BudgetExceeded(
        f"Weekly {what} budget spent: ${used:,.2f} of ${cap:,.2f}. "
        f"It resets {_reset_phrase(now)}. An administrator can raise it under "
        f"Admin → Accounts."
    )


def exhausted(username: str | None, budget: str, now: float | None = None) -> bool:
    """True when work in flight on this budget must stop. No grace — `index` only."""
    cap = effective(username, budget)
    if cap == UNLIMITED:
        return False
    return spent(username, budget, now) >= cap


CONTINUE, GRACE, STOP = "continue", "grace", "stop"


def turn_decision(
    username: str | None, *, holding_grace: bool = False, ref: str | None = None
) -> tuple[str, str]:
    """Whether a running answer may take another turn, and what to say about it.

    The whole rule in one place, because it is easy to get subtly wrong. An earlier
    version compared spend against a ceiling that already had the grace folded in,
    which quietly gave every answer a 10% overrun and never consumed the grace at
    all.

    Returns one of ``CONTINUE`` (under the cap, or already holding the grace and
    still inside it), ``GRACE`` (just claimed this week's overrun — the caller
    should remember it is holding it), or ``STOP``.
    """
    cap = effective(username, "ask")
    if cap == UNLIMITED:
        return CONTINUE, ""
    used = spent(username, "ask")
    if used < cap:
        return CONTINUE, ""

    limit = graced_cap(cap)
    if holding_grace:
        if used < limit:
            return CONTINUE, ""
        return STOP, _stop_message(cap, used, graced=True)

    if used < limit and username and claim_grace(username, ref=ref):
        extra = limit - cap
        broker.log(
            f"{username} claimed the weekly budget overrun (${extra:,.2f}) to finish an answer.",
            level="warn",
            source="budget",
            context={"username": username, "cap_usd": round(cap, 2),
                     "grace_usd": round(extra, 4), "spent_usd": round(used, 4), "ref": ref},
        )
        return GRACE, (
            f"Weekly question budget of ${cap:,.2f} reached. Using this week's one-time "
            f"${extra:,.2f} overrun to finish this answer — the next one will stop instead."
        )

    return STOP, _stop_message(cap, used, graced=False)


def _stop_message(cap: float, used: float, *, graced: bool) -> str:
    tail = (
        " The one-time overrun for this week is already spent."
        if graced else
        " This week's one-time overrun has already been used."
    )
    return (
        f"Stopping here: ${used:,.2f} of the ${cap:,.2f} weekly question budget is spent."
        f"{tail} It resets {_reset_phrase()}."
    )
