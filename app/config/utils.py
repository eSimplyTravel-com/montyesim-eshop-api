import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, ROUND_UP

import stripe
from dateutil import parser as dateutil_parser
from loguru import logger
from stripe import PaymentIntent, Charge

from app.config.config import STRIPE_SECRET_KEY
from app.config.constants import ErrorMessages
from app.config.db import ConfigKeysEnum
from app.config.helper import get_config
from app.exceptions import CustomException
from app.models.user import UserOrderModel
from app.schemas.bundle import PaymentDetailsDTO

stripe.api_key = STRIPE_SECRET_KEY


def create_payment_intent(user_bundle_order: UserOrderModel, user_email: str,
                          metadata: dict, rate: float, currency: str = os.getenv("DEFAULT_CURRENCY"),
                          ip_address: str = None) -> tuple[
    PaymentIntent, stripe.tax.Calculation | None]:
    tax = None
    try:
        customers = stripe.Customer.list(email=user_email)
        if not customers:
            customer = stripe.Customer.create(email=user_email)
        else:
            customer = customers.get("data")[0]
        # modified_amount == 0 is a legitimate 100%-discount total and must NOT fall back to the list price
        order_amount = user_bundle_order.modified_amount if user_bundle_order.modified_amount is not None else user_bundle_order.amount
        # Use Decimal to avoid float precision issues: order_amount is in cents
        rate_dec = Decimal(str(rate))
        order_amount_cents = Decimal(str(order_amount))
        # compute target currency smallest unit (cents) and round to whole cents
        order_amount = int((order_amount_cents * rate_dec).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if os.getenv("STRIPE_AUTOMATIC_TAX", "false").lower() in ("true", "1", "yes"):
            logger.info(f"Automatic tax calculation enabled, calculating tax for amount {order_amount}")
            tax = calculate_tax(currency=currency, amount=order_amount,
                                tax_code=get_config(ConfigKeysEnum.STRIPE_TAX_CODE, "txcd_10103101"),
                                tax_behavior=get_config(ConfigKeysEnum.STRIPE_TAX_BEHAVIOR, "exclusive"),
                                request_ip=ip_address,
                                reference=f"bundle:{user_bundle_order.bundle_id}")
            if tax:
                tax_excl = getattr(tax, "tax_amount_exclusive", 0)
                tax_incl = getattr(tax, "tax_amount_inclusive", 0)
                logger.info(f"Tax calculation result: exclusive={tax_excl}, inclusive={tax_incl}")
                order_amount = tax.amount_total
                metadata = {str(k): str(v) for k, v in {**metadata, "tax_calculation": tax.id}.items() if
                            v is not None}

                logger.info(
                    f"applying tax calculation: {tax.id} for order {user_bundle_order.id} with amount {order_amount}")
        payment_intent = stripe.PaymentIntent.create(
            amount=int(order_amount),
            currency=currency,
            payment_method_types=["card"],
            description=f"Bundle order ({user_bundle_order.order_type}) for bundle {user_bundle_order.bundle_id}",
            metadata=metadata,
            customer=customer.id
        )
        return payment_intent, tax

    except stripe.error.StripeError as e:
        raise CustomException(code=400, name=ErrorMessages.PAYMENT_INTENT_EXCEPTION,
                              details=f"Error while creating payment intent {str(e)}")


def calculate_tax(currency: str, amount: float, reference: str, tax_code: str,
                  tax_behavior: str, request_ip: str = None) -> stripe.tax.Calculation | None:
    try:
        logger.info(f"calculating tax for request ip {request_ip} and tax code {tax_code}")
        calc = stripe.tax.Calculation.create(
            currency=currency,
            line_items=[{
                "amount": int(amount),  # smallest currency unit
                "reference": reference,
                "tax_code": tax_code,
                "tax_behavior": tax_behavior,  # "exclusive" => tax added on top
            }],
            customer_details={
                # "address": customer_address,
                # "address_source": "billing",  # or "billing" depending on your flow
                "ip_address": request_ip,
            }
        )
        return calc
    except stripe.error.StripeError as e:
        logger.error(f"Error calculating tax: {str(e)}")
        return None


def create_wallet_top_up_intent(user_email: str, amount: float, currency: str, metadata: dict,
                                ip_address: str = None) -> tuple[PaymentIntent, stripe.tax.Calculation | None]:
    try:
        tax = None
        logger.info("Creating payment intent for wallet top-up")
        customers = stripe.Customer.list(email=user_email)
        if not customers:
            customer = stripe.Customer.create(email=user_email)
        else:
            customer = customers.get("data")[0]
        if os.getenv("STRIPE_AUTOMATIC_TAX", "false").lower() in ("true", "1", "yes"):
            logger.info(f"Automatic tax calculation enabled, calculating tax for amount {amount}")
            tax = calculate_tax(currency=currency, amount=amount,
                                tax_code=get_config(ConfigKeysEnum.STRIPE_TAX_CODE, "txcd_10103101"),
                                tax_behavior="inclusive", request_ip=ip_address,
                                reference=f"wallet_topup:{user_email}")
            if tax:
                tax_excl = getattr(tax, "tax_amount_exclusive", 0)
                tax_incl = getattr(tax, "tax_amount_inclusive", 0)
                logger.info(f"Tax calculation result: exclusive={tax_excl}, inclusive={tax_incl}")
                amount = tax.amount_total
                metadata = {str(k): str(v) for k, v in {**metadata, "tax_calculation": tax.id}.items() if
                            v is not None}

                logger.info(f"applying tax calculation: {tax.id} for wallet top up {user_email} with amount {amount}")

        payment_intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency,
            payment_method_types=["card"],
            description=f"Top-up for user {user_email} for amount {amount / 100:.2f} {currency}",
            metadata=metadata,
            customer=customer.id
        )
        return payment_intent, tax

    except stripe.error.StripeError as e:
        raise CustomException(code=400, name=ErrorMessages.PAYMENT_INTENT_EXCEPTION,
                              details=f"Error while creating payment intent {str(e)}")


