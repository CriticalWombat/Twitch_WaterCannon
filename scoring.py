"""
scoring.py
----------
Soak-leaderboard scoring for the water cannon.

Pure Python + SQLite: no GPIO, no Twitch, no asyncio, so it can be tested on
its own. main.py calls Scoreboard.record() *after* a shot is queued, inside a
try/except, so scoring can never stop the cannon from firing.

Scoring rules
  - Each redemption adds its shots to the viewer's session score.
  - The first redemption of a session claims First Blood: +bonus points.
  - The king is whoever has the highest session score. Ties never dethrone:
    the viewer who reached that score first keeps the crown.
  - Events are deduped by event ID (Twitch can redeliver).
  - Viewers are keyed by user ID; the latest display name is stored beside it.

Session rules
  - stream.online calls start_session(); stream.offline calls end_session().
  - The first redemption starts a session if none is open, so scoring works
    even if the online event never arrives or arrives late.
  - A late online event keeps the open session if it saw activity within the
    grace window; otherwise that session is stale and is closed first.
  - A session idle for longer than idle_hours (a missed offline event) is
    closed automatically by the next redemption.
  - Closing a session crowns the king as winner, adds one win, and builds a
    recap (podium, totals, First Blood, longest reign).
"""

import functools
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

log = logging.getLogger("water_cannon.scoring")

