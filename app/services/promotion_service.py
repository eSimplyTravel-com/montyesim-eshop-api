import os
from typing import List, Literal

from loguru import logger
from soupsieve.util import lower

from app.config.constants import UserWalletTransactionSource, ErrorMessages
from app.config.db import PromotionRuleAction, Beneficiary, PromotionRuleEvent, ConfigKeysEnum, PromotionStatusEnum
from app.config.helper import get_config
from app.config.i18n import I18n
from app.config.utils import truncate_two_decimals_decimal
from app.exceptions import CustomException
from app.models.promotion import PromotionModel, PromotionUsageModel
from app.models.promotion import PromotionRuleModel
from app.models.user import UsersCopyModel, UserOrderModel
from app.repo import PromotionRepo, PromotionRuleRepo, PromotionUsageRepo, UserRepo, UserProfileRepo
from app.repo.bundle_repo import BundleRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionValidationRequest, PromotionHistoryDto, \
    PromotionValidationResponse, ReferralInfoDto
from app.schemas.response import Response, ResponseHelper
from app.services.currency_service import CurrencyService
from app.services.user_wallet_service import UserWalletService


class PromotionService:

    def __init__(self):
        self.__promotion_repo = PromotionRepo()
        self.__promotion_rule_repo = PromotionRuleRepo()
        self.__promotion_usage_repo = PromotionUsageRepo()
        self.__user_repo = UserRepo()
        self.__user_wallet_service = UserWalletService()
        self.__bundle_repo = BundleRepo()
        self.__currency_service = CurrencyService()
        self.__user_profile_repo = UserProfileRepo()

    async def history(self, user_id: str, x_currency: str) -> Response[List[PromotionHistoryDto]]:
        rate = self.__currency_service.get_currency_rate(from_currency=os.getenv("SYSTEM_CURRENCY", "USD"),
                                                         to_currency=x_currency)
        transactions = self.__user_wallet_service.get_wallet_transactions(user_id=user_id)
        history = []
        for transaction in transactions:
            if transaction.source == UserWalletTransactionSource.PURCHASE_BUNDLE:
                continue
            promotion_history = PromotionHistoryDto(
                is_referral=transaction.source != UserWalletTransactionSource.CASHBACK,
                amount=f"{truncate_two_decimals_decimal(transaction.amount * rate)} {x_currency}",
                name=transaction.source,
                promotion_name="",
                date=transaction.created_at)
            history.append(promotion_history)
        return ResponseHelper.success_data_response_with_message(history, "Success", len(history))

    async def validate_promotion_code(self, promotion_validation_request: PromotionValidationRequest, x_currency: str,
                                      user_id: str, device_id: str,
                                      locale: str = "en") -> Response[BundleDTO]:
        from app.services.bundle_service import BundleService
        bundle_service = BundleService()
        bundle_response = bundle_service.get_bundle(bundle_id=promotion_validation_request.bundle_code,
                                                    currency_name=x_currency, locale=locale)
        bundle: BundleDTO = bundle_response.data
        if 0.5 > bundle.original_price > 0:
            raise CustomException(code=400, name=ErrorMessages.PROMO_CODE_CANNOT_BE_USED_FOR_THIS_BUNDLE,
                                  details="Promo Code Can not be used for this bundle")
        validation_response = await self.validate_promo_code(code=promotion_validation_request.promo_code,
                                                             bundle=bundle, user_id=user_id, device_id=device_id,
                                                             currency=x_currency,
                                                             locale=locale)
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        return ResponseHelper.success_data_response_with_message(
            DtoMapper.bundle_currency_update(bundle=validation_response.bundle, rate=rate, currency=x_currency),
            validation_response.message, 1)

    async def validate_promo_code(self, code: str, user_id: str, bundle: BundleDTO, device_id: str,
                                  currency: str, apply_usage: bool = False,
                                  order_id: str = None,
                                  locale: str = "en") -> PromotionValidationResponse | None:
        # check if the code is promotion
        is_referral = self.is_referral_code(code)
        rate = self.__currency_service.get_rate_by_currency(currency)
        if is_referral:
            rule_id = get_config(ConfigKeysEnum.DEFAULT_REFERRAL_RULE_ID)
            percentage = float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE))
            amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
            rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})
            self.__validate_referral(user_id=user_id, promotion_code=code, rule_id=rule_id, device_id=device_id)
            referred_to_user: UsersCopyModel = self.__user_repo.get_by_id(record_id=user_id)
            referred_by_user: UsersCopyModel = self.__user_repo.get_first_by(where={},
                                                                             filters={
                                                                                 self.__user_repo.referral_code_key(): code})
            if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
                bundle.original_price = max(bundle.original_price - amount, 0)
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name=ErrorMessages.PROMO_CODE_CANNOT_BE_USED_FOR_THIS_BUNDLE,
                                          details="Promo Code Can not be used for this bundle")
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referred_by_user.id, referrer_user_id=referred_to_user.id,
                                                 code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 device_id=device_id,
                                                 order_id=order_id,
                                                 referred_to=referred_to_user.email)
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referred_to_user.id, referrer_user_id=referred_by_user.id,
                                                 code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 device_id=device_id,
                                                 order_id=order_id,
                                                 referred_to=referred_by_user.email)
                return PromotionValidationResponse(bundle=bundle, rule_id=rule_id,
                                                   message=f"{I18n.get_message(key='DISCOUNT_AMOUNT', lang=locale)} {round(amount * rate, 2)} {currency}")
            elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
                discounted = (bundle.original_price * percentage) / 100
                discounted = round(discounted, 2)
                bundle.original_price = max(bundle.original_price - discounted, 0)
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name=ErrorMessages.PROMO_CODE_CANNOT_BE_USED_FOR_THIS_BUNDLE,
                                          details="Promo Code Can not be used for this bundle")
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referred_by_user.id, referrer_user_id=referred_to_user.id,
                                                 code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 referred_to=referred_to_user.email, device_id=device_id,
                                                 order_id=order_id)
                    await self.__handle_cashback(amount=discounted, beneficiary=str(rule.beneficiary),
                                                 user_id=referred_to_user.id, referrer_user_id=referred_by_user.id,
                                                 code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 device_id=device_id,
                                                 order_id=order_id,
                                                 referred_to=referred_by_user.email)
                return PromotionValidationResponse(bundle=bundle,
                                                   rule_id=rule_id,
                                                   message=f"{I18n.get_message(key='DISCOUNT_PERCENTAGE', lang=locale)} {percentage} %")
            else:
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referred_to_user.id, referrer_user_id=referred_to_user.id,
                                                 code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 referred_to=referred_by_user.email, device_id=device_id,
                                                 order_id=order_id)
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referred_by_user.id, referrer_user_id=referred_to_user.id,
                                                 code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 referred_to=referred_to_user.email, device_id=device_id,
                                                 order_id=order_id)
                return PromotionValidationResponse(bundle=bundle, rule_id=rule_id,
                                                   message=f"{I18n.get_message(key='CASHBACK_AMOUNT', lang=locale)} {round(amount * rate, 2)} {currency}")

        else:
            promotion: PromotionModel = self.__promotion_repo.get_first_by(where={"code": code})
            if promotion is None:
                logger.error(f"promotion code {code} not found")
                raise CustomException(code=400, name=ErrorMessages.PROMOTION_NOT_FOUND, details="Not Found")
            self.__validate_promotion(promotion=promotion, user_id=user_id, device_id=device_id)
            bundle_codes = promotion.bundle_code.split(",") if promotion.bundle_code else []
            if len(bundle_codes) > 0:
                logger.info(f"promotion model bundle code: {promotion.bundle_code}")
                if bundle.bundle_code not in bundle_codes:
                    logger.error(
                        f"Bundle code {bundle.bundle_code} does not match with promotion bundle code {promotion.bundle_code}")
                    raise CustomException(code=400, name=ErrorMessages.INVALID_BUNDLE_CODE,
                                          details="Bundle code does not match with promotion bundle code")
            rule = self.__promotion_rule_repo.get_first_by(where={"id": promotion.rule_id})
            if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
                bundle.original_price = max(bundle.original_price - promotion.amount, 0)
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name=ErrorMessages.PROMO_CODE_CANNOT_BE_USED_FOR_THIS_BUNDLE,
                                          details="Promo Code Can not be used for this bundle")
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if apply_usage:
                    self._insert_promotion_usage(user_id=user_id, amount=promotion.amount, status="pending", code=code,
                                                 is_referral=is_referral, bundle=bundle, device_id=device_id,
                                                 order_id=order_id)
                return PromotionValidationResponse(bundle=bundle,
                                                   rule_id=rule.id,
                                                   message=f"{I18n.get_message(key='DISCOUNT_AMOUNT', lang=locale)} {round(promotion.amount * rate, 2)} {currency}")
            elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
                discounted = bundle.original_price * promotion.amount / 100
                discounted = round(discounted, 2)
                bundle.original_price = max(bundle.original_price - discounted, 0)
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name=ErrorMessages.PROMO_CODE_CANNOT_BE_USED_FOR_THIS_BUNDLE,
                                          details="Promo Code Can not be used for this bundle")
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if apply_usage:
                    self._insert_promotion_usage(user_id=user_id, amount=discounted, status="pending", code=code,
                                                 is_referral=is_referral, bundle=bundle, device_id=device_id,
                                                 order_id=order_id)
                return PromotionValidationResponse(bundle=bundle,
                                                   rule_id=rule.id,
                                                   message=f"{I18n.get_message(key='DISCOUNT_PERCENTAGE', lang=locale)} {promotion.amount} %")
            else:
                if rule.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                    amount = (bundle.original_price * promotion.amount) / 100
                    message = f"{I18n.get_message(key='CASHBACK_PERCENTAGE', lang=locale)} ({promotion.amount}%) {round(amount * rate, 2)} {currency}"
                else:
                    amount = promotion.amount
                    message = f"{I18n.get_message(key='CASHBACK_AMOUNT', lang=locale)} {round(amount * rate, 2)} {currency}"
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=user_id, referrer_user_id="0", code=code, is_referral=False,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 device_id=device_id, order_id=order_id)
                return PromotionValidationResponse(bundle=bundle, rule_id=rule.id,
                                                   message=message)

    async def __handle_cashback(self, amount: float, beneficiary: str, user_id: str, referrer_user_id: str,
                                code: str, is_referral: bool, event_id, bundle: BundleDTO, order_id: str | None,
                                referred_to: str = None, device_id: str = None):
        logger.info(
            f"handle_cashback {amount=} {beneficiary=} {user_id=} {referrer_user_id=} {code=} {event_id=} {bundle=}")
        self._insert_promotion_usage(user_id=user_id, amount=amount, status="pending", code=code,
                                     is_referral=is_referral, bundle=bundle, referred_to=referred_to,
                                     device_id=device_id, order_id=order_id)
        return amount

    async def __handle_cashback_after_success_create_order(self, amount: float, beneficiary: int, user_id: str,
                                                           referrer_user_id: str):
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for user {user_id} with amount {amount}")
            self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                              source=UserWalletTransactionSource.CASHBACK_REFERRAL,
                                                              order_currency="USD")

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for user {user_id} with amount {amount}")
            self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=referrer_user_id,
                                                              source=UserWalletTransactionSource.CASHBACK_REFERRAL,
                                                              order_currency="USD")

    async def __handle_discount(self, original_price: float, discount: float, beneficiary: str,
                                user_id: str, referrer_user_id: str, code: str, is_referral: bool,
                                bundle: BundleDTO) -> float:
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(user_id, discount, "pending", code, is_referral, bundle)

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(referrer_user_id, discount, "pending", code, is_referral, bundle)

        return max(original_price - discount, 0)

    def _insert_promotion_usage(self, user_id, amount, status, code, is_referral, bundle, referred_to: str = None,
                                device_id: str = None,
                                order_id: str = None
                                ):
        bundle_id = None
        if bundle:
            bundle_id = bundle.bundle_code
        self.__promotion_usage_repo.create(data={
            "user_id": user_id,
            "amount": amount,
            "promotion_code": code if not is_referral else None,
            "referral_code": code if is_referral else None,
            "status": status,
            "bundle_id": bundle_id,
            "referred_to": referred_to,
            "device_id": device_id,
            "order_id": order_id
        })

    async def update_promotion_usage(self, user_id: str, code: str, status: str, rule_id: str, paid_amount: float = 0,
                                     order_id: str = None):
        data = {"status": status}
        is_referral = self.is_referral_code(code)
        conditions = {"user_id": user_id, "promotion_code": code} if not is_referral else {"user_id": user_id,
                                                                                           "referral_code": code}
        if order_id:
            conditions["order_id"] = order_id
            conditions.pop("user_id")
        referrer_user = self.__user_repo.get_first_by(where={},
                                                      filters={self.__user_repo.referral_code_key(): code})
        self.__promotion_usage_repo.update_by(where=conditions, data=data)
        if referrer_user:
            self.__promotion_usage_repo.update_by(where={"user_id": referrer_user.id, "referral_code": code}, data=data)

        if status == "completed" and rule_id != "0" and is_referral:
            rule_promotion: PromotionRuleModel = self.__promotion_rule_repo.get_by_id(record_id=rule_id)
            usages = self.__promotion_usage_repo.list(where={"promotion_code": code, "status": "completed"})
            self.__promotion_repo.update_by(where={"code": code}, data={"times_used": len(usages)})
            if (rule_promotion.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE
                    or rule_promotion.promotion_rule_action_id == PromotionRuleAction.CASHBACK_AMOUNT):
                promotion: PromotionModel = self.__promotion_repo.get_first_by(where={"code": code})
                if rule_promotion.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE:
                    amount = (float(paid_amount) * float(promotion.amount)) / 100
                else:
                    if promotion:
                        amount = float(promotion.amount)
                    else:
                        amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
                # rate = self.__currency_service.get_rate_by_currency(os.getenv("DEFAULT_CURRENCY"))
                await self.__handle_cashback_after_success_create_order(amount, Beneficiary.REFERRER.value,
                                                                        user_id, "")

    @staticmethod
    def __validate_rule_constraints(event_id, action_id, bundle, is_referral, beneficiary):
        if event_id == PromotionRuleEvent.CREATE_ORDER.value and not bundle:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_MISSING, details="bundle is missing")

        if action_id != PromotionRuleAction.CASHBACK_AMOUNT.value and not bundle:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_MISSING, details="bundle is missing")

        if event_id == PromotionRuleEvent.CREATE_ACCOUNT.value and action_id != PromotionRuleAction.CASHBACK_AMOUNT.value:
            raise CustomException(code=400, name=ErrorMessages.INVALID_ACTION,
                                  details="login event can have only cashback amount")

        if not is_referral and beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            raise CustomException(code=400, name=ErrorMessages.INVALID_INPUT,
                                  details="promotion rule for promotion can have beneficiary user only")

    def __validate_promotion(self, promotion: PromotionModel, user_id: str, device_id: str = None):
        rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": promotion.rule_id})

        if not promotion.is_active:
            raise CustomException(code=400, name=ErrorMessages.PROMOTION_NOT_ACTIVE, details="promotion not active")

        from datetime import datetime
        today = datetime.now().date()
        if promotion.valid_from and promotion.valid_to:
            def parse_date(date_str):
                try:
                    return datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    return datetime.strptime(date_str[:10], "%Y-%m-%d").date()

            start_date = parse_date(promotion.valid_from)
            end_date = parse_date(promotion.valid_to)
            if not (start_date <= today <= end_date):
                raise CustomException(code=400, name=ErrorMessages.PROMOTION_NOT_ACTIVE,
                                      details="Promotion is not active for current date")

        if promotion.times_used >= rule.max_usage:
            raise CustomException(code=400, name=ErrorMessages.PROMOTION_REACHED_MAX_USAGE,
                                  details="times used is full")

        last_pending_usage = self.__promotion_usage_repo.select_procedure(
            function_name="get_latest_pending_promotion_usage_per_user",
            where={"p_user_id": user_id, "p_promotion_code": promotion.code,
                   "p_window_seconds": int(get_config("PROMOTION_PENDING_LOCK_SECONDS", 60 * 60))})
        if last_pending_usage and len(last_pending_usage) > 0:
            raise CustomException(code=400, name=ErrorMessages.PROMOTION_ALREADY_IN_USE,
                                  details="Promotion code used too recently, please wait before reusing.")

        promotion_limit_active = get_config("PROMOTION_LIMIT_ACTIVE", "true")
        if lower(promotion_limit_active) == "false":
            logger.info("promotion limit is not active, applying 1 minute rate-limit per user+promo")
            last_usages = self.__promotion_usage_repo.select_procedure(
                function_name="get_latest_promotion_usage_per_user",
                where={"p_user_id": user_id, "p_promotion_code": promotion.code,
                       "p_window_seconds": int(get_config("PROMOTION_LIMIT_WINDOW_SECONDS", 60))})
            if last_usages and len(last_usages) > 0:
                raise CustomException(code=400, name=ErrorMessages.PROMOTION_MAX_USAGE_VALIDATION,
                                      details="Promotion code used too recently, please wait before reusing.")
            else:
                logger.info("no recent usage found, proceeding")
                return

        promotion_usage = self.__promotion_usage_repo.list(
            where={"user_id": user_id, "promotion_code": promotion.code, "status": "completed"})
        if promotion_usage:
            raise CustomException(code=400, name=ErrorMessages.PROMOTION_ALREADY_USED, details="Promotion Already Used")

    def __validate_referral(self, user_id: str, promotion_code: str, rule_id: str, device_id: str = None):

        referred_user: UsersCopyModel = self.__user_repo.get_first_by(where={},
                                                                      filters={
                                                                          self.__user_repo.referral_code_key(): promotion_code})

        old_profiles = self.__user_profile_repo.list(where={"user_id": user_id})
        if len(old_profiles) > 0:
            raise CustomException(code=400, name=ErrorMessages.USER_HAS_PREVIOUS_ESIM,
                                  details="User already purchased esim before, cannot use referral code")
        user_model: UsersCopyModel = self.__user_repo.get_by_id(user_id)
        if user_model.metadata.get("referral_code") and user_model.metadata.get("referral_code") == promotion_code:
            raise CustomException(code=400, name=ErrorMessages.OWN_REFERRAL_CODE_CANNOT_BE_USED,
                                  details="Own Referral Code Can not be used")

        if referred_user:
            previously_used = self.__promotion_usage_repo.list(
                where={"device_id": device_id, "status": PromotionStatusEnum.COMPLETED.value})
            previously_used = list(
                filter(lambda x: x.referral_code != "" and x.referral_code is not None, previously_used))
            if len(previously_used) > 0:
                raise CustomException(code=400, name=ErrorMessages.REFERRAL_CODE_ALREADY_USED_ON_THIS_DEVICE,
                                      details=ErrorMessages.REFERRAL_CODE_ALREADY_USED_ON_THIS_DEVICE)
            old_device = self.__promotion_usage_repo.list(
                where={"device_id": device_id, "referral_code": promotion_code, "referred_to": referred_user.email,
                       "status": PromotionStatusEnum.COMPLETED.value})
            if len(old_device) > 0:
                raise CustomException(code=400, name=ErrorMessages.REFERRAL_CODE_ALREADY_USED_ON_THIS_DEVICE,
                                      details="Referral Code Already Used on this device")
            referred_usage = self.__promotion_usage_repo.list(
                where={"referred_to": referred_user.email, "user_id": user_id, "referral_code": promotion_code,
                       "status": PromotionStatusEnum.COMPLETED.value})
            if len(referred_usage) > 0:
                for usage in referred_usage:
                    if usage.device_id == device_id:
                        logger.error("Referral code already used on this device")
                        raise CustomException(code=400, name=ErrorMessages.REFERRAL_CODE_ALREADY_USED_ON_THIS_DEVICE,
                                              details="Referral Code Already Used on this device")
                logger.error("Referral code already used by referred user")
                raise CustomException(code=400, name=ErrorMessages.REFERRAL_CODE_ALREADY_USED,
                                      details="Referral Code Already Used by referred user")

        promotion_usage = self.__promotion_usage_repo.list(
            where={"user_id": user_id, "referral_code": promotion_code, "status": PromotionStatusEnum.COMPLETED.value})

        if promotion_usage:
            logger.error("Referral code already used")
            raise CustomException(code=400, name=ErrorMessages.REFERRAL_CODE_ALREADY_USED,
                                  details="Referral Code Already Used")

        rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})
        if not rule:
            raise CustomException(code=400, name=ErrorMessages.PROMOTION_RULE_NOT_FOUND,
                                  details="promotion rule not found")

        promotion_referral_usage = self.__promotion_usage_repo.list(where={"referral_code": promotion_code})
        if len(promotion_referral_usage) > rule.max_usage:
            raise CustomException(code=400, name=ErrorMessages.PROMOTION_MAX_USAGE_VALIDATION,
                                  details="times used is full")

    def is_referral_code(self, referral_code: str) -> bool:
        return self.__user_repo.get_first_by(where={},
                                             filters={self.__user_repo.referral_code_key(): referral_code}) is not None

    async def apply_promotion_code_after_purchase(self, user_id: str, status: Literal["pending", "failed", "completed"],
                                                  rule_id: str, user_order: UserOrderModel):
        code = user_order.promo_code or user_order.referral_code
        order_id = user_order.id
        paid_amount = user_order.modified_amount / 100
        if user_order.currency != os.getenv("SYSTEM_CURRENCY", "USD"):
            rate = self.__currency_service.get_currency_rate(from_currency=user_order.currency,
                                                             to_currency=os.getenv("SYSTEM_CURRENCY", "USD"))
            paid_amount = paid_amount * rate
        is_referral = self.is_referral_code(code)
        referrer_user = self.__user_repo.get_first_by(where={},
                                                      filters={self.__user_repo.referral_code_key(): code})
        logger.info(
            f"applying {'referral' if is_referral else 'promotion'} code {code} for user {user_id} with status {status}")
        condition = {"user_id": user_id, "promotion_code": code} if not is_referral else {"user_id": user_id,
                                                                                          "referral_code": code}
        if order_id:
            condition["order_id"] = order_id
            condition.pop("user_id")

        logger.info(f"condition for updating promotion usage: {condition}")

        if status != "completed":
            self.__promotion_usage_repo.update_by(where=condition, data={"status": status})
            return None
        promotion_rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})

        if is_referral:
            return await self.__apply_promotion_code_for_referral(user_id=user_id,
                                                                  referrer_user=referrer_user,
                                                                  code=code,
                                                                  paid_amount=paid_amount,
                                                                  status=status,
                                                                  condition=condition,
                                                                  promotion_rule=promotion_rule)
        else:
            usage: PromotionUsageModel = self.__promotion_usage_repo.get_first_by(
                where={"promotion_code": code, "user_id": user_id, "status": "pending"})
            if usage:
                if promotion_rule.promotion_rule_action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value,
                                                               PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
                    amount = float(usage.amount)
                    self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                                      source=UserWalletTransactionSource.CASHBACK_PROMO,
                                                                      order_currency="USD")
            old_usage = self.__promotion_usage_repo.list(where={"promotion_code": code, "status": "completed"})
            if status == "completed":
                self.__promotion_repo.update_by(where={"code": code}, data={"times_used": len(old_usage) + 1})
            self.__promotion_usage_repo.update_by(where=condition, data={"status": status})
            return None

    async def __apply_promotion_code_for_referral(self, user_id: str, referrer_user: UsersCopyModel, code: str,
                                                  paid_amount: float, status: Literal["pending", "failed", "completed"],
                                                  condition: dict, promotion_rule: PromotionRuleModel = None):
        referred_promotion_usage = self.__promotion_usage_repo.get_first_by(
            where={"user_id": user_id, "status": "pending"})
        referrer_promotion_usage = self.__promotion_usage_repo.get_first_by(
            where={"user_id": referrer_user.id, "status": "pending"})
        if referred_promotion_usage is None and referrer_promotion_usage is None:
            logger.error(f"No pending promotion found for user {user_id} with code {code}")
            self.__promotion_usage_repo.update_by(where=condition, data={"status": "failed"})
            return None
        self.__promotion_usage_repo.update_by(where=condition, data={"status": status})
        return await self.__apply_referral_rewards(user_id=user_id, referral_code=code, paid_amount=paid_amount,
                                                   promotion_rule=promotion_rule)

    async def __apply_referral_rewards(self, user_id: str, referral_code: str, paid_amount: float,
                                       promotion_rule: PromotionRuleModel):
        referrer_user = self.__user_repo.get_first_by(where={},
                                                      filters={self.__user_repo.referral_code_key(): referral_code})
        amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
        if referrer_user is None:
            logger.error(f"Referrer User not found for referral code {referral_code}")
            return
        referrer_user_id = referrer_user.id

        if promotion_rule.beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for REFERRER user {referrer_user_id} with amount {amount}")
            self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=referrer_user_id,
                                                              source=UserWalletTransactionSource.CASHBACK_REFERRAL,
                                                              order_currency="USD")

        if promotion_rule.beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            usage = self.__promotion_usage_repo.get_first_by(where={"user_id": user_id, "referral_code": referral_code})
            if usage is None:
                logger.error(f"No pending promotion usage found for user {user_id} with referral code {referral_code}")
                return
            else:
                if promotion_rule.promotion_rule_action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value,
                                                               PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
                    if promotion_rule.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                        amount = round(
                            (paid_amount * float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE, 20))) / 100, 2)
                    logger.info(f"Adding cashback for REFERRED user {user_id} with amount {amount}")
                    self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                                      source=UserWalletTransactionSource.CASHBACK_REFERRAL,
                                                                      order_currency="USD")
        return None

    def cancel_promotion_usage(self, order_id: str):
        self.__promotion_usage_repo.update_by(where={"order_id": order_id},
                                              data={"status": PromotionStatusEnum.FAILED.value})

    def referral_info(self, x_currency: str, locale: str = "en") -> Response[ReferralInfoDto]:
        # Normalize incoming locale (may be Accept-Language header) to primary language tag like 'en' or 'ar'
        try:
            primary = locale.split(',')[0].split(';')[0].strip().split('-')[0].lower() if locale else 'en'
            if primary == '':
                primary = 'en'
        except Exception:
            primary = 'en'
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        rule_id = get_config(ConfigKeysEnum.DEFAULT_REFERRAL_RULE_ID)
        rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})
        if not rule:
            raise CustomException(code=400, name=ErrorMessages.PROMOTION_RULE_NOT_FOUND,
                                  details="promotion rule not found")

        amount = round(float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT)) * float(rate), 2)
        percentage = float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE))
        # Use I18n templates to produce localized referral messages. Templates support placeholders:
        # {amount} - formatted amount, {currency} - currency code, {percentage} - numeric percentage
        if rule.promotion_rule_action_id == PromotionRuleAction.CASHBACK_AMOUNT.value:
            tpl_key = "REFERRAL_MESSAGE_CASHBACK_AMOUNT"
        elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
            tpl_key = "REFERRAL_MESSAGE_DISCOUNT_PERCENTAGE"
        elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
            tpl_key = "REFERRAL_MESSAGE_DISCOUNT_AMOUNT"
        else:
            tpl_key = "REFERRAL_MESSAGE_DEFAULT"

        tpl = I18n.get_message(tpl_key, primary)
        try:
            # Format template with safe values
            message = tpl.format(amount=round(amount, 2), currency=x_currency, percentage=percentage)
        except Exception:
            # Fallback to an English safe string if template formatting fails
            message = f"Get {amount} {x_currency} credit for every friend that signs up and completes a purchase"
        dto = ReferralInfoDto(amount=round(amount, 2), type=str(rule.promotion_rule_action_id), currency=x_currency,
                              message=message)
        return ResponseHelper.success_data_response(dto, 1)

    def get_promotion_by_code(self, promo_code: str) -> PromotionModel | None:
        return self.__promotion_repo.get_first_by(where={"code": promo_code})

    def get_referral_rule(self) -> PromotionRuleModel | None:
        referral_rule_id = get_config(ConfigKeysEnum.DEFAULT_REFERRAL_RULE_ID)
        return self.__promotion_rule_repo.get_by_id(referral_rule_id)

    def get_rule_by_id(self, rule_id: str) -> PromotionRuleModel | None:
        return self.__promotion_rule_repo.get_by_id(rule_id)
