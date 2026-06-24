"""Coverage for the ``tree`` archive layout of the Fascicolo export.

``layout="tree"`` (services/patient_export.py) mirrors the patient's
curated Folder tree on disk: every study / document lands under the
path of the folder that contains it, named by its clinical
description / title instead of a UUID, so the unzipped archive is
navigable exactly like the in-app folder view. The model's no-orphan
invariant (every document lives in >= 1 folder, root at minimum)
means there is no synthetic "_unfiled" bucket — an item is always
placed where it is actually filed.

Two layers are exercised:

* the pure :class:`_ExportNamer` + :func:`_sanitize_component`
  (deterministic, no DB) — flat-parity, tree paths, OS-style
  de-duplication, extension handling;
* :func:`_build_export_plan` against a real DB (skips when none is
  reachable) with a small Folder tree, asserting member paths reflect
  the folders and that ``flat`` is byte-identical to the legacy layout.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    Document,
    Folder,
    FolderItem,
    ImagingStudy,
    Instance,
    Patient,
    Series,
    User,
)
from bvphoenix.db.models.principals import Subject
from bvphoenix.services.patient_export import (
    _build_export_plan,
    _build_folder_layout,
    _ExportNamer,
    _sanitize_component,
)
from tests.conftest import skip_if_no_db

# ---------------------------------------------------------------------------
# Pure helpers — no DB, fully deterministic
# ---------------------------------------------------------------------------


def test_sanitize_component_preserves_readable_names() -> None:
    # The user types accents, spaces and em-dashes in folder names;
    # only true path-breakers (/, \, :, control chars) are replaced.
    assert _sanitize_component("2024-05-20 RM addome (Primovist) — diagnosi") == (
        "2024-05-20 RM addome (Primovist) — diagnosi"
    )
    assert _sanitize_component("a/b\\c:d") == "a-b-c-d"
    assert _sanitize_component("  ...trim... ") == "trim"  # surrounding ws + dots stripped
    assert _sanitize_component("") == "senza-nome"
    assert _sanitize_component("\x00\x01") == "senza-nome"
    assert len(_sanitize_component("x" * 500, maxlen=80)) == 80


def _study(desc: str | None = "TC addome", *, sid: uuid.UUID | None = None) -> ImagingStudy:
    return ImagingStudy(
        id=sid or uuid.uuid4(),
        study_description=desc,
        modalities=["CT"],
        study_date=date(2024, 9, 16),
    )


def test_namer_flat_is_legacy_layout() -> None:
    namer = _ExportNamer("flat", {})
    study = _study()
    sr = namer.study_root(study)
    assert sr == f"studies/{study.id}"
    series = Series(id=uuid.uuid4(), series_description="torace", modality="CT", series_number=2)
    series_root = namer.series_root(sr, 1, series)
    assert series_root == f"studies/{study.id}/series_1"
    assert namer.series_manifest(series_root) == f"studies/{study.id}/series_1/manifest.json"
    inst = Instance(id=uuid.uuid4(), sop_instance_uid="1.2.3", instance_number=5)
    assert namer.instance(series_root, inst) == f"studies/{study.id}/series_1/1.2.3.dcm"
    assert not namer.tree


def test_namer_tree_paths_use_folders_and_names() -> None:
    study = _study(desc="TC addome completo")
    paths = {("study", study.id): "2024/2024-09-16 TC addome (post-op)"}
    namer = _ExportNamer("tree", paths)
    sr = namer.study_root(study)
    assert sr == "2024/2024-09-16 TC addome (post-op)/TC addome completo"
    series = Series(
        id=uuid.uuid4(), series_description="torace mdc", modality="CT", series_number=2
    )
    series_root = namer.series_root(sr, 1, series)
    assert series_root == f"{sr}/serie_001_torace mdc"
    assert namer.series_manifest(series_root) == f"{series_root}/_serie.json"
    inst = Instance(id=uuid.uuid4(), sop_instance_uid="9.9.9", instance_number=7)
    assert namer.instance(series_root, inst) == f"{series_root}/0007.dcm"
    # Instance with no number falls back to a UID tail, never collides.
    inst2 = Instance(id=uuid.uuid4(), sop_instance_uid="1.2.840.55", instance_number=None)
    assert namer.instance(series_root, inst2).endswith(".dcm")


def test_namer_tree_dedups_colliding_names() -> None:
    # Two studies with the same description filed in the same folder
    # must not collide on one ZIP member path.
    s1 = _study(desc="TC addome")
    s2 = _study(desc="TC addome")
    paths = {("study", s1.id): "2024", ("study", s2.id): "2024"}
    namer = _ExportNamer("tree", paths)
    r1 = namer.study_root(s1)
    r2 = namer.study_root(s2)
    assert r1 == "2024/TC addome"
    assert r2 == "2024/TC addome (2)"
    assert r1 != r2


def test_namer_tree_unfiled_item_lands_at_root() -> None:
    # A study with no folder mapping (defensive: should not happen given
    # the no-orphan invariant) lands at the archive root, not in a
    # synthetic bucket.
    study = _study(desc="orfano")
    namer = _ExportNamer("tree", {})
    assert namer.study_root(study) == "orfano"


# ---------------------------------------------------------------------------
# Integration — _build_folder_layout + _build_export_plan against a DB
# ---------------------------------------------------------------------------


pytestmark_db = [pytest.mark.asyncio, skip_if_no_db]


@pytest_asyncio.fixture
async def filed_patient(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[Patient, User, Folder, Document]]:
    """Patient + materialised root + ``2024`` + a dated leaf folder, with
    a Document filed in the leaf. Mirrors the real fascicolo shape
    (root empty, everything under year/exam folders)."""
    sid = uuid.uuid4()
    db_session.add(Subject(id=sid, kind="user", display_name=f"layout-test-{sid}"))
    await db_session.flush()
    user = User(
        subject_id=sid, email=f"layout-{sid}@example.com", password_hash=None, is_admin=False
    )
    db_session.add(user)
    await db_session.flush()
    patient = Patient(
        id=uuid.uuid4(), managed_by_subject_id=user.subject_id, display_name="Layout Test"
    )
    db_session.add(patient)
    await db_session.flush()

    root = Folder(
        id=uuid.uuid4(),
        name="__root__",
        owner_subject_id=user.subject_id,
        parent_folder_id=None,
        patient_id=patient.id,
        is_root=True,
    )
    db_session.add(root)
    await db_session.flush()
    year = Folder(
        id=uuid.uuid4(),
        name="2024",
        owner_subject_id=user.subject_id,
        parent_folder_id=root.id,
        patient_id=patient.id,
    )
    db_session.add(year)
    await db_session.flush()
    leaf = Folder(
        id=uuid.uuid4(),
        name="2024-09-16 TC addome (post-op)",
        owner_subject_id=user.subject_id,
        parent_folder_id=year.id,
        patient_id=patient.id,
    )
    db_session.add(leaf)
    await db_session.flush()

    doc = Document(
        id=uuid.uuid4(),
        patient_id=patient.id,
        uploaded_by_subject_id=user.subject_id,
        kind_id="radiology_report",
        provenance_id="manual_entry",
        authority_id="original",
        title="referto.pdf",
        text=None,
        file_s3_key=f"raw/test/{uuid.uuid4()}.pdf",
        file_content_type="application/pdf",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(FolderItem(folder_id=leaf.id, resource_kind="document", resource_id=doc.id))
    await db_session.flush()

    yield patient, user, leaf, doc


@pytest.mark.asyncio
@skip_if_no_db
async def test_folder_layout_maps_document_to_leaf_path(
    db_session: AsyncSession,
    filed_patient: tuple[Patient, User, Folder, Document],
) -> None:
    patient, _user, _leaf, doc = filed_patient
    layout = await _build_folder_layout(db_session, patient)
    assert layout[("document", doc.id)] == "2024/2024-09-16 TC addome (post-op)"


@pytest.mark.asyncio
@skip_if_no_db
async def test_tree_layout_files_document_under_its_folder(
    db_session: AsyncSession,
    filed_patient: tuple[Patient, User, Folder, Document],
) -> None:
    patient, user, _leaf, _doc = filed_patient
    _manifest, work = await _build_export_plan(
        db_session, user, patient, {"documents"}, layout="tree"
    )
    names = {w["name"] for w in work}
    # The title already carries ".pdf"; the namer must not double it.
    assert "2024/2024-09-16 TC addome (post-op)/referto.pdf" in names


@pytest.mark.asyncio
@skip_if_no_db
async def test_flat_layout_is_unchanged(
    db_session: AsyncSession,
    filed_patient: tuple[Patient, User, Folder, Document],
) -> None:
    patient, user, _leaf, doc = filed_patient
    _manifest, work = await _build_export_plan(
        db_session, user, patient, {"documents"}, layout="flat"
    )
    names = {w["name"] for w in work}
    assert f"documents/{doc.id}.pdf" in names
    # No tree paths leaked into the flat layout.
    assert not any(n.startswith("2024/") for n in names)