SEEN_EVENT_TTL_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewers (
    user_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    lifetime_shots INTEGER NOT NULL DEFAULT 0,
    wins           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    seq            INTEGER NOT NULL DEFAULT 0,
    first_blood_id TEXT,
    king_id        TEXT,
    winner_id      TEXT,
    last_event_at  TEXT
);
CREATE TABLE IF NOT EXISTS session_scores (
    session_id  INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    score       INTEGER NOT NULL,
    reached_seq INTEGER NOT NULL,
    PRIMARY KEY (session_id, user_id)
);
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    seen_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reigns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    user_id    TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);
"""

# Columns added after the first release; applied to databases created before them.
MIGRATIONS = [
    ("viewers",  "wins",          "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "winner_id",     "TEXT"),
    ("sessions", "last_event_at", "TEXT"),
    ("sessions", "locked_at",     "TEXT"),
]


@dataclass
class Dethrone:
    old_id: str
    old_name: str
    new_id: str
    new_name: str
    margin: int  # how many points the new king leads the old king by


@dataclass
class Standing:
    user_id: str
    name: str
    score: int


@dataclass
class Reign:
    user_id: str
    name: str
    seconds: float


@dataclass
class Recap:
    session_id: int
    started_at: str
    ended_at: str
    winner: Standing
    winner_wins: int                 # the winner's career wins, including this one
    podium: List[Standing]           # top 3, best first (winner is first)
    participants: int
    total_points: int
    first_blood_name: Optional[str]
    longest_reign: Optional[Reign]


@dataclass
class StartResult:
    session_id: int
    created: bool                    # False if an open session was kept
    closed: Optional[Recap]          # recap of a stale session closed to make room


@dataclass
class Result:
    session_id: int
    user_id: str
    name: str
    shots: int          # shots paid for by this redemption
    points: int         # shots plus any First Blood bonus
    score: int          # the viewer's session score after this redemption
    first_blood: bool   # this redemption claimed First Blood
    king_id: str
    king_name: str
    lead: int           # the king's lead over second place (whole score if alone)
    dethrone: Optional[Dethrone]
    closed_stale: Optional[Recap] = None  # an idle session closed to start this one
    locked: bool = False  # board was locked: shot fired, score unchanged


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _locked(method):
    """Run a public method under the scoreboard lock (the overlay server and the
    scoring worker thread share one connection)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Scoreboard:
    def __init__(self, path: str, first_blood_bonus: int = 1, idle_hours: float = 3,
                 clock: Callable[[], datetime] = _utcnow):
        self._bonus = first_blood_bonus
        self._idle = timedelta(hours=idle_hours)
        self._clock = clock
        # Used from the event loop and from a worker thread; _lock serialises access.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(SCHEMA)
            for table, column, decl in MIGRATIONS:
                have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            cutoff = (self._clock() - timedelta(days=SEEN_EVENT_TTL_DAYS)).isoformat()
            self._db.execute("DELETE FROM seen_events WHERE seen_at < ?", (cutoff,))

    @_locked
    def close(self) -> None:
        self._db.close()

    # -- helpers ------------------------------------------------------------
    def _first_sight(self, event_id: Optional[str]) -> bool:
        """Mark an event seen. False if it was already seen (a redelivery)."""
        if not event_id:
            return True
        cur = self._db.execute(
            "INSERT OR IGNORE INTO seen_events (event_id, seen_at) VALUES (?, ?)",
            (event_id, self._clock().isoformat()),
        )
        if cur.rowcount == 0:
            log.info("Duplicate event %s ignored", event_id)
            return False
        return True

    def _open_row(self) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def _insert_session(self, now: datetime) -> sqlite3.Row:
        stamp = now.isoformat()
        cur = self._db.execute(
            "INSERT INTO sessions (started_at, last_event_at) VALUES (?, ?)", (stamp, stamp)
        )
        log.info("Started session %d", cur.lastrowid)
        return self._db.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone()

    def _last_activity(self, row: sqlite3.Row) -> datetime:
        return datetime.fromisoformat(row["last_event_at"] or row["started_at"])

    def _standing_rows(self, sid: int) -> List[sqlite3.Row]:
        return self._db.execute(
            "SELECT s.user_id, v.display_name, s.score "
            "FROM session_scores s JOIN viewers v USING (user_id) "
            "WHERE s.session_id = ? ORDER BY s.score DESC, s.reached_seq ASC",
            (sid,),
        ).fetchall()

    def _finish(self, row: sqlite3.Row, now: datetime, lock_only: bool = False) -> Optional[Recap]:
        """Close a session: crown the winner, count the win, build the recap.

        Returns None for a session nobody scored in. Runs inside the caller's
        transaction.
        """
        sid = row["id"]
        stamp = now.isoformat()
        if row["locked_at"] and not lock_only:
            # Winner was crowned and counted at the reveal: just close the session.
            self._db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (stamp, sid))
            log.info("Closed session %d (winner already revealed)", sid)
            return None
        standings = self._standing_rows(sid)
        self._db.execute(
            "UPDATE reigns SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
            (stamp, sid),
        )
        winner_id = row["king_id"] or (standings[0]["user_id"] if standings else None)
        column = "locked_at" if lock_only else "ended_at"
        self._db.execute(
            f"UPDATE sessions SET {column} = ?, winner_id = ? WHERE id = ?",
            (stamp, winner_id, sid),
        )
        if not standings:
            log.info("Closed empty session %d", sid)
            return None

        self._db.execute("UPDATE viewers SET wins = wins + 1 WHERE user_id = ?", (winner_id,))
        wins = self._db.execute(
            "SELECT wins FROM viewers WHERE user_id = ?", (winner_id,)
        ).fetchone()["wins"]
        winner = next(s for s in standings if s["user_id"] == winner_id)

        first_blood = None
        if row["first_blood_id"]:
            fb = self._db.execute(
                "SELECT display_name FROM viewers WHERE user_id = ?", (row["first_blood_id"],)
            ).fetchone()
            first_blood = fb["display_name"] if fb else None

        longest = None
        for r in self._db.execute(
            "SELECT r.user_id, v.display_name, r.started_at, r.ended_at "
            "FROM reigns r JOIN viewers v USING (user_id) WHERE r.session_id = ?",
            (sid,),
        ):
            secs = (datetime.fromisoformat(r["ended_at"])
                    - datetime.fromisoformat(r["started_at"])).total_seconds()
            if longest is None or secs > longest.seconds:
                longest = Reign(r["user_id"], r["display_name"], secs)

        log.info("%s session %d, winner %s", "Locked" if lock_only else "Closed",
                 sid, winner["display_name"])
        return Recap(
            session_id=sid, started_at=row["started_at"], ended_at=stamp,
            winner=Standing(winner["user_id"], winner["display_name"], winner["score"]),
            winner_wins=wins,
            podium=[Standing(s["user_id"], s["display_name"], s["score"]) for s in standings[:3]],
            participants=len(standings),
            total_points=sum(s["score"] for s in standings),
            first_blood_name=first_blood,
            longest_reign=longest,
        )

    # -- sessions -----------------------------------------------------------
    @_locked
    def start_session(self, event_id: Optional[str] = None,
                      grace_minutes: float = 180) -> StartResult:
        """Handle stream.online.

        Keeps the open session if it is empty or saw activity within the grace
        window (the online event just arrived late). Otherwise that session is
        stale: it is closed, and a fresh one starts. A session whose winner was
        already revealed (locked) is always closed.
        """
        with self._db:
            now = self._clock()
            row = self._open_row()
            if not self._first_sight(event_id):
                return StartResult(row["id"] if row else 0, False, None)
            closed = None
            if row is not None:
                has_scores = self._db.execute(
                    "SELECT 1 FROM session_scores WHERE session_id = ? LIMIT 1", (row["id"],)
                ).fetchone()
                fresh = now - self._last_activity(row) <= timedelta(minutes=grace_minutes)
                # A locked (revealed) session is finished: a new stream must not inherit it.
                if not row["locked_at"] and (not has_scores or fresh):
                    log.info("Online event: keeping open session %d", row["id"])
                    return StartResult(row["id"], False, None)
                closed = self._finish(row, now)
            return StartResult(self._insert_session(now)["id"], True, closed)

    @_locked
    def lock_session(self) -> Optional[Recap]:
        """The reveal: crown the winner, count the win and freeze tonight's scores.

        The session stays open, so shots keep scoring toward lifetime totals and
        the cannon keeps firing, but the standings no longer change. Returns
        None if no session is open, nobody has scored yet, or it is already locked.
        """
        with self._db:
            row = self._open_row()
            if row is None or row["locked_at"]:
                return None
            has_scores = self._db.execute(
                "SELECT 1 FROM session_scores WHERE session_id = ? LIMIT 1", (row["id"],)
            ).fetchone()
            if not has_scores:
                return None
            return self._finish(row, self._clock(), lock_only=True)

    @_locked
    def end_session(self, event_id: Optional[str] = None) -> Optional[Recap]:
        """Handle stream.offline: close the open session and return its recap.

        None if no session is open, the session was empty, or the event was a
        redelivery.
        """
        with self._db:
            if not self._first_sight(event_id):
                return None
            row = self._open_row()
            if row is None:
                log.info("Offline event: no open session")
                return None
            return self._finish(row, self._clock())

    @_locked
    def standings(self, limit: Optional[int] = None) -> List[tuple]:
        """(user_id, display_name, score) for the open session, best first."""
        row = self._open_row()
        if row is None:
            return []
        rows = self._standing_rows(row["id"])
        if limit is not None:
            rows = rows[:limit]
        return [(r["user_id"], r["display_name"], r["score"]) for r in rows]

    @_locked
    def snapshot(self, top_n: int = 5) -> dict:
        """The open session as plain data for the overlay (JSON-serialisable)."""
        row = self._open_row()
        if row is None:
            return {"active": False, "session_id": None, "standings": [],
                    "king": None, "first_blood_id": None, "locked": False}
        rows = self._standing_rows(row["id"])
        king = None
        if rows:
            second = rows[1]["score"] if len(rows) > 1 else 0
            king = {"id": rows[0]["user_id"], "name": rows[0]["display_name"],
                    "score": rows[0]["score"], "lead": rows[0]["score"] - second}
        return {
            "active": True,
            "session_id": row["id"],
            "standings": [{"id": r["user_id"], "name": r["display_name"], "score": r["score"]}
                          for r in rows[:top_n]],
            "king": king,
            "first_blood_id": row["first_blood_id"],
            "locked": bool(row["locked_at"]),
        }

    # -- scoring ------------------------------------------------------------
    @_locked
    def record(self, event_id: Optional[str], user_id: str, display_name: str,
               shots: int) -> Optional[Result]:
        """Score one redemption. Returns None if the event was already seen.

        The dedupe marker and the score change share one transaction, so a
        crash can never mark an event seen without scoring it.
        """
        with self._db:
            if not self._first_sight(event_id):
                return None

            now = self._clock()
            stamp = now.isoformat()

            session = self._open_row()
            closed_stale = None
            if session is not None and now - self._last_activity(session) > self._idle:
                log.warning("Session %d idle since %s – closing it as stale",
                            session["id"], session["last_event_at"] or session["started_at"])
                closed_stale = self._finish(session, now)
                session = None
            if session is None:
                session = self._insert_session(now)
            sid = session["id"]

            if session["locked_at"]:
                self._db.execute(
                    "INSERT INTO viewers (user_id, display_name, lifetime_shots) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name, "
                    "lifetime_shots = lifetime_shots + excluded.lifetime_shots",
                    (user_id, display_name, shots),
                )
                top = self._standing_rows(sid)[:2]
                king = top[0]
                mine = self._db.execute(
                    "SELECT score FROM session_scores WHERE session_id = ? AND user_id = ?",
                    (sid, user_id),
                ).fetchone()
                return Result(
                    session_id=sid, user_id=user_id, name=display_name, shots=shots,
                    points=0, score=mine["score"] if mine else 0, first_blood=False,
                    king_id=king["user_id"], king_name=king["display_name"],
                    lead=king["score"] - (top[1]["score"] if len(top) > 1 else 0),
                    dethrone=None, closed_stale=closed_stale, locked=True,
                )

            self._db.execute(
                "INSERT INTO viewers (user_id, display_name, lifetime_shots) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name, "
                "lifetime_shots = lifetime_shots + excluded.lifetime_shots",
                (user_id, display_name, shots),
            )

            seq = session["seq"] + 1
            points = shots
            first_blood = session["first_blood_id"] is None
            if first_blood:
                points += self._bonus
                self._db.execute(
                    "UPDATE sessions SET first_blood_id = ? WHERE id = ?", (user_id, sid)
                )

            self._db.execute(
                "INSERT INTO session_scores (session_id, user_id, score, reached_seq) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, user_id) DO UPDATE SET "
                "score = score + excluded.score, reached_seq = excluded.reached_seq",
                (sid, user_id, points, seq),
            )
            self._db.execute(
                "UPDATE sessions SET seq = ?, last_event_at = ? WHERE id = ?",
                (seq, stamp, sid),
            )

            top = self._standing_rows(sid)[:2]
            king = top[0]
            lead = king["score"] - (top[1]["score"] if len(top) > 1 else 0)

            dethrone = None
            old_king_id = session["king_id"]
            if old_king_id != king["user_id"]:
                if old_king_id is not None:
                    old = self._db.execute(
                        "SELECT v.display_name, s.score FROM viewers v "
                        "JOIN session_scores s ON s.user_id = v.user_id AND s.session_id = ? "
                        "WHERE v.user_id = ?",
                        (sid, old_king_id),
                    ).fetchone()
                    dethrone = Dethrone(
                        old_id=old_king_id,
                        old_name=old["display_name"],
                        new_id=king["user_id"],
                        new_name=king["display_name"],
                        margin=king["score"] - old["score"],
                    )
                self._db.execute(
                    "UPDATE sessions SET king_id = ? WHERE id = ?", (king["user_id"], sid)
                )
                self._db.execute(
                    "UPDATE reigns SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
                    (stamp, sid),
                )
                self._db.execute(
                    "INSERT INTO reigns (session_id, user_id, started_at) VALUES (?, ?, ?)",
                    (sid, king["user_id"], stamp),
                )

            me = self._db.execute(
                "SELECT score FROM session_scores WHERE session_id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()

        return Result(
            session_id=sid, user_id=user_id, name=display_name, shots=shots,
            points=points, score=me["score"], first_blood=first_blood,
            king_id=king["user_id"], king_name=king["display_name"],
            lead=lead, dethrone=dethrone, closed_stale=closed_stale,
        )
