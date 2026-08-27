import asyncio
import json
import os
from dataclasses import dataclass
from typing import Dict, Optional

import stripe
from fastapi import Request, HTTPException
from loguru import logger
from soupsieve.util import lower

from app.config.config import STRIPE_WEBHOOK_SECRET, esim_hub_service_instance, send_email, get_email_template
from app.config.constants import PaymentIntentEvents, UserWalletTransactionSource
from app.config.db import PaymentTypeEnum
from app.config.helper import get_config
from app.config.notification_types import send_consumption_80_bundle_notification, \
    send_consumption_100_bundle_notification, send_plan_started_notification, \
    send_wallet_top_up_failed_notification
from app.config.push_notification_manager import fcm_service
from app.config.utils import parse_iso_datetime, truncate_two_decimals_decimal_rounded
from app.models.user import OrderStatusEnum, UserOrderType, UsersCopyModel, UserOrderModel, UserProfileBundleModel, \
    UserProfileModel
from app.repo import UserOrderRepo, UserProfileRepo, UserRepo, UserProfileBundleRepo
from app.schemas.callback import ConsumptionLimitRequest
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.response import ResponseHelper
from app.services.bundle_service import BundleService
from app.services.promotion_service import PromotionService
from app.services.sync_service import SyncService
from app.services.task_executor import TaskExecutor
from app.services.user_wallet_service import UserWalletService


@dataclass
class SyncRequest:
    bundle_id: str
    operation: str
    reseller_id: Optional[str] = None


