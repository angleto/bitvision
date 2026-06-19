"""Build the engine's runtime config + ProfileOptions from Settings (BVP_DEID_*)."""

from __future__ import annotations

from dataclasses import dataclass

from bvphoenix.config import get_settings
from bvphoenix.services.deid.profile_table import ProfileOptions


@dataclass(frozen=True)
class EngineConfig:
    salt: str
    org_root_uid: str


def build_profile_options() -> ProfileOptions:
    s = get_settings()
    return ProfileOptions(
        date_policy=s.deid_date_policy,
        clean_descriptors=s.deid_clean_descriptors,
        retain_patient_characteristics=s.deid_retain_patient_characteristics,
        retain_device_identity=s.deid_retain_device_identity,
        retain_safe_private=s.deid_retain_safe_private,
        method_version=s.deid_method_version,
        safe_private_version=s.deid_safe_private_version,
    )


def get_engine_config() -> EngineConfig:
    s = get_settings()
    return EngineConfig(salt=s.deid_secret_salt, org_root_uid=s.deid_org_root_uid)


__all__ = ["EngineConfig", "build_profile_options", "get_engine_config"]
