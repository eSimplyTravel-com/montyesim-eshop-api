from app.config.db import DatabaseTables
from app.models.user import UserOrderModel, UserProfileModel, UserProfileBundleModel, UsersCopyModel
from app.repo.base_repo import BaseRepository


class UserOrderRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_ORDER, UserOrderModel)


class OrderFulfilmentLock:
    """Exactly-one-worker lock around the non-idempotent eSIM Hub call.

    The lock never expires by itself. An order left mid-fulfilment stays locked until a human
    checks the Monty portal and releases it, because a timer that hands the order to a second
    worker is how one payment becomes two eSIMs.
    """

    def __init__(self, repo: "UserOrderRepo"):
        self.__repo = repo

    def acquire(self, order_id: str) -> bool:
        return bool(self.__repo.client.rpc("claim_order_fulfilment", {
            "p_order_id": str(order_id),
        }).execute().data)


class UserProfileRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_PROFILE, UserProfileModel)


class UserProfileBundleRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_PROFILE_BUNDLE, UserProfileBundleModel)


class UserRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_COPY, UsersCopyModel)

    def referral_code_key(self):
        return "metadata ->> referral_code"
