import asyncio
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List

import bleach
import stripe
from fastapi import Request
from loguru import logger
from soupsieve.util import lower

from app.config.config import esim_hub_service_instance, generate_otp, dcb_service_instance, supabase_client
from app.config.constants import ErrorMessages, PaymentStatusEnum, UserWalletTransactionSource
from app.config.db import DatabaseTables, PaymentTypeEnum, ConfigKeysEnum
from app.config.helper import get_config
from app.config.utils import create_payment_intent, create_payment_ephemeral, stripe_get_payment_details, \
    truncate_two_decimals_decimal
from app.exceptions import BadRequestException, CustomException
from app.models.user import UserModel, UserOrderType, OrderStatusEnum, UserOrderModel, UsersCopyModel, UserWalletModel, \
    UserProfileModel, UserProfileBundleModel
from app.repo import NotificationRepo, UserOrderRepo, UserProfileRepo, UserProfileBundleRepo, UserRepo
from app.repo.bundle_repo import BundleTranslationRepo
from app.schemas.app import UserNotificationResponse
from app.schemas.bundle import AssignRequest, AssignTopUpRequest, PaymentIntentResponse, EsimBundleResponse, \
    ConsumptionResponse, UserOrderHistoryResponse, UpdateBundleLabelRequest, VerifyOtpRequestDto
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.response import Response, ResponseHelper
from app.services.bundle_service import BundleService
from app.services.currency_service import CurrencyService
from app.services.promotion_service import PromotionService
from app.services.task_executor import TaskExecutor
from app.services.user_wallet_service import UserWalletService


