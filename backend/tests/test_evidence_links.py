"""Cross-patient guard for the Evidenze e sintesi DSL.

The parser is exercised in isolation; the validator behaviour is
covered via the live clinical-notes endpoint in DB tests under
``test_clinical_notes_*`` (skipped without DATABASE_URL).
"""

from __future__ import annotations

import uuid

from bvphoenix.services.evidence_links import (
    Mention,
    iter_mention_kinds,
    parse_mentions,
)


def test_parse_mentions_extracts_kinds_and_uuids() -> None:
    sid = uuid.uuid4()
    fid = uuid.uuid4()
    body = (
        f"Vedi @study:{sid} per i reperti recenti, "
        f"poi rivedere la cartella @folder:{fid} "
        f"con tag @tag:liver e @tag:priority.high"
    )
    out = parse_mentions(body)
    by_kind = {m.kind: m for m in out}
    assert by_kind["study"].target_id == sid
    assert by_kind["folder"].target_id == fid
    # Tag mentions: both @tag:liver and @tag:priority.high should appear.
    tags = [m for m in out if m.kind == "tag"]
    assert {t.tag_value for t in tags} == {"liver", "priority.high"}


def test_parse_mentions_ignores_non_uuid_after_at() -> None:
    out = parse_mentions("@study:not-a-uuid and @folder:also bad")
    assert out == []


def test_parse_mentions_link_form_carries_title() -> None:
    sid = uuid.uuid4()
    body = f"Vedi [Studio di questa ceppa](@study:{sid}) per i reperti."
    out = parse_mentions(body)
    assert len(out) == 1
    m = out[0]
    assert m.kind == "study"
    assert m.target_id == sid
    assert m.title == "Studio di questa ceppa"


def test_parse_mentions_mixes_link_form_and_bare_form() -> None:
    sid_a = uuid.uuid4()
    sid_b = uuid.uuid4()
    body = (
        f"Linkato [Visita 1](@study:{sid_a}) e bare @study:{sid_b} "
        f"più [#priorità](@tag:priorita) e @tag:liver "
    )
    out = parse_mentions(body)
    studies = [m for m in out if m.kind == "study"]
    tags = [m for m in out if m.kind == "tag"]
    assert {m.target_id for m in studies} == {sid_a, sid_b}
    # The link form mention reports its title, the bare form reports None.
    titled = next(m for m in studies if m.target_id == sid_a)
    bare = next(m for m in studies if m.target_id == sid_b)
    assert titled.title == "Visita 1"
    assert bare.title is None
    assert {t.tag_value for t in tags} == {"priorita", "liver"}


def test_parse_mentions_link_form_is_not_double_counted_as_bare() -> None:
    # The bare regex would otherwise match the @study:UUID inside the
    # link form. The covered-spans pass must skip those.
    sid = uuid.uuid4()
    body = f"[Caso A](@study:{sid})"
    out = parse_mentions(body)
    # Exactly one mention, with the title from the link form.
    assert len(out) == 1
    assert out[0].title == "Caso A"


def test_parse_mentions_does_not_match_markdown_heading() -> None:
    # The old ``#tag`` syntax collided with markdown H1 / H2 / H3.
    # The new ``@tag:`` form must not trigger on a heading line.
    out = parse_mentions("# Heading 1\n## Heading 2\n### Heading 3\nbody text without tags")
    assert out == []


def test_iter_mention_kinds_dedupes() -> None:
    sid_a = uuid.uuid4()
    sid_b = uuid.uuid4()
    fid = uuid.uuid4()
    body = f"@study:{sid_a} and @study:{sid_b} and @folder:{fid} and @tag:x"
    kinds = iter_mention_kinds(parse_mentions(body))
    assert kinds == {"study", "folder", "tag"}


def test_mention_dataclass_is_frozen() -> None:
    m = Mention(kind="study", raw="@study:x", target_id=uuid.uuid4())
    try:
        m.kind = "folder"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Mention should be frozen")


def test_parse_mentions_normalises_plural_kinds() -> None:
    # Users naturally type ``@documents:UUID`` when mentioning several
    # papers in one pill (link form ``[Title](@documents:UUID)``).
    # The DSL is officially singular; the parser must accept the
    # plural alias and normalise to the canonical kind so downstream
    # cross-patient validation + URL resolution keep working.
    did = uuid.uuid4()
    sid = uuid.uuid4()
    fid = uuid.uuid4()
    body = f"Linkati [Referti vari](@documents:{did}), @studies:{sid} e [Imaging](@folders:{fid})."
    out = parse_mentions(body)
    by_kind = {m.kind: m for m in out}
    assert by_kind["document"].target_id == did
    assert by_kind["document"].title == "Referti vari"
    assert by_kind["study"].target_id == sid
    assert by_kind["folder"].target_id == fid
    assert by_kind["folder"].title == "Imaging"
    # The raw span is preserved (still says ``@documents:`` plural) so
    # the editor can still highlight the original text on validation
    # failure; only the parsed ``kind`` is normalised.
    assert "@documents:" in by_kind["document"].raw
