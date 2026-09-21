import os
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from app.schemas.bundle import BundleDTO, BundleCategoryDTO
from app.services.bundle_service import BundleService
from tests.mocks import get_bundle_mock, get_region_mocks


# Patch get_region_mocks to accept any arguments


def get_region_mocks(*args, **kwargs):
    class Region:
        def __init__(self, name, region_code, guid=None, icon=None):
            self.name = name
            self.region_code = region_code
            self.guid = guid or f"guid_{region_code}"
            self.icon = icon

    return [Region('Europe', 'EUROPE'), Region('Asia', 'ASIA')]


def get_bundle_mock(*args, **kwargs):
    return BundleDTO(
        bundle_code='TEST123',
        original_price=10.0,
        price=10.0,
        currency_code='USD',
        price_display='10.00 USD',
        display_title='Test Bundle',
        display_subtitle='Subtitle',
        bundle_category=BundleCategoryDTO(type='data', title='Data Category', code='CAT1'),
        bundle_marketing_name='Marketing Name',
        bundle_name='Bundle Name',
        count_countries=1,
        gprs_limit_display='1GB',
        unlimited=False,
        validity=30,
        validity_display='30 days',
        countries=[]
    )


class TestBundleService(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DEFAULT_CURRENCY"] = "EUR"
        os.environ["SUPABASE_URL"] = "None"
        os.environ["SUPABASE_KEY"] = "KEY"
        os.environ["STRIPE_PUBLIC_KEY"] = "PK"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "WEBHOOK_SECRET"
        os.environ["STRIPE_SECRET_KEY"] = "SECRET_KEY"

    @patch('app.repo.bundle_tage_repo.BundleTagRepo')
    @patch('app.repo.notification_repo.NotificationRepo')
    @patch('app.repo.user_order_repo.UserProfileBundleRepo')
    @patch('app.repo.user_order_repo.UserProfileRepo')
    @patch('app.repo.user_order_repo.UserRepo')
    @patch('app.repo.user_order_repo.UserOrderRepo')
    @patch('app.repo.tag_repo.TagRepo')
    @patch('app.repo.bundle_repo.BundleRepo')
    @patch('app.services.bundle_service.GroupingService')
    @patch('app.services.bundle_service.esim_hub_service_instance')
    def setUp(self, mock_esim_hub, mock_grouping, mock_bundle_repo, mock_tag_repo,
              mock_user_order_repo, mock_user_repo, mock_user_profile_repo, mock_user_profile_bundle_repo,
              mock_notification_repo, mock_bundle_tag_repo):
        self.mock_esim_hub_service = mock_esim_hub.return_value
        self.mock_grouping_service = mock_grouping.return_value
        self.mock_bundle_repo = mock_bundle_repo.return_value
        self.mock_tag_repo = mock_tag_repo.return_value
        self.mock_user_order_repo = mock_user_order_repo.return_value
        self.mock_user_repo = mock_user_repo.return_value
        self.mock_user_profile_repo = mock_user_profile_repo.return_value
        self.mock_user_profile_bundle_repo = mock_user_profile_bundle_repo.return_value
        self.mock_notification_repo = mock_notification_repo.return_value
        self.mock_bundle_tag_repo = mock_bundle_tag_repo.return_value

        # Setup default returns
        self.mock_bundle_repo.get_by_id.return_value = MagicMock()
        self.mock_bundle_repo.get_first_by.return_value = MagicMock()
        self.mock_bundle_tag_repo.list.return_value = []
        self.mock_tag_repo.get_first_by.return_value = MagicMock(tag_group_id=1)
        self.mock_esim_hub_service.get_bundle = AsyncMock(side_effect=get_bundle_mock)
        self.mock_grouping_service.get_all_regions = AsyncMock(side_effect=get_region_mocks)
        self.mock_grouping_service.get_all_countries = AsyncMock(return_value=[])

        self.bundle_service = BundleService()
        self.bundle_service._BundleService__esim_hub_service = self.mock_esim_hub_service
        self.bundle_service._BundleService__grouping_service = self.mock_grouping_service
        self.bundle_service._BundleService__bundle_repo = self.mock_bundle_repo
        self.bundle_service._BundleService__tag_repo = self.mock_tag_repo
        self.bundle_service._BundleService__bundle_tag_repo = self.mock_bundle_tag_repo
        self.bundle_service._BundleService__user_order_repo = self.mock_user_order_repo
        # The fulfilment lock guards the non-idempotent hub call; these tests exercise the happy path.
        self.bundle_service._BundleService__fulfilment_lock = MagicMock()
        self.bundle_service._BundleService__fulfilment_lock.acquire.return_value = True
        self.bundle_service._BundleService__user_repo = self.mock_user_repo
        self.bundle_service._BundleService__user_profile_repo = self.mock_user_profile_repo
        self.bundle_service._BundleService__user_profile_bundle_repo = self.mock_user_profile_bundle_repo
        self.bundle_service._BundleService__notification_repo = self.mock_notification_repo

    def test_bundle_exists_true(self):
        self.mock_bundle_repo.get_by_id.return_value = MagicMock()
        result = self.bundle_service.bundle_exists("test_bundle")
        self.assertTrue(result)

    def test_bundle_exists_false(self):
        self.mock_bundle_repo.get_by_id.return_value = None
        result = self.bundle_service.bundle_exists("test_bundle")
        self.assertFalse(result)

    def test_get_bundle_by_id(self):
        mock_bundle = MagicMock()
        self.mock_bundle_repo.get_by_id.return_value = mock_bundle
        result = self.bundle_service.get_bundle_by_id("test_bundle")
        self.assertEqual(result, mock_bundle)

    def test_get_bundle(self):
        bundle = BundleDTO(
            bundle_code='TEST123',
            original_price=10.0,
            price=10.0,
            currency_code='USD',
            price_display='10.00 USD',
            display_title='Test Bundle',
            display_subtitle='Subtitle',
            bundle_category=BundleCategoryDTO(type='data', title='Data Category', code='CAT1'),
            bundle_marketing_name='Marketing Name',
            bundle_name='Bundle Name',
            count_countries=1,
            gprs_limit_display='1GB',
            unlimited=False,
            validity=30,
            validity_display='30 days',
            countries=[]
        )
        self.mock_bundle_repo.get_bundle_by_id.return_value = bundle
        self.bundle_service._BundleService__currency_service = MagicMock()
        self.bundle_service._BundleService__currency_service.get_rate_by_currency.return_value = 1.0
        response = self.bundle_service.get_bundle("test_bundle", "USD", "en")
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    async def test_get_regions(self):
        self.mock_grouping_service.get_all_regions.side_effect = get_region_mocks
        response = await self.bundle_service.get_regions("en")
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    def test_get_bundles_by_country(self):
        self.mock_bundle_repo.list.return_value = []
        self.mock_tag_repo.get_by_id.return_value = MagicMock(data={
            "id": "1",
            "alternative_country": "USA",
            "country": "United States",
            "country_code": "US",
            "iso3_code": "USA",
            "zone_name": "America",
            "name": "United States",
            "code": "US"
        })
        self.mock_tag_repo.list_in.return_value = [MagicMock(id="1")]
        self.mock_bundle_tag_repo.table.select.return_value.filter.return_value.execute.return_value = MagicMock(
            data=[])
        self.mock_bundle_repo.list_in.return_value = []
        self.bundle_service._BundleService__currency_service = MagicMock()
        self.bundle_service._BundleService__currency_service.get_rate_by_currency.return_value = 1.0
        response = self.bundle_service.get_bundles_by_country("US", "USD", "en")
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    async def test_get_bundles_by_region(self):
        self.mock_bundle_repo.list.return_value = []
        self.mock_grouping_service.get_all_regions.side_effect = get_region_mocks
        self.mock_bundle_repo.list_in.return_value = []
        self.bundle_service._BundleService__currency_service = MagicMock()
        self.bundle_service._BundleService__currency_service.get_rate_by_currency.return_value = 1.0
        response = await self.bundle_service.get_bundles_by_region("EUROPE", "USD", "en")
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    async def test_buy_bundle(self):
        from app.models.user import UserOrderModel
        from app.schemas.bundle import BundleDTO
        user_order = MagicMock(spec=UserOrderModel)
        user_order.currency = "USD"
        user_order.id = "order123"
        user_order.promo_code = None
        user_order.referral_code = None
        user_order.modified_amount = 1000
        user_order.user_id = "user123"
        user_order.searched_countries = "{}"
        bundle = MagicMock(spec=BundleDTO)
        bundle.bundle_code = "TEST123"
        bundle.bundle_name = "Test Bundle"
        bundle.model_dump.return_value = {"name": "test"}

        mock_user = MagicMock()
        mock_user.email = "test@example.com"
        mock_user.metadata = {"msisdn": "123456"}

        mock_esim_order = MagicMock()
        mock_esim_order.orderId = "esim123"
        mock_esim_order.iccid = "test_iccid"
        mock_esim_order.validityData = "30 days"
        mock_esim_order.smdpAdress = "smdp.test.com"
        mock_esim_order.activationCode = "AC123"
        mock_esim_order.allowTopup = True

        self.mock_esim_hub_service.create_reseller_order = AsyncMock(return_value=mock_esim_order)
        self.mock_user_repo.get_by_id.return_value = mock_user
        self.mock_user_profile_repo.create.return_value = MagicMock(id="profile123")
        self.mock_user_profile_bundle_repo.create.return_value = MagicMock()
        self.bundle_service._BundleService__currency_service = MagicMock()
        self.bundle_service._BundleService__currency_service.get_currency_rate.return_value = 1.0
        self.bundle_service._BundleService__promotion_service = MagicMock()
        self.bundle_service._BundleService__promotion_service.apply_promotion_code_after_purchase = AsyncMock()

        with patch('threading.Thread'):
            response = await self.bundle_service.buy_bundle(user_order, bundle, "user123", "completed", "rule123")
            self.assertEqual(response.status, "success")

    async def test_top_up_bundle(self):
        from app.models.user import UserOrderModel
        from app.schemas.bundle import BundleDTO
        user_order = MagicMock(spec=UserOrderModel)
        user_order.id = "order123"
        user_order.user_id = "user123"
        bundle = MagicMock(spec=BundleDTO)
        bundle.bundle_code = "TEST123"
        bundle.bundle_name = "Test Bundle"
        bundle.model_dump.return_value = {"name": "test"}

        mock_user = MagicMock()
        mock_user.email = "test@example.com"
        mock_user.metadata = {"msisdn": "123456"}

        mock_user_profile = MagicMock()
        mock_user_profile.esim_hub_order_id = "esim123"

        mock_primary_bundle = MagicMock()
        mock_primary_bundle.bundle_expired = False

        mock_esim_topup = MagicMock()
        mock_esim_topup.orderId = "topup123"

        self.mock_user_repo.get_by_id.return_value = mock_user
        self.mock_user_profile_repo.get_first_by.return_value = mock_user_profile
        self.mock_user_profile_bundle_repo.get_first_by.return_value = mock_primary_bundle
        self.mock_user_profile_bundle_repo.create.return_value = MagicMock()
        self.mock_esim_hub_service.create_reseller_topup = AsyncMock(return_value=mock_esim_topup)

        response = await self.bundle_service.top_up_bundle(bundle, user_order, "test_iccid", "user123", "completed")
        self.assertEqual(response.status, "success")


if __name__ == "__main__":
    unittest.main()