class CallbackService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__user_repo = UserRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()
        self.__user_profile_bundle_repo = UserProfileBundleRepo()
        self.__sync_service = SyncService()
        self.__user_wallet_service = UserWalletService()
        self.__promotion_service = PromotionService()
        self.__bundle_service = BundleService()
        self.__task_executor = TaskExecutor()

    def _execute_sync_request(self, sync_request: SyncRequest):
        """Execute a single sync request"""
        try:
            logger.info(f"Processing sync request: {sync_request.bundle_id}, operation: {sync_request.operation}")
            asyncio.run(self.__run_one_sync_internal(sync_request.bundle_id, sync_request.operation,
                                                     sync_request.reseller_id))
            logger.info(f"Completed sync request: {sync_request.bundle_id}")
        except Exception as e:
            logger.error(f"Error processing sync request {sync_request.bundle_id}: {str(e)}")

    async def handle_plan_event_callback(self, callback_request: Request):
        try:
            request_data = await callback_request.json()
            logger.info(f"received 'handle_plan_event_callback' request: {request_data}")
            request = ConsumptionLimitRequest.model_validate(request_data)
            event_type = request.event_type
            iccid = request.iccid

            user_profile_bundle: UserProfileBundleModel = self.__user_profile_bundle_repo.get_first_by(
                where={"iccid": iccid, "esim_hub_order_id": request.order_id})
            if not user_profile_bundle:
                logger.warning(f"No user profile bundle found for iccid {iccid} and order_id {request.order_id}")
                return
            user_profile: UserProfileModel = self.__user_profile_repo.get_by_id(
                record_id=user_profile_bundle.user_profile_id)
            bundles = []
            bundles.append(user_profile_bundle)
            user_profile.bundles = bundles
            if not user_profile:
                logger.warning(f"No user profile found for user_profile_id {user_profile_bundle.user_profile_id}")
                return

            order_info = user_profile
            orders = []

            primary_user_id = order_info.user_id
            if primary_user_id:
                primary_user_metadata = {}
                primary_user = self.__user_repo.get_by_id(
                    record_id=primary_user_id)
                if primary_user:
                    primary_user_metadata = primary_user.metadata
                model = DtoMapper.to_order_notification_model(bundle=order_info, user_id=primary_user_id,
                                                              user_metadata=primary_user_metadata, iccid=iccid)
                orders.append(model)
            shared_user_id = order_info.shared_user_id
            if shared_user_id:
                shared_user_metadata = {}
                shared_user = self.__user_repo.get_by_id(record_id=shared_user_id)
                if shared_user:
                    shared_user_metadata = shared_user.metadata
                model = DtoMapper.to_order_notification_model(order_info, shared_user_id, shared_user_metadata, iccid)
                orders.append(model)

            if not orders:
                logger.warning(f"No users found for ICCID: {iccid}")
                return

            for order in orders:
                await self.__handle_event_for_order(order=order, iccid=iccid, event_type=event_type,
                                                    esim_order_id=request.order_id)

        except Exception as e:
            logger.error(f"Error in handle_plan_event_callback: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    async def handle_payment_webhook(self, request: Request):
        payload = await request.body()
        sig_header = request.headers.get("stripe-signature")
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except ValueError:
            logger.error("Invalid Stripe webhook payload.")
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError:
            logger.error("Stripe webhook signature verification failed.")
            raise HTTPException(status_code=400, detail="Invalid signature")

        def task():
            return self.__handle_payment_webhook_data(event=event)

        self.__task_executor.add_task(task)

        return ResponseHelper.success_response()

    async def handle_payment_webhook_fake(self, request: Request):
        try:
            payload = await request.body()
            payload_json = json.loads(payload)
        except Exception as e:
            logger.error(e)
            raise HTTPException(status_code=400, detail="Invalid payload")
        await self.__handle_payment_webhook_data(payload_json)

    def handle_sync_all_bundles(self, page_index=1):
        def task():
            self.__run_full_sync(page_index=page_index)

        self.__task_executor.add_task(task)
        return ResponseHelper.success_response()

    async def handle_exchange_rate_update(self, request: Request):
        json_request = await request.json()
        system_currency_code = json_request["systemCurrencyCode"]
        currency_code = json_request["currencyCode"]
        reseller_id = json_request["resellerId"]
        rate = float(json_request["newRate"])
        logger.info(f"receiving exchange rate update request {json_request}")
        if reseller_id and reseller_id != get_config("RESELLER_ID"):
            logger.info(f"ignoring exchange rate update request for reseller {reseller_id}")
            return ResponseHelper.success_response()
        if system_currency_code != "USD":
            logger.info(f"ignoring exchange rate update request for {system_currency_code}")
            return ResponseHelper.success_response()
        from app.repo.currency_repo import CurrencyRepo
        currency_repo = CurrencyRepo()
        inverse_rate = truncate_two_decimals_decimal_rounded(1 / rate if rate != 0 else 0)
        logger.info(
            f"updating currency USD to  {currency_code=} with inverse rate {inverse_rate=}")

        old_record = currency_repo.get_first_by(
            where={"default_currency": currency_code, "name": "USD"})
        if old_record:
            currency_repo.update_by(
                {"default_currency": currency_code, "name": "USD"},
                data={'rate': inverse_rate}
            )
        else:
            currency_repo.create(
                data={"default_currency": currency_code, "name": "USD", "rate": inverse_rate}
            )

        currency = currency_repo.get_first_by(where={"name": currency_code, "default_currency": "USD"})
        if not currency:
            logger.info(f"currency {currency_code} not found, creating new currency")
            currency_repo.create({"name": currency_code, "default_currency": "USD", "rate": rate})
            return ResponseHelper.success_response()
        currency_repo.update_by(where={"name": currency_code, "default_currency": "USD"}, data={"rate": rate})
        logger.info(f"updated exchange rate for {currency_code} to {rate}")
        self.__sync_service.update_sync_version()
        return ResponseHelper.success_response()

    async def handle_sync_one_bundle(self, request: Request):
        json_data = await request.json()
        logger.info(f"receiving bundle sync request {json_data}")
        operation = json_data.get("operation", "insert")
        bundle_id = json_data.get("bundle_id")
        reseller_id = json_data.get("reseller_id", None)

        # Create a wrapper function to execute the async method
        def sync_task():
            asyncio.run(self.__run_one_sync_internal(bundle_id, operation, reseller_id))

        # Add sync request to task executor
        self.__task_executor.add_task(sync_task)
        logger.info(f"Added sync request to queue. Queue size: {self.__task_executor.queue_size()}")

        return ResponseHelper.success_response()

    def handle_sync_one_bundle_by_id(self, request: Request, id: str):
        logger.info(f"receiving bundle sync by id request for bundle {id}")
        return ResponseHelper.success_response()

    async def __run_one_sync_internal(self, bundle_id: str, operation: str, reseller_id: str = None):
        """Internal method that actually performs tkhe sync work - called by queue processor"""
        try:
            if reseller_id and reseller_id == get_config("RESELLER_ID"):
                if operation == "delete":
                    logger.info(f"deleting bundle {bundle_id} for reseller {reseller_id}")
                    await self.__sync_service.delete_bundle(bundle_id=bundle_id)
                elif operation == "assign" or operation == "edit_price":
                    logger.info(f"{operation} for bundle {bundle_id} for reseller {reseller_id}")
                    bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id=bundle_id,
                                                                            currency_code=os.getenv("DEFAULT_CURRENCY"))
                    if not bundle:
                        logger.error(f"bundle {bundle_id} not found in esim hub for reseller {reseller_id}")
                        await self.__sync_service.delete_bundle(bundle_id=bundle_id)
                        return
                    await self.__sync_service.sync_bundle(bundle)
                elif operation == "unassign":
                    logger.info(f"unassigning bundle {bundle_id} for reseller {reseller_id}")
                    await self.__sync_service.delete_bundle(bundle_id=bundle_id)
                elif operation == "activate":
                    logger.info(f"activating bundle {bundle_id} for reseller {reseller_id}")
                    await self.__sync_service.update_bundle_status(bundle_id=bundle_id, status=True)
                elif operation == "deactivate":
                    logger.info(f"deactivating bundle {bundle_id} for reseller {reseller_id}")
                    await self.__sync_service.delete_bundle(bundle_id=bundle_id)
            if operation == "update":
                await self.__sync_service.delete_bundle(bundle_id=bundle_id)
                bundle = None
                try:
                    bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id=bundle_id,
                                                                            currency_code=os.getenv("DEFAULT_CURRENCY"))
                except Exception as e:
                    logger.error(f"error while fetching bundle {bundle_id} from esim hub: {str(e)}")
                finally:
                    if bundle:
                        logger.info(f"updating bundle {bundle_id} for reseller {reseller_id}")
                        await self.__sync_service.sync_bundle(bundle)
            self.__sync_service.update_sync_version()
        except Exception as e:
            logger.error(f"error while syncing bundle {bundle_id}: {str(e)}")
            raise  # Re-raise so the queue processor can log it

    def __run_full_sync(self, page_index=1):
        import asyncio
        asyncio.run(self.__sync_service.sync_bundles(page_index=page_index))
        self.__sync_service.update_sync_version()

    def __handle_payment_webhook_data(self, event: dict):
        logger.debug(f"Received payment webhook.{event.get('type')}")
        if event.get("type") not in [PaymentIntentEvents.SUCCEEDED, PaymentIntentEvents.FAILED,
                                     PaymentIntentEvents.CANCELED]:
            logger.info(f"Ignoring payment intent {event.get('type')}")
            return ResponseHelper.success_response()

        payment_intent = event.get("data").get("object", {})
        metadata = payment_intent.get("metadata", {})
        payment_intent_id = payment_intent.get("id", None)

        environment = metadata.get("env")
        if environment != os.getenv("ENVIRONMENT", "DEV"):
            logger.info(
                f"Ignoring payment webhook for ({environment}) running environment({os.getenv('ENVIRONMENT', 'DEV')})")
            return ResponseHelper.success_response()
        if metadata.get("user_wallet_id", None):
            return self.__handle_wallet_top_up(metadata, event.get("type"))
        self.__check_metadata_fields(metadata)
        order_id = metadata.get("order_id")
        user_id = metadata.get("user_id")
        order_type = metadata.get("order_type")
        iccid = metadata.get("iccid", None)
        promo_code = metadata.get("promo_code", None)
        rule_id = metadata.get("rule_id", None)
        tax_calculation = metadata.get("tax_calculation", None)
        user_order = self.__user_order_repo.get_by_id(order_id)
        bundle = BundleDTO.model_validate_json(user_order.bundle_data)
        # canceled (abandoned/expired intents) releases the promo usage the same way a
        # failed payment does — otherwise the pending row locks the user+code combination
        payment_status = OrderStatusEnum.SUCCESS if event.get(
            "type") == "payment_intent.succeeded" else OrderStatusEnum.FAILURE
        if payment_status == OrderStatusEnum.FAILURE:
            logger.info(f"payment failed for order {order_id}")
            if promo_code:
                asyncio.run(
                    self.__promotion_service.update_promotion_usage(user_id=user_id, code=promo_code, status="failed",
                                                                    rule_id=rule_id, order_id=order_id))
            return HTTPException(status_code=200, detail="Payment Failed")
        if payment_status == OrderStatusEnum.SUCCESS and tax_calculation:
            try:
                stripe.tax.Transaction.create_from_calculation(
                    calculation=tax_calculation,
                    reference=payment_intent_id,
                    expand=["line_items"],
                )
            except Exception as e:
                logger.error(f"Error while creating tax transaction: {str(e)}")

        if payment_status == OrderStatusEnum.SUCCESS and order_type == UserOrderType.ASSIGN:
            return asyncio.run(self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle,
                                                                payment_status=payment_status,
                                                                user_id=user_id,
                                                                rule_id=rule_id,
                                                                payment_type=PaymentTypeEnum.CARD))
        elif payment_status == OrderStatusEnum.SUCCESS and order_type == UserOrderType.BUNDLE_TOP_UP:
            if not iccid:
                logger.error(f"invalid iccid ({iccid}) for topup request ({user_order.id})")
                return HTTPException(status_code=400, detail="Invalid iccid")
            return asyncio.run(self.__bundle_service.top_up_bundle(bundle=bundle, user_order=user_order, iccid=iccid,
                                                                   user_id=user_id,
                                                                   payment_status=payment_status))
        return ResponseHelper.success_response()

    def __check_metadata_fields(self, metadata: dict):
        if not all([metadata["order_id"], metadata["user_id"], metadata["bundle_code"]]):
            logger.error(f"Missing metadata fields: {metadata}")
            raise HTTPException(status_code=400, detail="Missing order details in metadata")

    async def __send_email_80_consumption(self, user: UsersCopyModel, bundle_name, iccid):
        try:
            msisdn = os.getenv("WHATSAPP_NUMBER", "")
            if msisdn:
                msisdn = msisdn.replace("+", "").replace("-", "").replace(" ", "")
            display_email = user.metadata.get("display_email", None)
            email = user.metadata.get("email", user.email) if display_email is None else display_email

            data = {
                "user": email,
                "bundle_name": bundle_name,
                "montyesim_msisdn": msisdn,
                "iccid": iccid,
                "base_url": get_config("BASE_URL", "https://sales-esim-shop-portal.onrender.com")
            }
            language = lower(user.metadata.get("language", "en"))
            template = get_email_template(f"eighty_percent_email_template_{language}.htm")
            html_content = template.render(data=data)
            send_email(subject="80% Consumption", html_content=html_content,
                       recipients=email)
        except Exception as e:
            logger.error(f"error while sending email {str(e)}")

    async def __send_email_100_consumption(self, user: UsersCopyModel, bundle_name, iccid):
        try:
            msisdn = os.getenv("WHATSAPP_NUMBER", "")
            if msisdn:
                msisdn = msisdn.replace("+", "").replace("-", "").replace(" ", "")
            display_email = user.metadata.get("display_email", None)
            email = user.metadata.get("email", user.email) if display_email is None else display_email

            data = {
                "user": email,
                "bundle_name": bundle_name,
                "montyesim_msisdn": msisdn,
                "iccid": iccid,
                "base_url": get_config("BASE_URL", "https://sales-esim-shop-portal.onrender.com")
            }
            language = lower(user.metadata.get("language", "en"))
            template = get_email_template(f"expiry_email_template_{language}.htm")
            html_content = template.render(data=data)
            send_email(subject="100% Consumption", html_content=html_content,
                       recipients=user.metadata.get("email", email))
        except Exception as e:
            logger.error(f"error while sending email {str(e)}")

    def __handle_wallet_top_up(self, metadata: Dict[str, str], event_type: str):

        user_wallet_id = metadata.get("user_wallet_id")
        user_id = metadata.get("user_id")
        order_id = metadata.get("order_id")
        order = self.__user_order_repo.get_by_id(order_id)
        user_wallet = self.__user_wallet_service.get_user_wallet_by_id(user_wallet_id)
        try:
            if event_type == "payment_intent.succeeded":
                amount = float(order.amount)
                logger.info(f"updating user wallet: {user_wallet} with new {amount=}")

                def task():
                    self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                                      source=UserWalletTransactionSource.TOP_UP_WALLET,
                                                                      order_currency="USD")
                    return

                self.__task_executor.add_task(task)
                self.__user_order_repo.update(order_id, {"payment_status": OrderStatusEnum.SUCCESS})
                logger.info(
                    f"Top-Up for user {user_id} wallet {user_wallet} with amount {amount} {order.currency} succeeded")
                return ResponseHelper.success_response()
            else:
                self.__user_order_repo.update(order_id, {"payment_status": OrderStatusEnum.FAILURE})
                logger.info(
                    f"Payment Failed for Wallet Top-Up for user {user_id} with amount {order.amount} {order.currency}")
                content = send_wallet_top_up_failed_notification()
                fcm_service.send_notification_to_user_from_template(content, user_id=user_id)
                return ResponseHelper.success_response()
        except Exception as e:
            logger.error(f"error while updating user wallet {str(e)}")
            content = send_wallet_top_up_failed_notification()
            fcm_service.send_notification_to_user_from_template(content, user_id=user_id)
            return ResponseHelper.success_response()

    async def __handle_event_for_order(self, order: UserOrderModel, iccid: str, event_type: str,
                                       esim_order_id: str = None):
        try:
            if event_type in ["limit_80", "PLAN-80", "Eighty"]:
                notification_data = send_consumption_80_bundle_notification(
                    user_name=order.user_display_name,
                    bundle_name=order.bundle_display_name,
                    iccid=iccid
                )
                user = self.__user_repo.get_by_id(record_id=order.user_id)
                await self.__send_email_80_consumption(
                    user=user,
                    bundle_name=order.bundle_display_name,
                    iccid=iccid
                )
            elif event_type in ["limit_100", "PLAN-100", "DATA_LIMIT", "PREPAID_PLAN_COMPLETION"]:
                notification_data = send_consumption_100_bundle_notification(
                    user_name=order.user_display_name,
                    bundle_name=order.bundle_display_name,
                    iccid=iccid
                )
                user = self.__user_repo.get_by_id(record_id=order.user_id)
                await self.__send_email_100_consumption(
                    user=user,
                    bundle_name=order.bundle_display_name,
                    iccid=iccid
                )
                self.__update_bundle_expired(iccid=iccid, esim_hub_order_id=esim_order_id, bundle_expired=True)
            elif event_type in ["StartBundle", "PLAN-STARTED", "thing activated", "Plan Started and Selected",
                                "SESSION_START", "Started"]:
                datetime_str = order.validity
                dt_object = parse_iso_datetime(datetime_str)
                if not dt_object:
                    logger.error(f"Failed to parse validity datetime '{datetime_str}'")
                    return

                # Use the date component in YYYY-MM-DD format
                date_only_str = dt_object.date().isoformat()
                notification_data = send_plan_started_notification(
                    bundle_name=order.bundle_display_name,
                    validity_date=date_only_str
                )
                self.__update_bundle_plan_started(iccid=iccid, esim_hub_order_id=esim_order_id,
                                                  plan_started=True)
            else:
                logger.warning(f"Unsupported event type for plan status callback: {event_type}")
                return

            fcm_service.send_notification_to_user_from_template(
                content_template=notification_data,
                user_id=order.user_id
            )
        except Exception as e:
            logger.error(f"Failed to send notification to user {order.user_id}: {str(e)}")

    def __update_bundle_expired(self, iccid: str, esim_hub_order_id: str, bundle_expired: bool):
        logger.info(f"Updating bundle {esim_hub_order_id} {iccid} bundle_expired to {bundle_expired}")
        self.__user_profile_bundle_repo.update_by(where={"esim_hub_order_id": esim_hub_order_id, "iccid": iccid},
                                                  data={"bundle_expired": bundle_expired})

    def __update_bundle_plan_started(self, iccid: str, esim_hub_order_id: str, plan_started: bool):
        logger.info(f"Updating bundle {esim_hub_order_id} {iccid} plan_started to {plan_started}")
        self.__user_profile_bundle_repo.update_by(where={"esim_hub_order_id": esim_hub_order_id, "iccid": iccid},
                                                  data={"plan_started": plan_started})
