import asyncio
import os
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from loguru import logger

from app.config.config import esim_hub_service_instance
from app.config.utils import truncate_two_decimals_decimal_rounded
from app.repo.currency_repo import CurrencyRepo
from app.schemas.app import ExchangeRate
from app.services.sync_service import SyncService

load_dotenv()


class SchedulerService:
    def __init__(self):
        self.__currency_repo = CurrencyRepo()
        self.scheduler = BackgroundScheduler()
        self.__esim_hub_service = esim_hub_service_instance()
        self.__started = False
        self.__sync_service = SyncService()

    async def _async_scheduled_task(self):
        logger.info(f"Scheduled task execution start at {time.strftime('%X')}")
        currencies = self.__currency_repo.list(where={"default_currency": "USD"})
        if not currencies:
            return
        names = [currency.name for currency in currencies]
        rates = await self.__esim_hub_service.get_exchange_rates(currency_codes=names)
        logger.info(f"exchange from esim hub: {rates}")
        for rate in rates:
            logger.info(
                f"updating currency {rate.currency_code=} to  {rate.current_rate=}")
            self.__handle_exchange_rate(rate)
            self.__handle_inverse_rate(rate)

        self.__sync_service.update_sync_version()
        logger.info(f"Scheduled task execution ends at {time.strftime('%X')}")

    def __handle_exchange_rate(self, rate: ExchangeRate):
        logger.info(
            f"updating currency {rate.currency_code=} to USD with rate {rate.current_rate=}")

        old_record = self.__currency_repo.get_first_by(
            where={"default_currency": "USD", "name": rate.currency_code})
        if old_record:
            self.__currency_repo.update_by(
                {"default_currency": "USD", "name": rate.currency_code},
                data={'rate': rate.current_rate}
            )
        else:
            self.__currency_repo.create(
                data={"default_currency": "USD", "name": rate.currency_code, "rate": rate.current_rate}
            )

    def __handle_inverse_rate(self, rate: ExchangeRate):
        inverse_rate = truncate_two_decimals_decimal_rounded(1 / rate.current_rate if rate.current_rate != 0 else 0)
        logger.info(
            f"updating currency USD to  {rate.currency_code=} with inverse rate {inverse_rate=}")

        old_record = self.__currency_repo.get_first_by(
            where={"default_currency": rate.currency_code, "name": "USD"})
        if old_record:
            self.__currency_repo.update_by(
                {"default_currency": rate.currency_code, "name": "USD"},
                data={'rate': inverse_rate}
            )
        else:
            self.__currency_repo.create(
                data={"default_currency": rate.currency_code, "name": "USD", "rate": inverse_rate}
            )

    def scheduled_task(self):
        try:
            # Run the async task within the scheduler's background thread
            asyncio.run(self._async_scheduled_task())
        except Exception as e:
            logger.error(f"Error in scheduled task: {e}")

    def start_scheduler(self):
        if self.__started:
            logger.info("Scheduler already started; skipping duplicate start.")
            return
        interval_seconds = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", 60 * 60 * 12))  # default to 12 hours
        misfire_grace = int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", 60))
        max_instances = int(os.getenv("SCHEDULER_MAX_INSTANCES", 1))
        self.scheduler.add_job(
            self.scheduled_task,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id="my_task",
            name="Update exchange rates",
            replace_existing=True,
            max_instances=max_instances,
            coalesce=True,
            misfire_grace_time=misfire_grace,
        )
        retry_seconds = int(os.getenv("STRIPE_EVENT_RETRY_SECONDS", 300))
        self.scheduler.add_job(
            self.__retry_stripe_events,
            trigger=IntervalTrigger(seconds=retry_seconds),
            id="retry_stripe_events",
            name="Retry unfinished Stripe events",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=misfire_grace,
        )
        self.scheduler.start()
        self.__started = True
        logger.info("Scheduler started")

    @staticmethod
    def __retry_stripe_events():
        # Imported here: CallbackService pulls in the bundle/sync services and would make the import circular.
        from app.services.callback_service import CallbackService
        try:
            CallbackService().retry_stripe_events()
        except Exception as e:
            logger.error(f"STRIPE_EVENT_RETRY_FAILED {e}")

    def shutdown_scheduler(self):
        self.scheduler.shutdown(wait=False)
        self.__started = False
        logger.info("Scheduler shut down")
