"""SQLite-backed refresh-token revocation/rotation store.

Design (Item 5 of Weekend MVP v3.1):

    Each refresh token has a random ``jti`` embedded in its JWT claims. The
    store persists that jti keyed to its user + expiry, and tracks rotation
    lineage via ``superseded_by``. On every refresh we mint a NEW pair and
    mark the old refresh row as superseded. A token presented after
    supersession (past the grace window) is treated as evidence of theft
    — we revoke the entire chain (walk ``superseded_by`` pointers forward)
    so the attacker AND the legitimate user are both logged out and must
    re-authenticate.

    References:
      * OAuth 2.0 Security BCP (draft-ietf-oauth-security-topics) §4.13
        "Refresh Token Protection"
      * Auth0 "Refresh Token Rotation" writeup

Concurrency:

    aiosqlite wraps the synchronous ``sqlite3`` module in a background
    thread. A single long-lived Connection is maintained and guarded by an
    ``asyncio.Lock`` so that rotate() — which is a multi-statement
    read-modify-write — is atomic with respect to any concurrent call into
    the store. sqlite3 has its own internal lock but that only protects
    individual statements, not logical transactions.

Schema:

    refresh_tokens (
      jti             TEXT PRIMARY KEY,   -- random token id (JWT claim)
      user            TEXT NOT NULL,      -- sub/user claim ("claudetunnel_user")
      exp             INTEGER NOT NULL,   -- unix ts of JWT exp
      revoked         INTEGER DEFAULT 0,  -- 1 = explicit revocation (logout / reuse)
      superseded_by   TEXT,               -- jti of the replacement token
      superseded_at   INTEGER             -- unix ts of rotation
    )

    Index on ``exp`` for the purge loop's range scan.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import List, Optional

import aiosqlite
import structlog

logger = structlog.get_logger()


# Keep superseded/revoked rows around for one day after expiry so the purge
# loop catches stragglers and we retain enough state for post-mortem on
# suspected token theft. Expiry + 1 day is well past any client's retry
# window, so nothing legitimate is still referencing these rows.
_PURGE_GRACE_SECONDS = 86400


class RefreshStore:
    """Async SQLite-backed jti store for refresh-token rotation."""

    def __init__(self, db_path: str):
        # Resolve the path eagerly so a bad path blows up at construction
        # rather than on first write. We do not create the parent dir here;
        # the lifespan wires that up before calling init().
        self._db_path = str(Path(db_path).expanduser())
        self._conn: Optional[aiosqlite.Connection] = None
        # Single connection behind a lock. SQLite serializes writes anyway
        # but we want atomic read-modify-write inside rotate() and the
        # single connection avoids connection-pool tuning for the MVP.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def init(self) -> None:
        """Open the connection and create the schema if missing."""
        self._conn = await aiosqlite.connect(self._db_path)
        # foreign_keys off by default in SQLite — we don't need them here,
        # just noting the defaults so they're not surprising.
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                jti TEXT PRIMARY KEY,
                user TEXT NOT NULL,
                exp INTEGER NOT NULL,
                revoked INTEGER DEFAULT 0,
                superseded_by TEXT,
                superseded_at INTEGER
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_exp "
            "ON refresh_tokens(exp)"
        )
        await self._conn.commit()
        # Tighten perms on the SQLite DB — it stores refresh-token jtis.
        # The file is guaranteed to exist here because CREATE TABLE + commit
        # just ran. Idempotent: safe to chmod every startup even if already
        # 0600. Wrapped in try/except OSError for defensive safety (e.g.
        # read-only FS, unusual ownership).
        try:
            Path(self._db_path).chmod(0o600)
        except OSError as e:
            logger.warning("refresh_store_chmod_failed", db=self._db_path, err=str(e))
        logger.info("refresh_store_initialized", db=self._db_path)

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------
    async def issue(self, jti: str, user: str, exp: int) -> None:
        """Persist a freshly-minted refresh token."""
        assert self._conn is not None, "RefreshStore.init() not called"
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO refresh_tokens (jti, user, exp) VALUES (?, ?, ?)",
                (jti, user, int(exp)),
            )
            await self._conn.commit()

    async def rotate(
        self,
        old_jti: str,
        new_jti: str,
        user: str,
        new_exp: int,
    ) -> bool:
        """Atomically rotate an old refresh token to a new one.

        Returns:
            True  — rotation succeeded; old token is now superseded.
            False — old token is unknown, revoked, or already superseded
                    past the grace window (caller treats as reuse-attack).

        Caller is responsible for chain revocation on the False path.
        """
        assert self._conn is not None, "RefreshStore.init() not called"
        now = int(time.time())
        async with self._lock:
            # Load the old row under the lock so nothing changes under us.
            cur = await self._conn.execute(
                "SELECT revoked, superseded_by, superseded_at, exp "
                "FROM refresh_tokens WHERE jti = ?",
                (old_jti,),
            )
            row = await cur.fetchone()
            await cur.close()

            if row is None:
                # Unknown jti. Could be forged, could be from before a
                # server restart that wiped the store. Either way: reject.
                logger.warning("refresh_rotate_unknown_jti", jti=old_jti)
                return False

            revoked, superseded_by, _superseded_at, old_exp = row

            if revoked:
                logger.warning("refresh_rotate_revoked", jti=old_jti)
                return False
            if superseded_by is not None:
                # Already rotated once. Real client either (a) fired two
                # refreshes in parallel (covered by the grace window on
                # is_valid) or (b) something is replaying an old token.
                # rotate() itself does NOT grant grace — grace is for
                # is_valid checks only, because rotating the SAME old
                # token twice would create a malformed chain.
                logger.warning(
                    "refresh_rotate_already_superseded",
                    jti=old_jti,
                    superseded_by=superseded_by,
                )
                return False
            if old_exp <= now:
                # Expired tokens are not eligible for rotation — client
                # must start a new session via TOTP.
                logger.warning("refresh_rotate_expired", jti=old_jti)
                return False

            # All good. Mark the old row superseded and insert the new one.
            # BEGIN IMMEDIATE would add explicit transaction scoping, but
            # aiosqlite's autocommit default + a single commit at the end
            # is equivalent for SQLite's serialized-writer model.
            await self._conn.execute(
                "UPDATE refresh_tokens "
                "SET superseded_by = ?, superseded_at = ? "
                "WHERE jti = ?",
                (new_jti, now, old_jti),
            )
            await self._conn.execute(
                "INSERT INTO refresh_tokens (jti, user, exp) VALUES (?, ?, ?)",
                (new_jti, user, int(new_exp)),
            )
            await self._conn.commit()
            logger.info(
                "refresh_rotated",
                old_jti=old_jti[:8] + "…",
                new_jti=new_jti[:8] + "…",
            )
            return True

    async def revoke(self, jti: str) -> None:
        """Explicitly revoke a refresh token (logout or reuse-detect)."""
        assert self._conn is not None, "RefreshStore.init() not called"
        async with self._lock:
            await self._conn.execute(
                "UPDATE refresh_tokens SET revoked = 1 WHERE jti = ?",
                (jti,),
            )
            await self._conn.commit()
            logger.info("refresh_revoked", jti=jti[:8] + "…")

    async def revoke_chain(self, start_jti: str) -> int:
        """Walk ``superseded_by`` forward from start_jti and revoke every row.

        This is the reuse-detection hammer: when a refresh token that has
        already been superseded comes back in, we assume the attacker got
        hold of a snapshot. Revoking every descendant logs out both parties
        and forces TOTP re-auth. Also revokes ``start_jti`` itself.

        Returns:
            Count of rows revoked (for logging / test assertions).
        """
        assert self._conn is not None, "RefreshStore.init() not called"
        count = 0
        async with self._lock:
            # Always revoke start_jti (the root of the chain).
            current: Optional[str] = start_jti
            visited: set[str] = set()
            while current is not None and current not in visited:
                visited.add(current)
                await self._conn.execute(
                    "UPDATE refresh_tokens SET revoked = 1 WHERE jti = ?",
                    (current,),
                )
                count += 1
                cur = await self._conn.execute(
                    "SELECT superseded_by FROM refresh_tokens WHERE jti = ?",
                    (current,),
                )
                row = await cur.fetchone()
                await cur.close()
                current = row[0] if row and row[0] else None
            await self._conn.commit()
        logger.warning("refresh_chain_revoked", start=start_jti[:8] + "…", count=count)
        return count

    async def is_valid(self, jti: str, grace_seconds: int = 10) -> bool:
        """Decide whether a refresh jti is currently acceptable.

        Acceptable means:
          * exists in the store,
          * not explicitly revoked,
          * not expired,
          * not superseded, OR superseded within the grace window.

        The grace window exists so near-simultaneous refresh attempts (the
        classic two-tab / double-submit scenario) don't both trigger
        reuse-detection. After the grace window closes, any presentation
        of the old token IS reuse.
        """
        assert self._conn is not None, "RefreshStore.init() not called"
        now = int(time.time())
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT revoked, superseded_at, exp FROM refresh_tokens "
                "WHERE jti = ?",
                (jti,),
            )
            row = await cur.fetchone()
            await cur.close()

        if row is None:
            return False
        revoked, superseded_at, exp = row
        if revoked:
            return False
        if exp <= now:
            return False
        if superseded_at is not None and (now - superseded_at) >= grace_seconds:
            return False
        return True

    async def is_superseded(self, jti: str) -> bool:
        """True if this jti exists AND has a non-null superseded_at."""
        assert self._conn is not None, "RefreshStore.init() not called"
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT superseded_at FROM refresh_tokens WHERE jti = ?",
                (jti,),
            )
            row = await cur.fetchone()
            await cur.close()
        return row is not None and row[0] is not None

    async def purge_expired(self) -> int:
        """Delete rows whose ``exp`` is more than one day in the past.

        Called on a background loop every 6 hours from main.lifespan. The
        grace cushion means the store retains enough state to diagnose
        reuse attempts that arrive slightly after token expiry, without
        unbounded growth.

        Returns:
            Count of deleted rows (for logging).
        """
        assert self._conn is not None, "RefreshStore.init() not called"
        cutoff = int(time.time()) - _PURGE_GRACE_SECONDS
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM refresh_tokens WHERE exp < ?",
                (cutoff,),
            )
            deleted = cur.rowcount or 0
            await cur.close()
            await self._conn.commit()
        if deleted:
            logger.info("refresh_purge", deleted=deleted, cutoff=cutoff)
        return deleted

    # Internal — exposed for tests only.
    async def _all_jtis(self) -> List[str]:  # pragma: no cover - test helper
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute("SELECT jti FROM refresh_tokens")
            rows = await cur.fetchall()
            await cur.close()
        return [r[0] for r in rows]