def create_payment_ephemeral(customer_id: str):
    try:
        ephemeral = stripe.EphemeralKey.create(
            customer=customer_id,
            stripe_version='2024-09-30.acacia',
        )
        logger.info("Ephemeral created: %s", ephemeral)
        return ephemeral
    except stripe.error.StripeError as e:
        raise CustomException(code=400, name=ErrorMessages.PAYMENT_INTENT_EXCEPTION,
                              details=f"Error while creating ephemeral key: {str(e)}")


def stripe_get_payment_details(intent_code) -> PaymentDetailsDTO | None:
    if not intent_code:
        return None
    payment: PaymentIntent = stripe.PaymentIntent.retrieve(intent_code)
    if not payment.latest_charge:
        return None
    charge_id = payment.latest_charge
    charge: Charge = stripe.Charge.retrieve(charge_id)
    address_details = charge.billing_details.address
    if address_details:
        address = (address_details.get("country") or "N/A") + "," + (address_details.get("postal_code") or "N/A")
    else:
        address = "N/A"
    card_number = charge.payment_method_details.card.get("last4")
    card_type = charge.payment_method_details.get("type")
    card_brand = charge.payment_method_details.card.get("brand").title()
    card_display = f"{card_brand} ****{card_number}"
    return PaymentDetailsDTO.model_validate({
        "id": payment.id,
        "description": "",
        "payment_method": card_type,
        "card_number": card_number,
        "receipt_email": charge.receipt_email,
        "address": address,
        "card_display": card_display,
        "display_brand": card_brand,
        "country": charge.payment_method_details.card.get("country"),
    })


def parse_iso_datetime(datetime_str: str):
    """Parse ISO8601 datetime strings robustly.

    Returns a datetime.datetime on success or None on failure.
    Handles fractional seconds and timezone offsets via dateutil.isoparse with a
    fallback to datetime.fromisoformat.
    """
    if not datetime_str:
        return None
    try:
        return dateutil_parser.isoparse(datetime_str)
    except Exception:
        try:
            return datetime.fromisoformat(datetime_str)
        except Exception:
            return None


def truncate_two_decimals_decimal(value: float) -> Decimal:
    d = Decimal(str(value))
    return d.quantize(Decimal('0.00'), rounding=ROUND_DOWN)


def truncate_two_decimals_decimal_rounded(value: float) -> float:
    d = Decimal(str(value))
    return float(d.quantize(Decimal('0.00'), rounding=ROUND_UP))
