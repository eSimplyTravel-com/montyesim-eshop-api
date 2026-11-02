"""The app cache key must only move when a sync actually wrote something.

Monty re-sends its catalogue as ~1,000 "update" callbacks per hour. Before this
change every callback deactivated the bundle, re-wrote it and bumped
APP_CACHE_KEY, which emptied the home/bundle cache continuously.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.app import BundleModel
from app.services.callback_service import CallbackService
from app.services.sync_service import SyncService

STORED = {
    "bundle_code": "b1",
    "price": 9.5,
    "price_display": "9.50 EUR",
    "countries": [{"id": "c1", "country": "Germany"}],
    "is_active": True,
}


def _bundle(data=None, is_active=True):
    bundle = MagicMock()
    bundle.bundle_code = "b1"
    bundle.countries = []
    bundle.bundle_region = []
    bundle.is_active = is_active
    bundle.model_dump.return_value = dict(data if data is not None else STORED)
    return bundle


@pytest.fixture
def sync_service():
    with patch("app.services.sync_service.BundleRepo"), patch("app.services.sync_service.TagRepo"), \
            patch("app.services.sync_service.BundleTagRepo"), patch("app.services.sync_service.ConfigRepo"), \
            patch("app.services.sync_service.esim_hub_service_instance"):
        service = SyncService()
    service._SyncService__bundle_repo = MagicMock()
    service._SyncService__tag_repo = MagicMock()
    service._SyncService__bundle_tag_repo = MagicMock()
    service._SyncService__config_repo = MagicMock()
    return service


def _stored(data=None, is_active=True):
    # Mirrors what the update path writes: timestamps/id may sit inside data.
    payload = dict(data if data is not None else STORED)
    payload["updated_at"] = "2026-11-01T10:00:00"
    return BundleModel(id="b1", data=payload, is_active=is_active)


@pytest.mark.asyncio
async def test_identical_bundle_writes_nothing(sync_service):
    sync_service._SyncService__bundle_repo.get_by_id.return_value = _stored()
    changed = await sync_service.sync_bundle(_bundle())
    assert changed is False
    sync_service._SyncService__bundle_repo.update.assert_not_called()
    sync_service._SyncService__bundle_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_price_change_is_written(sync_service):
    sync_service._SyncService__bundle_repo.get_by_id.return_value = _stored()
    with patch("app.services.grouping_service.GroupingService"):
        changed = await sync_service.sync_bundle(_bundle({**STORED, "price": 10.0}))
    assert changed is True
    sync_service._SyncService__bundle_repo.update.assert_called()


@pytest.mark.asyncio
async def test_status_change_is_written(sync_service):
    sync_service._SyncService__bundle_repo.get_by_id.return_value = _stored(is_active=False)
    with patch("app.services.grouping_service.GroupingService"):
        changed = await sync_service.sync_bundle(_bundle())
    assert changed is True
    sync_service._SyncService__bundle_repo.update.assert_called()


@pytest.mark.asyncio
async def test_unreadable_stored_row_counts_as_changed(sync_service):
    sync_service._SyncService__bundle_repo.get_by_id.return_value = BundleModel(id="b1", data=None, is_active=True)
    with patch("app.services.grouping_service.GroupingService"):
        changed = await sync_service.sync_bundle(_bundle())
    assert changed is True


@pytest.mark.asyncio
async def test_deactivate_if_present(sync_service):
    repo = sync_service._SyncService__bundle_repo
    repo.get_by_id.return_value = None
    assert await sync_service.deactivate_if_present("b1") is False
    repo.get_by_id.return_value = _stored(is_active=False)
    assert await sync_service.deactivate_if_present("b1") is False
    repo.update.assert_not_called()
    repo.get_by_id.return_value = _stored(is_active=True)
    assert await sync_service.deactivate_if_present("b1") is True
    repo.update.assert_called_once_with(record_id="b1", data={"is_active": False})


def _callback(hub_result=None, hub_error=None, sync_changed=False, deactivated=False):
    service = CallbackService.__new__(CallbackService)
    hub = MagicMock()
    hub.get_bundle_by_id = AsyncMock(return_value=hub_result, side_effect=hub_error)
    sync = MagicMock()
    sync.sync_bundle = AsyncMock(return_value=sync_changed)
    sync.deactivate_if_present = AsyncMock(return_value=deactivated)
    sync.delete_bundle = AsyncMock()
    service._CallbackService__esim_hub_service = hub
    service._CallbackService__sync_service = sync
    return service, sync


async def _run(service, operation="update", reseller_id=None):
    await service._CallbackService__run_one_sync_internal("b1", operation, reseller_id)


@pytest.mark.asyncio
async def test_update_unchanged_does_not_bump_cache():
    service, sync = _callback(hub_result=_bundle(), sync_changed=False)
    await _run(service)
    sync.delete_bundle.assert_not_called()  # no deactivate-then-reactivate double write
    sync.update_sync_version.assert_not_called()


@pytest.mark.asyncio
async def test_update_changed_bumps_cache():
    service, sync = _callback(hub_result=_bundle(), sync_changed=True)
    await _run(service)
    sync.update_sync_version.assert_called_once()


@pytest.mark.asyncio
async def test_update_hub_error_leaves_bundle_alone():
    service, sync = _callback(hub_error=RuntimeError("hub timeout"))
    await _run(service)
    sync.delete_bundle.assert_not_called()
    sync.deactivate_if_present.assert_not_called()
    sync.update_sync_version.assert_not_called()


@pytest.mark.asyncio
async def test_update_for_bundle_we_never_stored_does_not_bump_cache():
    service, sync = _callback(hub_result=None, deactivated=False)
    await _run(service)
    sync.deactivate_if_present.assert_awaited_once_with(bundle_id="b1")
    sync.update_sync_version.assert_not_called()


@pytest.mark.asyncio
async def test_update_removed_bundle_deactivates_and_bumps():
    service, sync = _callback(hub_result=None, deactivated=True)
    await _run(service)
    sync.update_sync_version.assert_called_once()


@pytest.mark.asyncio
async def test_foreign_reseller_non_update_does_nothing():
    service, sync = _callback()
    with patch("app.services.callback_service.get_config", return_value="ours"):
        await _run(service, operation="assign", reseller_id="someone-else")
    sync.sync_bundle.assert_not_called()
    sync.update_sync_version.assert_not_called()
