import os
from typing import List, Literal, Optional, Dict, Any, Union

import httpx
from loguru import logger

from app.config.api import EsimHubEndpoint
from app.config.helper import get_config
from app.exceptions import EsimHubUnknownOutcomeError, EsimHubException
from app.models.user import UsersCopyModel
from app.schemas.app import ExchangeRate
from app.schemas.bundle import ConsumptionResponse
from app.schemas.dto_mapper import DtoMapper
from app.schemas.esim_hub import EsimHubOrderResponse, GlobalConfigurationResponse, ContentResponse
from app.schemas.home import RegionDTO, CountryDTO, BundleDTO, AllBundleResponse


class EsimHubService:

    def __init__(self, base_url: str, api_key: str, tenant_key: str):
        self.__api_key = api_key
        self.__base_url = base_url
        self.__tenant_key = tenant_key

        self.__limits = httpx.Limits(
            max_keepalive_connections=int(os.getenv("MAX_KEEPALIVE_CONNECTIONS", 100)),
            max_connections=int(os.getenv("MAX_CONNECTIONS", 200)),
        )
        self.__timeout = httpx.Timeout(120.0)

    async def get_regions(self) -> List[RegionDTO]:
        params = {
            "pageIndex": 1,
            "pageSize": 300,
            "Name": "",
            "IsoCode": "",
            "CountryCode": ""
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_REGIONS, params=params)
        if "success" not in response:
            raise EsimHubException(response)
        regions = []
        for region in response["data"]["zones"]:
            try:
                regions.append(DtoMapper.to_region_dto(region))
            except Exception as e:
                logger.error(f"error while mapping region : {str(e)}")
                continue
        return regions

    async def health_check(self):
        try:
            configurations_status = await self.__do_request(method="GET", path="/configuration")
        except Exception as e:
            logger.error(f"error while getting configurations status {e}")
            configurations_status = {"success": False}
        try:
            catalog_status = await self.__do_request(method="GET", path="/catalog")
        except Exception as e:
            logger.error(f"error while getting catalog status {e}")
            catalog_status = {"success": False}
        try:
            core_status = await self.__do_request(method="GET", path="/core", base_url=os.getenv("ESIM_HUB_BASE_URL2"))
        except Exception as e:
            logger.error(f"error while getting core status {e}")
            core_status = {"success": False}
        return {
            "configurations_status": "ok" if "success" in configurations_status and configurations_status[
                "success"] else "failed",
            "catalog_status": "ok" if "success" in catalog_status and catalog_status["success"] else "failed",
            "core_status": "ok" if "success" in core_status and core_status["success"] else "failed",
        }

    async def get_countries(self) -> List[CountryDTO]:
        params = {
            "pageIndex": 1,
            "pageSize": 300,
            "Name": "",
            "IsoCode": "",
            "CountryCode": ""
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_COUNTRIES,
                                           base_url=os.getenv("ESIM_HUB_BASE_URL2"), params=params)
        if "success" not in response:
            raise EsimHubException(response)
        countries = []
        for country in response["data"]["countries"]:
            try:
                countries.append(DtoMapper.to_country_dto(country))
            except Exception as e:
                logger.error(f"error while mapping country : {str(e)}")
                continue
        return countries

    async def get_all_bundles(self, page_index: int = 1, page_size: int = 100,
                              currency_code=os.getenv("DEFAULT_CURRENCY")) -> AllBundleResponse:
        params = {
            "pageIndex": page_index,
            "pageSize": page_size,
            "CurrencyCode": currency_code,
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_ALL_BUNDLES, params=params)
        if "success" not in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle: {str(e)}")
                continue
        return AllBundleResponse(total_rows=response["data"]["totalRows"], bundles=bundles)

    async def get_bundles_by_category(self, category: str, currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> List[
        BundleDTO]:
        params = {
            "CategoryTags": category,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_BUNDLES_BY_CATEGORY,
                                           params=params)
        if "success" not in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle: {str(e)}")
                continue
        return bundles

    async def get_bundles_by_zone(self, zone: str, currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> List[
        BundleDTO]:
        params = {
            "ZoneGuids": zone,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_BUNDLES_BY_ZONE, params=params)
        logger.debug(response)
        if "success" not in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle : {str(e)}")
                continue
        return bundles

    async def get_activation_code(self, order_id: str) -> str | None:
        params = {
            "orderId": order_id,
        }
        try:
            response = await self.__do_request("GET", EsimHubEndpoint.API_GET_ACTIVATION_CODE, params=params,
                                               base_url=os.getenv("ESIM_HUB_BASE_URL2"))
            if "success" not in response:
                logger.error(f"error while getting activation code {response}")
                return None
            return response["data"]["activationCode"]
        except Exception as e:
            logger.error(f"error while getting activation code : {str(e)}")
            return None

    async def create_reseller_order(self, bundle_code: str, order_id: str, user: UsersCopyModel,
                                    payment_type: str = "", discount_amount: float = 0, discount_rate: float = 0,
                                    new_price: float = 0,
                                    bundle_type: Literal[
                                        "COUNTRY", "CRUISE"] = "COUNTRY") -> EsimHubOrderResponse | None:
        request_body = {
            "BundleGuid": bundle_code,
            "Quantity": 1,
            "UniqueIdentifier": order_id,
            "ServiceTag": "ESIM",
            "PhoneNumber": user.metadata.get("msisdn", ""),
            "ClientName": user.metadata.get("first_name", "") + " " + user.metadata.get("last_name", ""),
            "Email": user.metadata.get("display_email", user.email),
            "PaymentMethod": payment_type,
            "DiscountAmount": discount_amount,
            "DiscountRate": discount_rate,
            "NewPrice": new_price,
            "BundleType": bundle_type

        }
        try:
            response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_CREATE_RESELLER_ORDER,
                                               body=request_body,
                                               base_url=os.getenv("ESIM_HUB_BASE_URL2"))
            logger.debug(f"request body: {request_body}")
            logger.debug(f"response: {response}")
            if "success" not in response or response["success"] == False:
                # An explicit rejection: nothing was created, so the caller may safely retry.
                logger.error("Failed to create reseller Hub: {}".format(response.get("message")))
                return None
            response_data = response["data"]
            esim_order_id = response_data["orderId"]
            try:
                response_data["activationCode"] = await self.get_activation_code(esim_order_id)
                return EsimHubOrderResponse.model_validate(response_data)
            except Exception as e:
                # The order EXISTS at this point. Losing it here must never look like "not created".
                raise EsimHubUnknownOutcomeError(
                    f"order {esim_order_id} created but could not be read back: {e}")
        except EsimHubUnknownOutcomeError:
            raise
        except Exception as e:
            # Timeout, connection reset, 5xx: the request may well have reached the hub.
            logger.error(f"Failed to create reseller order (outcome unknown): {str(e)}")
            raise EsimHubUnknownOutcomeError(str(e))

    async def create_reseller_topup(self, bundle_code: str, esim_hub_order_id,
                                    order_id: str, user: UsersCopyModel,
                                    payment_type: str = "",
                                    bundle_type: Literal[
                                        "COUNTRY", "CRUISE"] = "COUNTRY") -> EsimHubOrderResponse | None:
        request_body = {
            "BundleGuid": bundle_code,
            "OrderId": esim_hub_order_id,
            "UniqueIdentifier": order_id,
            "ServiceTag": "ESIM",
            "PhoneNumber": user.metadata.get("msisdn", ""),
            "ClientName": user.metadata.get("first_name", "") + " " + user.metadata.get("last_name", ""),
            "Email": user.metadata.get("display_email", user.email),
            "PaymentMethod": payment_type,
            "DiscountAmount": None,
            "DiscountRate": None,
            "NewPrice": None,
            "BundleType": bundle_type

        }
        response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_CREATE_RESELLER_TOPUP,
                                           body=request_body,
                                           base_url=os.getenv("ESIM_HUB_BASE_URL2"))
        logger.debug(f"request body: {request_body}")
        logger.debug(f"response: {response}")
        logger.info(response)
        if "success" not in response:
            logger.error("Failed to create reseller Hub: {}".format(response))
            return None
        return EsimHubOrderResponse.model_validate(response["data"])

    async def get_bundles_by_country(self, country_codes: List[str],
                                     currency_code: str = os.getenv("DEFAULT_CURRENCY", "EUR")) -> \
            List[BundleDTO]:
        params = {
            "CountryGuids": country_codes,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_SEARCH_BUNDLES_BY_COUNTRY,
                                           params=params)
        if "success" not in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle : {str(e)}")
                continue
        return bundles

    async def get_topup_related_bundles(self, order_id: str, currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> \
            List[BundleDTO]:
        params = {
            "orderId": order_id,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET",
                                           path=EsimHubEndpoint.API_GET_TOPUP_RELATED_BUNDLES,
                                           base_url=os.getenv("ESIM_HUB_BASE_URL2"), params=params)
        if "success" not in response:
            raise EsimHubException(response)
        # Handle invalid order id or empty items
        if not response["success"] or not response["data"] or not response["data"].get("items"):
            return []
        bundles = []
        for bundle in response["data"]["items"]:
            bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
        return bundles

    async def get_bundle_by_id(self, bundle_id: str,
                               currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> BundleDTO | None:
        params = {
            "RecordGuid": bundle_id,
            "CurrencyCode": currency_code
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_RESELLER_BUNDLE_BY_ID,
                                           params=params)
        if "success" not in response:
            raise EsimHubException(response)
        items = response["data"].get("items", [])
        if not items:
            return None
        if len(items) == 0:
            return None
        data = response["data"]["items"][0]
        return DtoMapper.to_bundle_dto(bundle=data, currency=currency_code)

    async def get_bundle_consumption(self, order_id: str) -> ConsumptionResponse:
        params = {
            "orderId": order_id
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_BUNDLE_CONSUMPTION,
                                           base_url=os.getenv("ESIM_HUB_CONSUMPTION_API_URL"), params=params)
        logger.debug(f"get bundle consumption: {response}")
        logger.info(f"get bundle consumption: {response}")
        if "success" not in response:
            raise EsimHubException(response)
        return DtoMapper.to_consumption_response(dict(response["data"]))

    async def get_global_configurations(self) -> List[GlobalConfigurationResponse]:
        params = {
            "Keys": "CATALOG.BUNDLES_CACHE_VERSION"
        }
        try:
            response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_GLOBAL_CONFIGURATIONS,
                                               params=params)
            if "success" not in response:
                logger.error("Failed to get global configurations: {}".format(response))
                return []

            logger.debug(response["data"]["globalConfigurations"])
            return [GlobalConfigurationResponse.model_validate(data) for data in
                    response["data"]["globalConfigurations"]]
        except Exception as e:
            logger.error("Failed to get global configurations: {}".format(e))
            raise EsimHubException(str(e))

    async def get_content_tag(self,
                              tag: Literal["TERM_CONDITION", "ABOUT_US", "ADS", "FAQ", "PRIVACY_POLICY"],
                              lang_code: str = "en") -> ContentResponse:
        body_request = {
            "Tag": tag
        }
        try:
            response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_GET_CONTENT_TAG,
                                               body=body_request, headers={"LanguageCode": lang_code})
            logger.debug("success" in response)
            if "success" not in response:
                raise EsimHubException(response)
            return ContentResponse.model_validate(response["data"]["item"])
        except Exception as e:
            logger.error("Failed to get content tag: {}".format(e))
            raise EsimHubException(e)

    async def get_content_tags(self,
                               tag: Literal["TERM_CONDITION", "ABOUT_US", "ADS", "FAQ", "PRIVACY_POLICY"],
                               lang_code: str = "en") -> List[ContentResponse]:
        body_request = {
            "Tag": tag
        }
        try:
            response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_GET_CONTENT_TAGS,
                                               body=body_request, headers={"LanguageCode": lang_code})
            logger.debug(response)
            if "success" not in response:
                raise EsimHubException(response)
            return [ContentResponse.model_validate(item) for item in response["data"]["items"]]
        except Exception as e:
            logger.error("Failed to get content tag: {}".format(e))
            raise EsimHubException(e)

    async def get_exchange_rates(self, currency_codes: List[str]) -> List[ExchangeRate]:
        params = {
            "CurrencyCodes": currency_codes,
            "ResellerGuid": get_config("RESELLER_ID", ""),
        }
        try:
            response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_EXCHANGE_RATE, params=params)
            if "success" not in response:
                logger.error("Failed to get exchange rates: {}".format(response))
                return []
            return [DtoMapper.to_exchange_rate(data) for data in response["data"]["items"]]
        except Exception as e:
            logger.error("Failed to get exchange rates: {}".format(e))
            return []

    async def __do_request(self,
                           method: Literal["GET", "OPTIONS", "HEAD", "POST", "PUT", "PATCH", "DELETE"], path: str,
                           headers: Optional[Dict[str, str]] = None,
                           params: Optional[Dict[str, str]] | Optional[Dict[str, List[str]]] = None,
                           body: Optional[Any] = None,
                           base_url=None) -> Union[Dict[str, Any], List[Any], EsimHubException]:
        if headers is None:
            headers = {}
        if base_url is None:
            base_url = self.__base_url
        try:
            headers["Tenant"] = self.__tenant_key
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"
            headers["Api-Key"] = self.__api_key

            # Create a per-request AsyncClient to avoid cross-thread/event-loop reuse
            async with httpx.AsyncClient(limits=self.__limits, timeout=self.__timeout) as client:
                response = await client.request(
                    method=method,
                    url=base_url + path,
                    headers=headers,
                    params=params,
                    json=body
                )

            logger.debug("Request: curl -X {} {} {} Response: {}".format(method, base_url + path, " ".join(
                [f'--header "{key}: {value}"' for key, value in headers.items()]), response))
            if response.status_code != httpx.codes.OK:
                try:
                    json_response = response.json()
                    raise EsimHubException(
                        json_response["message"] if "message" in json_response else str(json_response))
                except Exception as e:
                    logger.debug(f"failed to parse response as JSON: {str(e)}")
                    raise EsimHubException(f"eSIM Hub API request failed: {response.status_code}")
            return response.json()
        except Exception as e:
            logger.error(f"Error during eSIM Hub API request ({base_url + path}): {str(e)}")
            # Raise CustomException as is, otherwise wrap in EsimHubException
            from app.exceptions import CustomException
            if isinstance(e, CustomException):
                raise e
            raise EsimHubException(str(e))

    async def check_bundle_applicable(self, bundle_id: str) -> bool:
        try:
            params = {
                "bundleCode": bundle_id
            }
            response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_CHECK_BUNDLE_APPLICABLE,
                                               params=params, base_url=os.getenv("ESIM_HUB_BASE_URL2"))
            if "success" not in response:
                return False
            return True
        except Exception as e:
            logger.error("Failed to check bundle applicable: {}".format(e))
            return False
