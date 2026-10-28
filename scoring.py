"""
scoring.py
----------
Soak-leaderboard scoring for the water cannon.

Pure Python + SQLite: no GPIO, no Twitch, no asyncio, so it can be tested on
its own. main.py calls Scoreboard.record() *after* a shot is queued, inside a
try/except, so scoring can never stop the cannon from firing.

Rules
  - Each redemption adds its shots to the viewer's session score.
  - The first redemption of a session claims First Blood: +bonus points.
  - The king is whoever has the highest session score. Ties never dethrone:
    the viewer who reached that score first keeps the crown.
  - Events are deduped by event ID (Twitch can redeliver).
  - Viewers are keyed by user ID; the latest display name is stored beside it.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

log = logging.getLogger("water_cannon.scoring")

SEEN_EVENT_TTL_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewers (
    user_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    lifetime_shots INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    seq            INTEGER NOT NULL DEFAULT 0,
    first_blood_id TEXT,
    king_id        TEXT
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
"""


@dataclass
class Dethrone:
    old_id: str
    old_name: str
    new_id: str
    new_name: str
    margin: int  # how many points the new king leads the old king by


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Scoreboard:
    def __init__(self, path: str, first_blood_bonus: int = 1):
        self._bonus = first_blood_bonus
        # Called from a worker thread (never concurrently: the caller awaits each call).
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(SCHEMA)
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=SEEN_EVENT_TTL_DAYS)).isoformat()
            self._db.execute("DELETE FROM seen_events WHERE seen_at < ?", (cutoff,))

    def close(self) -> None:
        self._db.close()

    # -- sessions -----------------------------------------------------------
    def _open_session(self) -> sqlite3.Row:
        """Return the open session, starting one if none is open.

        The first redemption starts a session, so scoring works even if a
        stream-online event never arrives.
        """
        row = self._db.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            cur = self._db.execute("INSERT INTO sessions (started_at) VALUES (?)", (_now(),))
            row = self._db.execute(
                "SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            log.info("Started session %d", row["id"])
        return row

    def standings(self, limit: Optional[int] = None) -> List[Tuple[str, str, int]]:
        """(user_id, display_name, score) for the open session, best first."""
        row = self._db.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return []
        sql = (
            "SELECT s.user_id, v.display_name, s.score "
            "FROM session_scores s JOIN viewers v USING (user_id) "
            "WHERE s.session_id = ? ORDER BY s.score DESC, s.reached_seq ASC"
        )
        args: tuple = (row["id"],)
        if limit is not None:
            sql += " LIMIT ?"
            args += (limit,)
        return [(r[0], r[1], r[2]) for r in self._db.execute(sql, args)]

    # -- scoring ------------------------------------------------------------
    def record(self, event_id: Optional[str], user_id: str, display_name: str,
               shots: int) -> Optional[Result]:
        """Score one redemption. Returns None if the event was already seen.

        The dedupe marker and the score change share one transaction, so a
        crash can never mark an event seen without scoring it.
        """
        with self._db:
            if event_id:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO seen_events (event_id, seen_at) VALUES (?, ?)",
                    (event_id, _now()),
                )
                if cur.rowcount == 0:
                    log.info("Duplicate event %s ignored", event_id)
                    return None

            session = self._open_session()
            sid = session["id"]

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
            self._db.execute("UPDATE sessions SET seq = ? WHERE id = ?", (seq, sid))

            top = self._db.execute(
                "SELECT s.user_id, v.display_name, s.score "
                "FROM session_scores s JOIN viewers v USING (user_id) "
                "WHERE s.session_id = ? ORDER BY s.score DESC, s.reached_seq ASC LIMIT 2",
                (sid,),
            ).fetchall()
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

            me = self._db.execute(
                "SELECT score FROM session_scores WHERE session_id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()

        return Result(
            session_id=sid, user_id=user_id, name=display_name, shots=shots,
            points=points, score=me["score"], first_blood=first_blood,
            king_id=king["user_id"], king_name=king["display_name"],
            lead=lead, dethrone=dethrone,
        )
