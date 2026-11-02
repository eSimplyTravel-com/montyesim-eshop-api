import json
import math
import uuid
from typing import List

from loguru import logger

from app.config.config import esim_hub_service_instance
from app.config.db import ConfigKeysEnum
from app.models.app import TagModel, BundleModel, BundleTagModel
from app.repo.bundle_repo import BundleRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.config_repo import ConfigRepo
from app.repo.tag_repo import TagRepo
from app.schemas.home import CountryDTO, RegionDTO, BundleDTO


class SyncService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__bundle_repo = BundleRepo()
        self.__tag_repo = TagRepo()
        self.__bundle_tag_repo = BundleTagRepo()
        self.__config_repo = ConfigRepo()

    async def sync_bundles(self, page_index=1):
        logger.info("Syncing bundles started")
        page_size = 100
        all_bundle_response = await self.__esim_hub_service.get_all_bundles(page_index=page_index, page_size=page_size)
        all_bundles_count = all_bundle_response.total_rows
        pages = math.ceil(all_bundles_count / page_size)
        logger.info(f"all bundle count: {all_bundles_count}, pages: {pages}")
        for index, bundle in enumerate(all_bundle_response.bundles):
            logger.info("Syncing bundle {}".format(index + 1))
            await self.sync_bundle(bundle)
        for page in range(2, pages + 1):
            all_bundle_response = await self.__esim_hub_service.get_all_bundles(page_index=page, page_size=page_size)
            logger.info(f"Syncing bundle page: {page} of size: {pages}")
            for index, bundle in enumerate(all_bundle_response.bundles):
                logger.info("Syncing bundle {}".format(index + 1))
                await self.sync_bundle(bundle)
        logger.info("Syncing bundles finished")

    async def sync_bundle(self, bundle: BundleDTO) -> bool:
        """Store the hub's bundle. Returns False only when the stored copy was
        already identical and nothing was written, so callers can skip
        invalidating the app cache. Any doubt (errors, unreadable row) -> True."""
        countries = bundle.countries
        regions = bundle.bundle_region
        try:
            await self.__sync_country_tags(countries)
        except Exception as e:
            logger.error(f"Error while syncing country tags: {e}")
        try:
            regions = list(filter(lambda r: r.region_code != "GLOBAL", regions))
            await self.__sync_region_tags(regions)
        except Exception as e:
            logger.error(f"Error while syncing region tags: {e}")
        try:
            existing = self.__bundle_repo.get_by_id(bundle.bundle_code)
            if not existing:
                await self.__handle_create_bundle(bundle=bundle, countries=countries, regions=regions)
            elif self.__is_unchanged(existing, bundle):
                # Monty re-sends the whole catalogue as "update" callbacks; most
                # of them carry no change. Skip the row/translation/status
                # writes, but still repair any missing tag links (reads only).
                logger.info(f"bundle {bundle.bundle_code} unchanged, skipping write")
                self.__ensure_bundle_tags(bundle=bundle, countries=countries, regions=regions)
                return False
            else:
                await self.__handle_update_bundle(bundle=bundle, countries=countries, regions=regions)
            await self.update_bundle_status(bundle_id=bundle.bundle_code, status=bundle.is_active)
        except Exception as e:
            logger.error(e)
        return True

    @staticmethod
    def __comparable(data: dict) -> str:
        # Stored rows come back from jsonb; normalise both sides through JSON so
        # float/str/None representations compare equal. Timestamps and ids are
        # stored inconsistently between the create and update paths, so ignore them.
        trimmed = {k: v for k, v in data.items() if k not in ("updated_at", "created_at", "id")}
        return json.dumps(trimmed, sort_keys=True, default=str)

    @classmethod
    def __is_unchanged(cls, existing, bundle: BundleDTO) -> bool:
        stored = getattr(existing, "data", None)
        if not isinstance(stored, dict):
            return False
        if getattr(existing, "is_active", None) != bundle.is_active:
            return False
        try:
            incoming = bundle.model_dump(mode="json")
        except Exception:
            return False
        if not isinstance(incoming, dict):
            return False
        return cls.__comparable(stored) == cls.__comparable(incoming)

    async def deactivate_if_present(self, bundle_id: str) -> bool:
        """Deactivate a stored, active bundle. Returns True only if a write happened,
        so an "update" for a bundle we never stored (e.g. not assigned to us) costs
        one read and does not invalidate the app cache."""
        try:
            existing = self.__bundle_repo.get_by_id(bundle_id)
        except Exception as e:
            logger.error(f"error while reading bundle {bundle_id=} {e}")
            return False
        if not existing or getattr(existing, "is_active", None) is False:
            return False
        await self.delete_bundle(bundle_id=bundle_id)
        return True

    async def resync_bundles(self, bundle: BundleDTO):
        await self.delete_bundle(bundle_id=bundle.bundle_code)
        await self.sync_bundle(bundle=bundle)

    def update_sync_version(self):
        new_key = uuid.uuid4().hex
        old_config = self.__config_repo.get_first_by({"key": ConfigKeysEnum.APP_CACHE_KEY})
        if not old_config:
            self.__config_repo.create({"key": ConfigKeysEnum.APP_CACHE_KEY, "value": new_key})
        else:
            self.__config_repo.update_by(where={"key": ConfigKeysEnum.APP_CACHE_KEY}, data={"value": new_key})

    async def delete_bundle(self, bundle_id: str):
        try:
            await self.update_bundle_status(bundle_id=bundle_id, status=False)
            logger.info(f"deleted bundle {bundle_id}")
        except Exception as e:
            logger.error(f"error while deleting bundle {bundle_id=} {e}")

    async def update_bundle_status(self, bundle_id: str, status: bool):
        try:
            self.__bundle_repo.update(record_id=bundle_id, data={"is_active": status})
        except Exception as e:
            logger.error(f"error while deactivating bundle {bundle_id=} {e}")

    async def __sync_country_tags(self, countries: List[CountryDTO]):
        for country in countries:
            if not self.__tag_repo.get_first_by({"id": country.id}):
                self.__tag_repo.create(
                    TagModel(name=country.country, icon=country.icon, tag_group_id=1, data=country.model_dump(),
                             id=country.id).model_dump(
                        exclude={"updated_at", "created_at"}))

    async def __sync_region_tags(self, regions: List[RegionDTO]):
        for region in regions:
            if region.region_code == "GLOBAL":
                continue
            if not self.__tag_repo.get_first_by({"id": region.guid}):
                self.__tag_repo.create(
                    TagModel(name=region.region_name, icon=region.icon, tag_group_id=2, data=region.model_dump(),
                             id=region.guid).model_dump(
                        exclude={"updated_at", "created_at"}))

    async def __handle_create_bundle(self, bundle: BundleDTO, countries: List[CountryDTO], regions: List[RegionDTO]):
        self.__bundle_repo.create(BundleModel(id=bundle.bundle_code, is_active=True,
                                              data=bundle.model_dump(
                                                  exclude={"updated_at", "created_at", "id"})).model_dump(
            exclude={"updated_at", "created_at"}))
        for country in countries:
            self.__bundle_tag_repo.create(
                BundleTagModel(bundle_id=bundle.bundle_code, tag_id=country.id, id=None).model_dump(
                    exclude={"updated_at", "created_at", "id"}))
        for region in regions:
            tag = self.__tag_repo.get_first_by({"name": region.region_name})
            self.__bundle_tag_repo.create(
                BundleTagModel(bundle_id=bundle.bundle_code, tag_id=tag.id, id=None).model_dump(
                    exclude={"updated_at", "created_at", "id"}))
            logger.debug("adding region for bundle {}".format(bundle.bundle_code))
        from app.services.grouping_service import GroupingService
        grouping_service = GroupingService()
        grouping_service.update_bundle_translation(bundle_code=bundle.bundle_code)

    async def __handle_update_bundle(self, bundle: BundleDTO, countries: List[CountryDTO], regions: List[RegionDTO]):
        logger.info(f"bundle already added, updating it {bundle.bundle_code}")
        self.__bundle_repo.update(record_id=bundle.bundle_code,
                                  data=BundleModel(id=bundle.bundle_code, is_active=True,
                                                   data=bundle.model_dump()).model_dump(
                                      exclude={"updated_at", "created_at", "id"}))
        self.__ensure_bundle_tags(bundle=bundle, countries=countries, regions=regions)
        from app.services.grouping_service import GroupingService
        grouping_service = GroupingService()
        grouping_service.update_bundle_translation(bundle_code=bundle.bundle_code)

    def __ensure_bundle_tags(self, bundle: BundleDTO, countries: List[CountryDTO], regions: List[RegionDTO]):
        for country in countries:
            if not self.__bundle_tag_repo.get_first_by({"bundle_id": bundle.bundle_code, "tag_id": country.id}):
                self.__bundle_tag_repo.create(
                    BundleTagModel(bundle_id=bundle.bundle_code, tag_id=country.id, id=None).model_dump(
                        exclude={"updated_at", "created_at", "id"}))
        for region in regions:
            if not self.__bundle_tag_repo.get_first_by(
                    {"bundle_id": bundle.bundle_code, "tag_id": region.guid}):
                self.__bundle_tag_repo.create(
                    BundleTagModel(bundle_id=bundle.bundle_code, tag_id=region.guid, id=None).model_dump(
                        exclude={"updated_at", "created_at", "id"}))
                logger.debug("updating region for bundle {}".format(bundle.bundle_code))
