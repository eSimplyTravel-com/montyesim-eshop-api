from typing import List

from app.config.db import DatabaseTables
from app.models.app import StripeEventModel
from app.repo.base_repo import BaseRepository

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"


class StripeEventRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_STRIPE_EVENT, StripeEventModel)

    def record(self, event_id: str, event_type: str) -> bool:
        """Insert the event. Returns False when Stripe delivered it before (primary key clash)."""
        try:
            self.create({"id": event_id, "type": event_type, "status": STATUS_PENDING, "attempts": 0})
            return True
        except Exception:
            return False

    def claim(self, event_id: str) -> bool:
        """Move one event to processing. Returns False if another worker already holds it."""
        rows = self.update_by(where={"id": event_id, "status": STATUS_PENDING}, data={"status": STATUS_PROCESSING})
        return bool(rows)

    def claim_failed(self, event_id: str) -> bool:
        rows = self.update_by(where={"id": event_id, "status": STATUS_FAILED}, data={"status": STATUS_PROCESSING})
        return bool(rows)

    def mark_processed(self, event_id: str) -> None:
        self.update_by(where={"id": event_id},
                       data={"status": STATUS_PROCESSED, "processed_at": "now()", "last_error": None})

    def mark_failed(self, event_id: str, attempts: int, error: str) -> None:
        self.update_by(where={"id": event_id},
                       data={"status": STATUS_FAILED, "attempts": attempts, "last_error": str(error)[:1000]})

    def get_attempts(self, event_id: str) -> int:
        event = self.get_by_id(event_id)
        return int(event.attempts or 0) if event else 0

    def list_retryable(self, max_attempts: int, limit: int = 20) -> List[StripeEventModel]:
        """Events left behind by a crash (processing/pending) or a failed attempt."""
        stuck: List[StripeEventModel] = []
        for status in (STATUS_FAILED, STATUS_PENDING, STATUS_PROCESSING):
            for event in self.list(where={"status": status}, limit=limit, order_by="created_at"):
                if int(event.attempts or 0) < max_attempts:
                    stuck.append(event)
        return stuck[:limit]
