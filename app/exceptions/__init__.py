from typing import Any

from app.config.constants import ErrorMessages


class CustomException(Exception):
    def __init__(self, name: ErrorMessages | str, details: str, code: int):
        self.name = name
        self.details = details
        self.code = code
        super().__init__(f"{name}: {details}")


class EsimHubException(CustomException):
    def __init__(self, details: str | dict | Any):
        self.name = "ESIMHub Exception"
        self.details = details
        self.code = 400
        if isinstance(details, dict):
            self.details = details["message"] or details["code"]
        else:
            self.details = str(details)
        super().__init__(name=ErrorMessages.ESIM_HUB_EXCEPTION, details=self.details, code=self.code)


class BadRequestException(CustomException):
    def __init__(self, details: str):
        super().__init__(code=400, name=ErrorMessages.REQUEST_FAILED, details=details)


class FulfilmentNeedsReviewError(CustomException):
    """A paid order whose eSIM Hub outcome is unknown. Never retried automatically: ordering again
    could buy a second eSIM, so it is parked for a human to reconcile in the Monty portal."""

    def __init__(self, details: str):
        super().__init__(code=409, name=ErrorMessages.REQUEST_FAILED, details=details)


class DatabaseException(CustomException):
    def __init__(self, details: str):
        super().__init__(code=400, name=ErrorMessages.REQUEST_FAILED, details=details)


class DCBException(CustomException):
    def __init__(self, details: str | dict | Any, error: ErrorMessages = ErrorMessages.REQUEST_FAILED):
        self.name = "DCB Exception"
        self.details = details
        self.code = 400
        if isinstance(details, dict):
            self.details = details["message"] or details["code"]
        else:
            self.details = str(details)
        super().__init__(name=error, details=self.details, code=self.code)
