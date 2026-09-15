import os
import threading
from typing import List

from fastapi import Request
from loguru import logger

from app.config.constants import ErrorMessages
from app.config.db import UserOrderType
from app.config.notification_types import send_wallet_top_up_succeeded_notification
from app.config.push_notification_manager import fcm_service
from app.config.utils import create_wallet_top_up_intent, create_payment_ephemeral, truncate_two_decimals_decimal, \
    vat_in_price_cents
from app.exceptions import CustomException
from app.models.user import UserWalletModel, UserModel, UserWalletTransactionModel, UsersCopyModel
from app.repo import UserWalletRepo, UserOrderRepo, UserWalletTransactionRepo, UserRepo
from app.schemas.bundle import PaymentIntentResponse
from app.schemas.dto_mapper import DtoMapper
from app.schemas.response import Response, ResponseHelper
from app.schemas.user_wallet import UserWalletRequestDto, TopUpWalletRequest
from app.schemas.user_wallet import UserWalletResponse
from app.services.currency_service import CurrencyService


class UserWalletService:
    def __init__(self):
        self.__user_wallet_repo = UserWalletRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_wallet_transaction_repo = UserWalletTransactionRepo()
        self.__currency_service = CurrencyService()
        self.__user_repo = UserRepo()

    async def get_user_wallet_by_id(self, user_wallet_id: str) -> UserWalletResponse | None:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"id": user_wallet_id})
        if not wallet:
            return None
        return DtoMapper.to_user_wallet_response(wallet)

    async def create_wallet(self, user_wallet_request_dto: UserWalletRequestDto) -> UserWalletResponse:
        user_wallet = self.__user_wallet_repo.get_first_by(where={"user_id": user_wallet_request_dto.user_id})
        if user_wallet:
            return DtoMapper.to_user_wallet_response(user_wallet)
        wallet = self.__create_wallet(user_id=user_wallet_request_dto.user_id, amount=user_wallet_request_dto.amount)
        return DtoMapper.to_user_wallet_response(wallet)

    async def get_user_wallet_by_user_id(self, user_id: str, currency_code: str = os.getenv(
        "DEFAULT_CURRENCY")) -> UserWalletResponse | None:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"user_id": user_id})
        if not wallet:
            return None
        wallet.amount = self.__currency_service.convert(from_currency=os.getenv("SYSTEM_CURRENCY", "USD"),
                                                        to_currency=currency_code,
                                                        amount=wallet.amount)
        return DtoMapper.to_user_wallet_response(wallet)

    def get_user_wallet(self, user_id) -> UserWalletModel:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"user_id": user_id})
        return wallet

    def add_wallet_transaction(self, amount: float, user_id: str, source: str = "TopUp", order_currency: str = None) -> \
            Response[
                UserWalletResponse]:
        try:
            user: UsersCopyModel = self.__user_repo.get_first_by(where={"id": user_id})
            transaction_currency = user.metadata.get("currency", os.getenv("SYSTEM_CURRENCY", "USD"))

            user_wallet: UserWalletModel = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
            if user_wallet is None:
                raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")

            transaction_amount = amount
            notification_amount = amount
            if user_wallet.currency != transaction_currency and order_currency is None:
                rate = self.__currency_service.get_currency_rate(from_currency=user_wallet.currency,
                                                                 to_currency=transaction_currency)
                transaction_amount = truncate_two_decimals_decimal(amount * rate)
            if user_wallet.currency != transaction_currency:
                rate = self.__currency_service.get_currency_rate(from_currency=user_wallet.currency,
                                                                 to_currency=transaction_currency)
                notification_amount = truncate_two_decimals_decimal(amount * rate)
            current_amount = float(user_wallet.amount)
            add_amount = float(transaction_amount)
            new_amount = current_amount + add_amount
            user_wallet.amount = new_amount
            self.__user_wallet_repo.update_by(where={"user_id": user_id},
                                              data=user_wallet.model_dump())

            self.__user_wallet_transaction_repo.create(data={
                "wallet_id": user_wallet.id,
                "amount": add_amount,
                "source": source,
                "status": "success"
            })
            if amount > 0:
                thread = threading.Thread(target=self.__send_push,
                                          args=(notification_amount, transaction_currency, user_id))
                thread.start()
            dto = DtoMapper.to_user_wallet_response(user_wallet)
            return ResponseHelper.success_data_response(dto, 1)
        except Exception as e:
            logger.error(str(e))
            raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")

    def top_up_wallet(self, top_up_request: TopUpWalletRequest, user: UserModel, request: Request,
                      x_currency: str) -> Response[
        PaymentIntentResponse]:
        amount = top_up_request.amount

        user_wallet = self.__user_wallet_repo.get_first_by(where={"user_id": user.id})
        if not user_wallet:
            user_wallet = self.__create_wallet(user_id=user.user_id, amount=0)

        order_amount = amount
        if x_currency != user_wallet.currency:
            order_amount = self.__currency_service.convert(from_currency=x_currency,
                                                           to_currency=os.getenv("SYSTEM_CURRENCY", "USD"),
                                                           amount=top_up_request.amount)

        if order_amount <= 0.5:
            raise CustomException(code=400, name=ErrorMessages.INVALID_TOP_UP_AMOUNT,
                                  details="Top up amount must be greater than 0.5")
        order = self.__user_order_repo.create(data={
            "user_id": user.id,
            "bundle_id": None,
            "order_type": UserOrderType.WALLET_TOP_UP,
            "amount": order_amount,
            "currency": os.getenv("SYSTEM_CURRENCY", "USD"),
            "bundle_data": "-",
            "searched_countries": "-",
            "anonymous_user_id": None,
        })

        intent, tax = create_wallet_top_up_intent(user_email=user.email, amount=int(amount * 100),
                                                  currency=x_currency,
                                                  metadata={
                                                      "user_id": user.id,
                                                      "user_wallet_id": user_wallet.id,
                                                      "order_id": order.id,
                                                      "env": os.environ.get("ENVIRONMENT", "DEV"),
                                                  }, ip_address=request.client.host)
        # Top-ups are taxed "inclusive", so the VAT sits in tax_amount_inclusive and
        # the charged total equals the requested amount; the subtotal is net of VAT.
        vat = round(vat_in_price_cents(tax) / 100, 2)
        total = intent.amount / 100

        order.payment_intent_code = intent.id
        self.__user_order_repo.update_by({"id": order.id}, data=order.model_dump(exclude={"id"}))
        ephemeral = create_payment_ephemeral(intent.customer)
        response = PaymentIntentResponse(publishable_key=os.getenv("STRIPE_PUBLIC_KEY"),
                                         merchant_identifier=os.getenv("MERCHANT_ID"),
                                         payment_intent_client_secret=intent.client_secret,
                                         customer_id=intent.customer,
                                         customer_ephemeral_key_secret=ephemeral.secret,
                                         test_env=not intent.livemode,
                                         merchant_display_name=os.getenv("MERCHANT_DISPLAY_NAME"),
                                         billing_country_code="GB",
                                         order_id=order.id,
                                         total_price_display=f"{total:.2f} {x_currency}",
                                         subtotal_price_display=f"{total - vat:.2f} {x_currency}",
                                         tax_price_display=f"{vat:.2f} {x_currency}",
                                         has_tax=vat > 0
                                         )
        return ResponseHelper.success_data_response(response, 0)

    def get_wallet_transactions(self, user_id: str) -> List[UserWalletTransactionModel]:
        user_wallet = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
        if not user_wallet:
            return []
        transactions = self.__user_wallet_transaction_repo.list(where={"wallet_id": user_wallet.id},
                                                                order_by="created_at", desc=True)
        return transactions

    def __create_wallet(self, user_id: str, amount: float):
        wallet = self.__user_wallet_repo.create(data={
            "user_id": user_id,
            "amount": amount,
            "currency": os.getenv("SYSTEM_CURRENCY", "USD")
        })
        logger.info(f"creating wallet for user {user_id}")
        return wallet

    def __send_push(self, amount: float, currency: str, user_id: str):
        content = send_wallet_top_up_succeeded_notification(f"{amount} {currency}")
        fcm_service.send_notification_to_user_from_template(content, user_id=user_id)
