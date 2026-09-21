import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app.config.db import DatabaseTables
from app.exceptions import DatabaseException
from app.models.app import StripeEventModel
from app.repo.base_repo import BaseRepository

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"

# Marks a row whose worker never came back (deploy, crash, OOM) as safe to pick up again.
LEASE_SECONDS = 15 * 60
RETRYABLE_STATUSES = [STATUS_PENDING, STATUS_FAILED]


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class DuplicateEventError(Exception):
    """Stripe delivered an event we already recorded."""


class StripeEventRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_STRIPE_EVENT, StripeEventModel)

    def record(self, event_id: str, event_type: str, payload: str, api_version: str = None) -> None:
        """Persist the event. Raises DuplicateEventError on redelivery, DatabaseException otherwise.

        The distinction matters: a duplicate is safe to acknowledge, any other failure means the
        event is NOT stored and Stripe must be asked to send it again.
        """
        try:
            self.table.insert({
                "id": event_id,
                "type": event_type,
                "payload": payload,
                "api_version": api_version,
                "status": STATUS_PENDING,
                "attempts": 0,
                "next_attempt_at": _now().isoformat(),
            }).execute()
        except Exception as e:
            # Only an existing row with this id proves a duplicate; any other constraint or
            # transport error must stay retryable, or we would acknowledge a lost event.
            if self.__looks_like_conflict(e) and self.__exists(event_id):
                raise DuplicateEventError(event_id)
            raise DatabaseException(str(e))

    @staticmethod
    def __looks_like_conflict(error: Exception) -> bool:
        code = getattr(error, "code", None)
        message = f"{getattr(error, 'message', '')} {error}".lower()
        return code == "23505" or "duplicate key" in message or "already exists" in message

    def __exists(self, event_id: str) -> bool:
        try:
            return bool(self.table.select("id").eq("id", event_id).execute().data)
        except Exception:
            return False

    def claim(self, event_id: str, max_attempts: int) -> Optional[str]:
        """Take ownership of one event. Returns a token, or None if it is not claimable.

        The work is done by the claim_stripe_event() SQL function: one statement, the database
        clock, and the attempt increment together, so two workers cannot consume one attempt.
        """
        token = str(uuid.uuid4())
        try:
            claimed = self.client.rpc("claim_stripe_event", {
                "p_event_id": event_id,
                "p_max_attempts": max_attempts,
                "p_token": token,
                "p_lease_seconds": LEASE_SECONDS,
            }).execute().data
        except Exception as e:
            raise DatabaseException(str(e))
        return token if claimed else None

    def mark_processed(self, event_id: str, token: str) -> None:
        # claimed_by guards against a superseded worker overwriting a newer result.
        self.table.update({"status": STATUS_PROCESSED, "processed_at": _now().isoformat(),
                           "last_error": None}) \
            .eq("id", event_id).eq("claimed_by", token).execute()

    def mark_failed(self, event_id: str, token: str, error: str, max_attempts: int,
                    backoff_seconds: int) -> str:
        """Schedule a retry, or park the event as dead once the budget is spent."""
        current = self.get_by_id(event_id)
        attempts = int(current.attempts or 0) if current else max_attempts
        status = STATUS_DEAD if attempts >= max_attempts else STATUS_FAILED
        self.table.update({
            "status": status,
            "last_error": str(error)[:1000],
            "next_attempt_at": (_now() + timedelta(seconds=backoff_seconds * attempts)).isoformat(),
        }).eq("id", event_id).eq("claimed_by", token).execute()
        return status

    def list_abandoned(self, max_attempts: int, limit: int = 20) -> List[StripeEventModel]:
        """Rows whose worker died on the last permitted attempt: they can never be claimed again,
        so nothing would ever move them to dead."""
        expired_before = (_now() - timedelta(seconds=LEASE_SECONDS)).isoformat()
        rows = self.table.select("*").eq("status", STATUS_PROCESSING) \
            .gte("attempts", max_attempts).lt("claimed_at", expired_before) \
            .order("claimed_at").limit(limit).execute().data or []
        return [StripeEventModel(**row) for row in rows]

    def mark_dead(self, event_id: str, token: str, error: str) -> bool:
        """Only park the exact row we selected: if its worker finished or someone re-claimed it,
        the filters match nothing and we leave it alone."""
        rows = self.table.update({"status": STATUS_DEAD, "last_error": str(error)[:1000]}) \
            .eq("id", event_id).eq("status", STATUS_PROCESSING).eq("claimed_by", token) \
            .execute().data
        return bool(rows)

    def list_retryable(self, max_attempts: int, limit: int = 20) -> List[StripeEventModel]:
        """Events that are due for another run. Filtering happens in the database, so a batch of
        exhausted rows cannot hide newer ones."""
        try:
            expired_before = (_now() - timedelta(seconds=LEASE_SECONDS)).isoformat()
            due = _now().isoformat()
            rows = self.table.select("*") \
                .in_("status", RETRYABLE_STATUSES) \
                .lt("attempts", max_attempts) \
                .lte("next_attempt_at", due) \
                .order("next_attempt_at").limit(limit).execute().data or []
            stale = self.table.select("*") \
                .eq("status", STATUS_PROCESSING) \
                .lt("attempts", max_attempts) \
                .lt("claimed_at", expired_before) \
                .order("claimed_at").limit(limit).execute().data or []
            # Interleave so a steady stream of due rows cannot starve crash recovery.
            merged, half = [], max(1, limit // 2)
            merged.extend(rows[:limit - min(len(stale), half)])
            merged.extend(stale[:limit - len(merged)])
            return [StripeEventModel(**row) for row in merged][:limit]
        except Exception as e:
            raise DatabaseException(str(e))

    def payload_of(self, event_id: str) -> Tuple[Optional[str], Optional[str]]:
        rows = self.table.select("payload,api_version").eq("id", event_id).execute().data or []
        if not rows:
            return None, None
        return rows[0].get("payload"), rows[0].get("api_version")
