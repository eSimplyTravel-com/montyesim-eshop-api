from fastapi import APIRouter, HTTPException, Request
from fastapi.params import Query

from app.config.helper import fake_payment_webhook_enabled

from app.schemas.response import ResponseHelper
from app.services.callback_service import CallbackService

router = APIRouter()

service = CallbackService()


@router.post("/payment-webhook")
async def payment_webhook(request: Request):
    await service.handle_payment_webhook(request)


@router.post("/payment-webhook-fake")
async def payment_webhook_fake(request: Request):
    # This runs the full payment handler on a body the caller writes, with no Stripe signature and
    # no authentication: anyone holding an order id (the browser is given one at checkout) could
    # fulfil an unpaid order. Off unless ENABLE_FAKE_PAYMENT_WEBHOOK is explicitly set.
    if not fake_payment_webhook_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    await service.handle_payment_webhook_fake(request)


@router.post("/plan_status_callback")
async def consumption_limit(request: Request):
    await service.handle_plan_event_callback(callback_request=request)
    return ResponseHelper.success_response()


@router.post("/bundle/sync-all")
def bundle_sync_all(request: Request, page_index=Query(default=1, description="Page Index")):
    return service.handle_sync_all_bundles(page_index=page_index)


@router.post("/bundle/sync-one")
async def bundle_sync_all(request: Request):
    return await service.handle_sync_one_bundle(request=request)


@router.post("/bundle/sync-one-by-id/{id}")
def bundle_sync_all(request: Request, id: str):
    return service.handle_sync_one_bundle_by_id(request=request, id=id)


@router.post("/currency/exchange_rate")
async def currency_exchange_rate(request: Request):
    return await service.handle_exchange_rate_update(request=request)
