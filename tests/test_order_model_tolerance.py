from app.models.user import UserOrderModel


def _row(**extra):
    row = {"id": "ord_1", "user_id": "usr_1", "amount": 131, "currency": "USD"}
    row.update(extra)
    return row


def test_order_reads_survive_the_new_column():
    # UserOrderModel forbids extra fields, so a column the model does not know about breaks
    # every read the moment it exists in the database.
    order = UserOrderModel(**_row(fulfilment_claimed_at=None))
    assert order.fulfilment_claimed_at is None
    assert UserOrderModel(**_row(fulfilment_claimed_at="2026-09-21T10:00:00+00:00"))


def test_whole_model_writes_do_not_carry_the_lock_column():
    order = UserOrderModel(**_row(fulfilment_claimed_at="2026-09-21T10:00:00+00:00"))
    written = order.model_dump(exclude={"id", "fulfilment_claimed_at"})
    assert "fulfilment_claimed_at" not in written, "a stale write must not release another worker's lock"
