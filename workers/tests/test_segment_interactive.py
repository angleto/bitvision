"""Unit tests for the interactive-segment loader spec resolution.

The correctness core is ``resolve_sam2_spec``: it decides which SAM-2 config
name + checkpoint path ``build_sam2`` loads. The previous code passed a
HuggingFace model id where ``build_sam2`` expects a filesystem path, so the
model never loaded — these lock in the fix.
"""

from bvworkers.tasks.segment_interactive import (
    _DEFAULT_SAM2_CFG,
    _DEFAULT_SAM2_CKPT,
    resolve_sam2_spec,
)


def test_default_spec_is_the_baked_apache_checkpoint() -> None:
    cfg, ckpt = resolve_sam2_spec({})
    assert cfg == _DEFAULT_SAM2_CFG == "configs/sam2.1/sam2.1_hiera_t.yaml"
    assert ckpt == _DEFAULT_SAM2_CKPT == "/app/models/sam2/sam2.1_hiera_tiny.pt"
    # The default checkpoint MUST be a filesystem path, not a HF model id (the
    # historical bug: build_sam2 does torch.load on this string).
    assert ckpt.startswith("/")


def test_medsam_opt_in_overrides_checkpoint_and_config() -> None:
    env = {
        "BVP_MEDSAM_CKPT": "/mnt/medsam/MedSAM2_latest.pt",
        "BVP_MEDSAM_CFG": "configs/sam2.1/sam2.1_hiera_t512.yaml",
    }
    cfg, ckpt = resolve_sam2_spec(env)
    assert ckpt == "/mnt/medsam/MedSAM2_latest.pt"
    assert cfg == "configs/sam2.1/sam2.1_hiera_t512.yaml"


def test_checkpoint_override_keeps_default_config() -> None:
    cfg, ckpt = resolve_sam2_spec({"BVP_MEDSAM_CKPT": "/mnt/x.pt"})
    assert ckpt == "/mnt/x.pt"
    assert cfg == _DEFAULT_SAM2_CFG


def test_config_override_keeps_default_checkpoint() -> None:
    cfg, ckpt = resolve_sam2_spec({"BVP_MEDSAM_CFG": "configs/custom.yaml"})
    assert cfg == "configs/custom.yaml"
    assert ckpt == _DEFAULT_SAM2_CKPT


def test_empty_env_values_fall_back_to_defaults() -> None:
    # A ConfigMap/env that sets the keys to empty strings must not blank the
    # spec (``env.get(...) or default``).
    cfg, ckpt = resolve_sam2_spec({"BVP_MEDSAM_CKPT": "", "BVP_MEDSAM_CFG": ""})
    assert cfg == _DEFAULT_SAM2_CFG
    assert ckpt == _DEFAULT_SAM2_CKPT
