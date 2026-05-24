"""Connector interface shared by all source plugins (TCIA, OpenNeuro, ...)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, kw_only=True)
class DiscoveredStudy:
    """A study identified by a connector, ready to be downloaded."""

    source: str
    external_id: str
    study_instance_uid: str | None
    title: str
    license: str
    attribution: str
    source_url: str
    metadata: dict[str, str]


class Connector(Protocol):
    """Every source connector implements this protocol."""

    name: str
    description: str

    def discover(self, collection: str) -> AsyncIterator[DiscoveredStudy]:
        """Yield studies available in the given source collection."""
        ...

    async def download(self, study: DiscoveredStudy, dest_prefix: str) -> list[str]:
        """Download the study's DICOM files to S3 under `dest_prefix`.
        Returns the list of S3 keys written."""
        ...