class UserBundleService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__notification_repo = NotificationRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()
        self.__user_profile_bundle_repo = UserProfileBundleRepo()
        self.__user_wallet_service = UserWalletService()
        self.__promotion_service = PromotionService()
        self.__bundle_service = BundleService()
        self.__dcb_service = dcb_service_instance()
        self.__currency_service = CurrencyService()
        self.__user_repo = UserRepo()
        self.__task_executor = TaskExecutor()
        self.__bundle_translation_repo = BundleTranslationRepo()

    async def assign(self, user: UserModel, device_id: str, assign_request: AssignRequest, x_currency: str,
                     locale: str, request: Request) -> Response[PaymentIntentResponse] | Response[bool]:

        bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id=assign_request.bundle_code)
        if not bundle or not bundle.is_active:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                  details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        if not bundle.is_stockable:
            check_bundle_available = await self.__esim_hub_service.check_bundle_applicable(bundle.bundle_info_code)
            if not check_bundle_available:
                raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                      details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        modified_amount = bundle.original_price
        amount = bundle.original_price
        rule_id = "0"

        data = {
            "user_id": user.id,
            "bundle_id": assign_request.bundle_code,
            "order_type": UserOrderType.ASSIGN,
            "amount": int(round(amount * 100)),
            "modified_amount": int(round(modified_amount * 100)),
            "currency": "USD",
            "bundle_data": bundle.model_dump_json(),
            "searched_countries": assign_request.related_search.model_dump_json(),
            "anonymous_user_id": user.anonymous_user_id,
            "promo_code": assign_request.promo_code or None,
            "payment_type": assign_request.payment_type,
        }
        if assign_request.promo_code and self.__promotion_service.is_referral_code(assign_request.promo_code):
            data.setdefault("referral_code", assign_request.promo_code)
            data.pop("promo_code")

        order = self.__user_order_repo.create(data)

        if assign_request.promo_code:
            if self.__promotion_service.is_referral_code(assign_request.promo_code):
                self.__check_if_user_eligible_for_referral(user=user, promo_code=assign_request.promo_code)
            validation_response = await self.__promotion_service.validate_promo_code(code=assign_request.promo_code,
                                                                                     user_id=user.id, bundle=bundle,
                                                                                     device_id=device_id,
                                                                                     currency=x_currency,
                                                                                     apply_usage=True,
                                                                                     order_id=order.id)
            logger.info(f"applying promo code {assign_request.promo_code} with {validation_response.message}")
            bundle = validation_response.bundle
            # /validation runs bundle_currency_update after discounting; the assign path
            # must too, or the discounted price_display serialised into bundle_data is a
            # USD number labelled with the request currency (wrong string in receipts).
            rate = self.__currency_service.get_rate_by_currency(x_currency)
            bundle = DtoMapper.bundle_currency_update(bundle=bundle, rate=rate, currency=x_currency)
            modified_amount = bundle.original_price
            rule_id = validation_response.rule_id
            order.modified_amount = round(modified_amount * 100, 2)
            order.bundle_data = bundle.model_dump_json()
            logger.info(f"scheduling background update for order {order.id}")

            # Create background task for order update
            def task():
                self.__update_order_with_delay(
                    order_id=order.id,
                    bundle=bundle
                )

            self.__task_executor.add_task(task)

        payment_type = assign_request.payment_type

        if modified_amount == 0:
            self.__user_order_repo.update_by({"id": order.id}, data={"modified_amount": 0})
            await self.__bundle_service.buy_bundle(user_order=order, bundle=bundle, user_id=user.id,
                                                   payment_status=OrderStatusEnum.SUCCESS, rule_id=rule_id
                                                   , payment_type=payment_type)
            response = PaymentIntentResponse(order_id=order.id, payment_status=PaymentStatusEnum.COMPLETED)
            return ResponseHelper.success_data_response(response, 0)

        if payment_type == PaymentTypeEnum.WALLET:
            return await self.__handle_wallet_payment(user=user, bundle=bundle, user_order=order, rule_id=rule_id,
                                                      modified_amount=modified_amount)
        elif payment_type == PaymentTypeEnum.DCB:
            return await self.__handle_dcb_payment(user=user, bundle=bundle, user_order=order)
        elif payment_type == PaymentTypeEnum.CARD:
            return await self.__handle_card_payment(user=user, order=order, device_id=device_id,
                                                    assign_request=assign_request, rule_id=rule_id, request=request,
                                                    x_currency=x_currency)
        else:
            raise CustomException(code=400, name=ErrorMessages.INVALID_PAYMENT_TYPE,
                                  details=f"Payment type {payment_type} is not supported")

    async def assign_top_up(self, user: UserModel, assign_top_up_request: AssignTopUpRequest, device_id: str,
                            request: Request, x_currency: str, locale: str) -> Response:
        bundle_response = self.__bundle_service.get_bundle(bundle_id=assign_top_up_request.bundle_code,
                                                           currency_name=x_currency, locale=locale)
        bundle = bundle_response.data

        order = self.__user_order_repo.create({
            "user_id": user.id,
            "bundle_id": assign_top_up_request.bundle_code,
            "order_type": UserOrderType.BUNDLE_TOP_UP,
            "amount": round(bundle.original_price * 100),
            "modified_amount": round(bundle.original_price * 100),
            "currency": os.getenv("DEFAULT_CURRENCY"),
            "bundle_data": bundle.model_dump_json(),
            "searched_countries": None,
            "payment_type": assign_top_up_request.payment_type
        })

        payment_type = assign_top_up_request.payment_type

        if payment_type == PaymentTypeEnum.WALLET:
            return await self.__handle_wallet_payment(user=user, bundle=bundle, user_order=order,
                                                      iccid=assign_top_up_request.iccid,
                                                      modified_amount=bundle.original_price,
                                                      rule_id="")
        elif payment_type == PaymentTypeEnum.DCB:
            return await self.__handle_dcb_payment(user=user, bundle=bundle, user_order=order)
        elif payment_type == PaymentTypeEnum.CARD:
            return await self.__handle_card_payment(user=user, order=order, device_id=device_id,
                                                    assign_request=None, rule_id="0", request=request,
                                                    iccid=assign_top_up_request.iccid,
                                                    x_currency=x_currency)
        else:
            raise CustomException(code=400, name=ErrorMessages.INVALID_PAYMENT_TYPE,
                                  details=f"Payment type {payment_type} is not supported")

    async def get_user_esims(self, user: UserModel, x_currency: str, accept_language: str = "en") -> Response[
        List[EsimBundleResponse]]:
        # Fetch raw profile rows including joined bundles so we can robustly handle mapping
        user_profiles_raw = self.__user_profile_repo.select(
            tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
            where={"user_id": user.id},
            as_model=False)
        logger.debug(f"Fetched raw user profiles joined rows count={len(user_profiles_raw)} for user {user.id}")
        # Fallback: if the joined select returned nothing, try fetching bundles directly and synthesize profiles
        if not user_profiles_raw:
            logger.debug("No joined user_profile rows found, trying to fetch user_profile_bundle rows directly")
            bundles = self.__user_profile_bundle_repo.list(where={"user_id": user.id}, limit=100)
            logger.debug(f"Fetched {len(bundles)} user_profile_bundle rows for user {user.id}")
            # Group bundles by user_profile_id and construct minimal profile dicts
            profiles_map: dict = {}
            for b in bundles:
                pid = getattr(b, 'user_profile_id', None) or getattr(b, 'user_order_id', None) or 'default'
                if pid not in profiles_map:
                    profiles_map[pid] = {
                        "id": pid,
                        "user_id": b.user_id,
                        "user_order_id": b.user_order_id,
                        "iccid": b.iccid,
                        "validity": getattr(b, 'created_at', None),
                        "created_at": getattr(b, 'created_at', None),
                        "label": getattr(b, 'label', None),
                        "smdp_address": None,
                        "activation_code": None,
                        "allow_topup": False,
                        "esim_hub_order_id": getattr(b, 'esim_hub_order_id', None),
                        "searched_countries": None,
                        "user_profile_bundle": []
                    }
                # Append the raw bundle data dict to the joined key so model_validate will map it
                profiles_map[pid]["user_profile_bundle"].append(b.model_dump())
            user_profiles_raw = list(profiles_map.values())
            logger.debug(f"Synthesized {len(user_profiles_raw)} profile entries from bundles for user {user.id}")

        esim_bundle_response = []
        rate = self.__currency_service.get_rate_by_currency(x_currency)

        for profile_raw in user_profiles_raw:
            try:
                # Build pydantic model from raw data (handles alias 'user_profile_bundle' -> bundles)
                profile: UserProfileModel = UserProfileModel.model_validate(profile_raw)

                # Debug: log when bundles are missing so it's easier to trace empty responses
                raw_bundles = profile_raw.get("user_profile_bundle") if isinstance(profile_raw, dict) else None
                if not raw_bundles:
                    logger.debug(f"No bundles found in profile raw for user {user.id}: keys={list(profile_raw.keys())}")
                else:
                    logger.debug(f"Found {len(raw_bundles)} raw bundles for user {user.id}")

                # If pydantic didn't populate profile.bundles, try to build it from the raw join payload
                if not getattr(profile, 'bundles', None):
                    raw_bundles = profile_raw.get('user_profile_bundle') if isinstance(profile_raw, dict) else None
                    if raw_bundles:
                        try:
                            profile.bundles = [UserProfileBundleModel.model_validate(rb) for rb in raw_bundles]
                            logger.debug(
                                f"Populated profile.bundles from raw for user {user.id}, count={len(profile.bundles)}")
                        except Exception as e:
                            logger.debug(f"Failed to populate profile.bundles from raw: {e}")

                profile_current_bundle: UserProfileBundleModel = DtoMapper.get_profile_current_bundle(profile)
                if profile_current_bundle is None or profile_current_bundle.bundle_data is None:
                    logger.warning(f"Bundle data missing for user profile {getattr(profile, 'id', 'unknown')}")
                    continue

                bundle_data: BundleDTO = BundleDTO.model_validate(profile_current_bundle.bundle_data)

                translated_bundle = self.__bundle_translation_repo.get_first_by(where={
                    "bundle_id": bundle_data.bundle_code,
                    "locale": accept_language
                })
                if translated_bundle is not None:
                    bundle_data = BundleDTO.model_validate(translated_bundle.data)

                # Fetch related order to obtain tax amount for display calculations
                order_for_profile: UserOrderModel = self.__user_order_repo.get_by_id(record_id=profile.user_order_id)
                tax_amount = getattr(order_for_profile, 'tax_amount', 0) if order_for_profile else 0

                bundle = DtoMapper.to_esim_bundle_response(user_profile=profile, x_currency=x_currency, rate=rate,
                                                           tax=tax_amount, bundle_data=bundle_data)

                # Update display price for each transaction history item using the order's tax
                for history in bundle.transaction_history:
                    order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=history.user_order_id)
                    order_tax = getattr(order, 'tax_amount', 0) if order else 0
                    amount = float(history.bundle.original_price * rate) + float((order_tax / 100) * rate)
                    history.bundle.price_display = f"{round(amount, 2)} {x_currency}"

                # Ensure transaction history is ordered descending by created_at (newest first)
                try:
                    bundle.transaction_history = sorted(
                        bundle.transaction_history,
                        key=lambda h: int(h.created_at) if getattr(h, 'created_at', None) is not None else 0,
                        reverse=True
                    )
                except Exception:
                    # If created_at is not a timestamp yet or sorting fails, leave original order
                    pass

                if bundle is not None:
                    esim_bundle_response.append(bundle)
            except Exception as e:
                logger.error(
                    f"Failed to map profile (raw keys: {list(profile_raw.keys()) if isinstance(profile_raw, dict) else 'unknown'}): {e}")
        # Sort overall response by bundle payment_date descending (newest bundles first)
        try:
            esim_bundle_response = sorted(
                esim_bundle_response,
                key=lambda b: int(b.payment_date) if getattr(b, 'payment_date', None) is not None else 0,
                reverse=True,
            )
        except Exception:
            pass

        return ResponseHelper.success_data_response(esim_bundle_response, len(esim_bundle_response))

    async def get_user_esim(self, iccid: str, user: UserModel, x_currency: str, accept_language: str = "en") -> \
            Response[EsimBundleResponse | None]:
        user_profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                        where={"user_id": user.id, "iccid": iccid})
        if len(user_profiles) == 0:
            raise CustomException(code=404, name=ErrorMessages.USER_PROFILE_NOT_FOUND, details="user profile not found")
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        profile = user_profiles[0]
        # Ensure we have the current bundle and bundle_data
        profile_current_bundle = DtoMapper.get_profile_current_bundle(profile)
        if profile_current_bundle is None or profile_current_bundle.bundle_data is None:
            raise CustomException(code=404, name=ErrorMessages.USER_PROFILE_NOT_FOUND, details="user profile not found")
        bundle_data = BundleDTO.model_validate(profile_current_bundle.bundle_data)
        # Apply translation for requested locale when available
        translated_bundle = self.__bundle_translation_repo.get_first_by(where={"bundle_id": bundle_data.bundle_code,
                                                                               "locale": accept_language})
        if translated_bundle is not None and getattr(translated_bundle, 'data', None):
            bundle_data = BundleDTO.model_validate(translated_bundle.data)
        user_order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=profile.user_order_id)
        bundle = DtoMapper.to_esim_bundle_response(user_profile=profile, bundle_data=bundle_data, rate=rate,
                                                   x_currency=x_currency,
                                                   tax=user_order.tax_amount)
        for history in bundle.transaction_history:
            order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=history.user_order_id)
            amount = float(history.bundle.original_price * rate) + float((order.tax_amount / 100) * rate)
            history.bundle.price_display = f"{round(amount, 2)} {x_currency}"
        return ResponseHelper.success_data_response(bundle, 0)

    async def consumption(self, user: UserModel, iccid: str) -> Response[ConsumptionResponse]:
        started_bundle = self.__user_profile_bundle_repo.list(where={"user_id": user.id, "iccid": iccid,
                                                                     "bundle_expired": False, "plan_started": True
                                                                     }, limit=100)

        active_bundle = self.__user_profile_bundle_repo.get_first_by(where={"user_id": user.id, "iccid": iccid,
                                                                            "bundle_expired": False})

        profile = self.__user_profile_repo.get_first_by({"user_id": user.id, "iccid": iccid})
        if not profile:
            raise CustomException(code=400, name=ErrorMessages.USER_PROFILE_NOT_FOUND,
                                  details="user profile not found", )

        if started_bundle:
            latest_started_bundle = started_bundle[-1]
            logger.info(f"get started bundle consumption Order_id: { latest_started_bundle.esim_hub_order_id}")
            consumption = await self.__esim_hub_service.get_bundle_consumption(
                latest_started_bundle.esim_hub_order_id
            )
        elif active_bundle:
            logger.info(f"get started active bundle consumption Order_id: { active_bundle.esim_hub_order_id}")
            consumption = await self.__esim_hub_service.get_bundle_consumption(
                active_bundle.esim_hub_order_id
            )
        else:
            logger.info(f"get started bundle consumption Order_id: { profile.esim_hub_order_id}")
            consumption = await self.__esim_hub_service.get_bundle_consumption(
                profile.esim_hub_order_id
            )

        return ResponseHelper.success_data_response(consumption, 0)

    async def user_notifications(self, user: UserModel, page_index: int, page_size: int) -> Response[
        List[UserNotificationResponse]]:
        notifications = self.__notification_repo.list(where={"user_id": user.id}, limit=page_size,
                                                      offset=((page_index - 1) * page_size), order_by="created_at",
                                                      desc=True)
        return ResponseHelper.success_data_response(
            [DtoMapper.to_user_notification_response(data) for data in notifications], 1)

    async def read_user_notification(self, user: UserModel, device_id) -> Response:
        logger.info(f"read user notification for user {user.email=} {device_id=}")
        self.__notification_repo.update_by(where={"user_id": user.id}, data={"status": True})
        return ResponseHelper.success_response()

    async def bundle_exists(self, user_id: str, bundle_id: str) -> Response[bool]:
        orders = self.__user_order_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE: "*"}, where={
            "user_id": user_id,
            "bundle_id": bundle_id,
            "payment_status": "success",
            "order_status": "success",
            f"{DatabaseTables.TABLE_USER_PROFILE}.allow_topup": True
        }, as_model=False)
        if len(orders) == 0:
            return ResponseHelper.success_data_response(False, 0)
        if any(len(item.get("user_profile", [])) > 0 for item in orders):
            logger.info("At least one profile list is non-empty")
            return ResponseHelper.success_data_response(True, 0)
        return ResponseHelper.success_data_response(False, 0)

    async def update_bundle_name(self, code: str, bundle_label_request: UpdateBundleLabelRequest, user: UserModel):
        user_profile_bundle = self.__user_profile_bundle_repo.get_first_by(where={"user_id": user.id},
                                                                           filters={
                                                                               "bundle_data ->> bundle_code": code})
        if user_profile_bundle is None:
            raise CustomException(code=400, name=ErrorMessages.USER_PROFILE_BUNDLE_NOT_FOUND,
                                  details="Bundle Not Found")
        bundle = BundleDTO.model_validate(user_profile_bundle.bundle_data)
        bundle.label = bleach.clean(bundle_label_request.label)
        self.__user_profile_bundle_repo.update_by(
            where={"user_id": user.id}, filters={"bundle_data ->> bundle_code ": code},
            data={"bundle_data": bundle.model_dump()})
        return ResponseHelper.success_response()

    async def update_bundle_name_by_iccid(self, iccid: str, bundle_label_request: UpdateBundleLabelRequest,
                                          user: UserModel):
        user_profile_bundle = self.__user_profile_bundle_repo.get_first_by(where={"user_id": user.id, "iccid": iccid})
        if user_profile_bundle is None:
            raise CustomException(code=400, name=ErrorMessages.USER_PROFILE_BUNDLE_NOT_FOUND,
                                  details="Bundle Not Found")
        bundle = BundleDTO.model_validate(user_profile_bundle.bundle_data)
        bundle.label = bleach.clean(bundle_label_request.label)
        self.__user_profile_bundle_repo.update_by(
            where={"user_id": user.id, "iccid": iccid},
            data={"bundle_data": bundle.model_dump()})
        return ResponseHelper.success_response()

    async def get_topup_related_bundle(self, bundle_code: str, iccid: str, user: UserModel, accept_language: str = "en",
                                       currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> Response[
        List[BundleDTO]]:
        logger.info(f"get_topup_related_bundle {bundle_code=} {iccid=} {user=}")
        profile = self.__user_profile_repo.get_first_by({"user_id": user.id, "iccid": iccid})
        if not profile:
            raise BadRequestException(details="This ICCID is not linked to this user")
        bundles = await self.__esim_hub_service.get_topup_related_bundles(order_id=profile.esim_hub_order_id)
        all_bundles = []
        for bundle in bundles:
            if self.__bundle_service.bundle_exists(bundle.bundle_code):
                local_bundle = self.__bundle_service.get_bundle(bundle_id=bundle.bundle_code,
                                                                currency_name=currency_code,
                                                                locale=accept_language)
                logger.debug(f"local bundle {local_bundle.data}")
                all_bundles.append(local_bundle.data)

        return ResponseHelper.success_data_response(all_bundles, len(all_bundles))

    async def get_user_esim_by_order_id(self, order_id: str, user: UserModel, x_currency: str) -> Response[
        EsimBundleResponse]:
        user_order = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
        if not user_order:
            raise CustomException(code=404, name=ErrorMessages.ORDER_NOT_FOUND, details=ErrorMessages.ORDER_NOT_FOUND)
        if user_order.payment_status != OrderStatusEnum.SUCCESS:
            raise CustomException(code=400, name=f"{ErrorMessages.PAYMENT_}{user_order.payment_status.upper()}",
                                  details=ErrorMessages.PAYMENT_FAILED)
        if user_order.order_status != OrderStatusEnum.SUCCESS:
            raise CustomException(code=400, name=f"{ErrorMessages.ORDER_}{user_order.order_status.upper()}",
                                  details=ErrorMessages.ORDER_FAILED)
        profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                   where={"user_id": user.id, "user_order_id": order_id})
        if len(profiles) == 0:
            raise CustomException(code=404, name=ErrorMessages.USER_PROFILE_NOT_FOUND,
                                  details=ErrorMessages.ORDER_NOT_FOUND)
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        profile = profiles[0]
        # Ensure we have the current bundle and bundle_data
        profile_current_bundle = DtoMapper.get_profile_current_bundle(profile)
        if profile_current_bundle is None or profile_current_bundle.bundle_data is None:
            raise CustomException(code=404, name=ErrorMessages.USER_PROFILE_NOT_FOUND,
                                  details="No bundle data found for this profile")
        bundle_data = BundleDTO.model_validate(profile_current_bundle.bundle_data)
        user_order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=profile.user_order_id)
        translated_bundle = self.__bundle_translation_repo.get_first_by(where={"bundle_id": bundle_data.bundle_code,
                                                                               "locale": os.getenv('DEFAULT_LANGUAGE',
                                                                                                   'en')})
        if translated_bundle is not None:
            bundle_data = BundleDTO.model_validate(translated_bundle.data)
        bundle = DtoMapper.to_esim_bundle_response(user_profile=profile, bundle_data=bundle_data, rate=rate,
                                                   x_currency=x_currency,
                                                   tax=user_order.tax_amount)
        for history in bundle.transaction_history:
            order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=history.user_order_id)
            amount = float(history.bundle.original_price * rate) + float((order.tax_amount / 100) * rate)
            history.bundle.price_display = f"{round(amount, 2)} {x_currency}"
        return ResponseHelper.success_data_response(bundle, 0)

    async def get_order_history(self, user_id: str, page_index: int, page_size: int, x_currency: str,
                                accept_language: str = "en") -> Response[List[UserOrderHistoryResponse]]:
        """Return order history; translate bundle names to `accept_language` when a translation exists.

        Default `accept_language` is 'en' for backward compatibility with callers that don't pass a locale.
        """
        rate = self.__currency_service.get_currency_rate(from_currency="USD", to_currency=x_currency)
        user_orders = self.__user_order_repo.list(
            where={"user_id": user_id, "payment_status": OrderStatusEnum.SUCCESS,
                   "order_status": OrderStatusEnum.SUCCESS}, limit=page_size,
            offset=((page_index - 1) * page_size))

        result: List[UserOrderHistoryResponse] = []
        for data in user_orders:
            try:
                uoh: UserOrderHistoryResponse = DtoMapper.to_user_order_history(user_order=data, rate=rate,
                                                                                currency=x_currency)
                # Translate bundle details if a translation exists for the requested locale
                try:
                    bundle_code = getattr(uoh.bundle_details, 'bundle_code', None)
                    if bundle_code:
                        translated_bundle = self.__bundle_translation_repo.get_first_by(where={
                            "bundle_id": bundle_code,
                            "locale": accept_language
                        })
                        if translated_bundle is not None and getattr(translated_bundle, 'data', None):
                            uoh.bundle_details = BundleDTO.model_validate(translated_bundle.data)
                except Exception as e:
                    logger.debug(f"Failed to apply bundle translation for order {getattr(data, 'id', 'unknown')}: {e}")

                result.append(uoh)
            except Exception as e:
                logger.error(f"Failed mapping order to history for order {getattr(data, 'id', 'unknown')}: {e}")

        return ResponseHelper.success_data_response(result, len(result))

    async def get_order_history_by_id(self, user_id: str, order_id: str, x_currency: str) -> Response[
        UserOrderHistoryResponse]:
        order = self.__user_order_repo.get_first_by({"user_id": user_id, "id": order_id})
        rate = self.__currency_service.get_currency_rate(from_currency="USD", to_currency=x_currency)
        payment_details = stripe_get_payment_details(order.payment_intent_code)
        user_order_history = DtoMapper.to_user_order_history(user_order=order, rate=rate, currency=x_currency)
        user_order_history.payment_details = payment_details
        return ResponseHelper.success_data_response(user_order_history, 1)

    async def cancel_order(self, order_id: str, user: UserModel) -> Response[None]:

        order: UserOrderModel = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
        if not order:
            raise CustomException(code=404, name=ErrorMessages.ORDER_NOT_FOUND,
                                  details=ErrorMessages.ORDER_NOT_FOUND)
        try:
            self.__user_order_repo.update(order_id, {"order_status": OrderStatusEnum.CANCELED,
                                                     "payment_status": OrderStatusEnum.CANCELED})
            self.__promotion_service.cancel_promotion_usage(order_id=order_id)
            stripe.PaymentIntent.cancel(order.payment_intent_code)
        except Exception as e:
            logger.error(f"Error while cancelling order {order_id}: {e}")
        return ResponseHelper.success_response()

    async def verify_order_otp(self, user: UserModel, request: VerifyOtpRequestDto) -> Response[bool]:
        logger.info(f"receiving verification otp request {request}")
        user_order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=request.order_id)
        if not user_order:
            raise CustomException(code=404, name=ErrorMessages.ORDER_NOT_FOUND,
                                  details=ErrorMessages.ORDER_NOT_FOUND)
        if user_order.otp != request.otp:
            raise CustomException(code=404, name=ErrorMessages.OTP_INVALID,
                                  details=ErrorMessages.OTP_INVALID)

        if self.__is_order_otp_expired(order_id=user_order.id):
            raise CustomException(code=400, name=ErrorMessages.OTP_EXPIRED,
                                  details=ErrorMessages.OTP_EXPIRED)

        bundle = BundleDTO.model_validate_json(user_order.bundle_data)

        default_currency = os.getenv("DEFAULT_CURRENCY", "USD")
        usd_amount_units = ((
                                user_order.modified_amount if user_order.modified_amount else user_order.amount) or 0) / 100
        converted_units = self.__currency_service.convert(from_currency="USD", to_currency=default_currency,
                                                          amount=usd_amount_units)
        usermodel: UsersCopyModel = self.__user_repo.get_by_id(user.id)
        language = lower(usermodel.metadata.get("language", "en"))
        response = await self.__dcb_service.deduct_balance(msisdn=user.msisdn, amount=converted_units,
                                                           order_id=user_order.id,
                                                           locale=language)

        if not response:
            self.__user_order_repo.update_by(where={"id": user_order.id},
                                             data={"payment_status": OrderStatusEnum.FAILURE})
            raise CustomException(code=400, name=ErrorMessages.PAYMENT_FAILED, details=ErrorMessages.PAYMENT_FAILED)

        payment_status = OrderStatusEnum.SUCCESS if response else OrderStatusEnum.FAILURE

        if user_order.order_type == UserOrderType.BUNDLE_TOP_UP:
            return await self.__bundle_service.top_up_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                             payment_status=payment_status,
                                                             payment_type=PaymentTypeEnum.DCB,
                                                             iccid=request.iccid)
        return await self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                      payment_status=payment_status,
                                                      payment_type=PaymentTypeEnum.DCB)

    async def resend_order_otp(self, user: UserModel, order_id: str) -> Response[None]:
        order = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
        if not order:
            raise BadRequestException(f"Order {order_id} not found")
        otp = generate_otp()
        expiration_time = int(get_config(ConfigKeysEnum.OTP_EXPIRATION_TIME))
        expire_at = (datetime.now(tz=dt_timezone.utc) + timedelta(minutes=expiration_time)).isoformat()
        self.__user_order_repo.update_by(where={"id": order.id}, data={"otp": otp, "otp_expired_at": expire_at})
        usermodel = self.__user_repo.get_by_id(user.id)

        language = lower(usermodel.metadata.get("language", "en"))
        await self.__dcb_service.send_otp(msisdn=user.msisdn, otp=order.otp,
                                          locale=language)
        return ResponseHelper.success_response()

    async def __handle_wallet_payment(self, user: UserModel, bundle: BundleDTO, user_order: UserOrderModel,
                                      rule_id: str,
                                      modified_amount: float,
                                      iccid: str = None) -> Response[
        PaymentIntentResponse]:
        wallet: UserWalletModel = self.__user_wallet_service.get_user_wallet(user_id=user.id)
        rate = self.__currency_service.get_currency_rate(from_currency="USD", to_currency=wallet.currency)
        bundle_price = float(truncate_two_decimals_decimal(modified_amount * rate))
        logger.info(f"wallet balance and bundle price: {wallet.amount=} {bundle_price=}")
        if wallet.amount < bundle_price:
            raise CustomException(code=400, name=ErrorMessages.INSUFFICIENT_WALLET_BALANCE,
                                  details="Insufficient wallet balance, please top up your wallet")
        try:
            self.__user_wallet_service.add_wallet_transaction(amount=(bundle_price * -1),
                                                              user_id=user.id,
                                                              source=UserWalletTransactionSource.PURCHASE_BUNDLE,
                                                              order_currency="USD")
            if user_order.order_type == UserOrderType.ASSIGN:
                await self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                       payment_status=OrderStatusEnum.SUCCESS,
                                                       payment_type=PaymentTypeEnum.WALLET,
                                                       rule_id=rule_id)
            elif user_order.order_type == UserOrderType.BUNDLE_TOP_UP:
                await self.__bundle_service.top_up_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                          payment_status=OrderStatusEnum.SUCCESS,
                                                          payment_type=PaymentTypeEnum.WALLET,
                                                          iccid=iccid)
            response = PaymentIntentResponse(order_id=user_order.id, payment_status=PaymentStatusEnum.COMPLETED)
            return ResponseHelper.success_data_response(response, 0)
        except Exception as e:
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED,
                                  details=f"Error while creating order: {e}")

    async def __handle_dcb_payment(self, user: UserModel, user_order: UserOrderModel, bundle: BundleDTO) -> Response[
        PaymentIntentResponse]:
        logger.info(f"handle_dcb_payment request {user=} {bundle=} {user_order=}")
        expiration_time = int(get_config(ConfigKeysEnum.OTP_EXPIRATION_TIME))
        expire_at = (datetime.now(tz=dt_timezone.utc) + timedelta(minutes=expiration_time)).isoformat()

        try:
            otp = generate_otp()
            self.__user_order_repo.update_by(where={"id": user_order.id},
                                             data={"otp": otp, "otp_expired_at": expire_at})
            msisdn = user.msisdn
            logger.info(f"requesting new otp for msisdn: {msisdn}")
            usermodel = self.__user_repo.get_by_id(user.id)
            language = lower(usermodel.metadata.get("language", "en"))
            await self.__dcb_service.send_otp(msisdn=msisdn, otp=otp,
                                              locale=language)
            response = PaymentIntentResponse(order_id=user_order.id,
                                             payment_status=PaymentStatusEnum.PENDING_VERIFICATION)
            response.otp_expiration = int(get_config(ConfigKeysEnum.OTP_EXPIRATION_TIME, 5)) * 60
            return ResponseHelper.success_data_response(response, 0)
        except Exception as e:
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED,
                                  details=f"Error while creating order: {e}")

    async def __handle_card_payment(self, user: UserModel, order: UserOrderModel, device_id: str,
                                    assign_request: AssignRequest | None, rule_id: str,
                                    request: Request, iccid: str = None,
                                    x_currency: str = os.getenv("DEFAULT_CURRENCY")) -> Response:
        # Use Decimal throughout to avoid float rounding issues
        raw_rate = self.__currency_service.get_currency_rate("USD", to_currency=x_currency)
        rate = Decimal(str(raw_rate))

        # order.modified_amount may sometimes be a float (see background update), so convert via str to Decimal
        order_amount_value = order.modified_amount if order.modified_amount is not None else order.amount
        # represent amount in cents as Decimal (may include fractional cents in some edge cases earlier)
        order_amount_cents = Decimal(str(order_amount_value))

        # Calculate amounts using Decimal and quantize appropriately
        # original_amount is the display amount in the target currency (x_currency)
        original_amount = (order_amount_cents / Decimal('100') * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # stripe expects integer smallest-units (cents). Compute target cents and round half up to whole cents.
        target_cents = (order_amount_cents * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        stripe_amount = int(target_cents)

        metadata = {
            "order_id": order.id,
            "user_id": order.user_id,
            "device_id": device_id,
            "bundle_code": order.bundle_id,
            "order_type": order.order_type,
            "env": os.environ.get("ENVIRONMENT", "DEV"),
            "promo_code": assign_request.promo_code if assign_request else None,
            "rule_id": rule_id,
            "amount": stripe_amount
        }
        if order.order_type == UserOrderType.BUNDLE_TOP_UP and iccid:
            metadata["iccid"] = iccid
        payment_intent, tax = create_payment_intent(user_bundle_order=order, user_email=user.email,
                                                    metadata=metadata,
                                                    rate=raw_rate,
                                                    currency=x_currency,
                                                    ip_address=request.client.host)
        order.payment_intent_code = payment_intent.id
        self.__user_order_repo.update_by({"id": order.id}, data=order.model_dump(exclude={"id"}))
        tax_excl = float(getattr(tax, "tax_amount_exclusive", 0) / 100)
        ephemeral = create_payment_ephemeral(payment_intent.customer)
        response = PaymentIntentResponse(publishable_key=os.getenv("STRIPE_PUBLIC_KEY"),
                                         merchant_identifier=os.getenv("MERCHANT_ID"),
                                         payment_intent_client_secret=payment_intent.client_secret,
                                         customer_id=payment_intent.customer,
                                         customer_ephemeral_key_secret=ephemeral.secret,
                                         test_env=not payment_intent.livemode,
                                         merchant_display_name=os.getenv("MERCHANT_DISPLAY_NAME"),
                                         billing_country_code="GB",
                                         order_id=order.id,
                                         subtotal_price_display=f"{round(original_amount, 2)} {x_currency}",
                                         total_price_display=f"{round(payment_intent.amount / 100, 2)} {x_currency}",
                                         tax_price_display=f"{round(tax_excl, 2)} {x_currency}",
                                         has_tax=tax_excl > 0
                                         )

        usd_tax = self.__currency_service.convert(x_currency, order.currency, tax_excl)
        self.__user_order_repo.update(record_id=order.id, data={"tax_amount": usd_tax * 100})
        return ResponseHelper.success_data_response(response, 0)

    def __check_if_user_eligible_for_referral(self, user: UserModel, promo_code: str):
        old_profiles = self.__user_profile_repo.list(where={"user_id": user.id})
        if len(old_profiles) > 0:
            raise CustomException(code=400, name=ErrorMessages.USER_HAS_PREVIOUS_ESIM,
                                  details="User already purchased esim before, cannot use referral code")
        user_model: UsersCopyModel = self.__user_repo.get_by_id(user.id)
        if user_model.metadata.get("referral_code") and user_model.metadata.get("referral_code") == promo_code:
            raise CustomException(code=400, name=ErrorMessages.OWN_REFERRAL_CODE_CANNOT_BE_USED,
                                  details="Own Referral Code Can not be used")

    async def __update_order_with_delay(self, order_id: str, bundle: BundleDTO):
        """Background task to update order with delay"""
        await asyncio.sleep(3)
        try:
            modified_amount = bundle.original_price
            amount = bundle.original_price
            logger.info(f"Updating order {order_id} with delayed background task at {datetime.now()}")
            self.__user_order_repo.update_by(where={"id": order_id}, data={
                "amount": int(round(amount * 100)),
                "modified_amount": int(round(modified_amount * 100)),
                "bundle_data": bundle.model_dump_json()
            })
            logger.info(f"Successfully updated order {order_id} in background")
        except Exception as e:
            logger.error(f"Error updating order {order_id} in background: {str(e)}")

    def __is_order_otp_expired(self, order_id: str) -> bool:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        resp = (
            supabase_client()
            .table("user_order")
            .select("id")
            .eq("id", order_id)
            .lt("otp_expired_at", now)  # otp_expired_at < now
            .execute()
        )

        return len(resp.data) > 0
