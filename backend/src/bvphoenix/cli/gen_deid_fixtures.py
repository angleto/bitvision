"""CLI: generate a synthetic burned-in-PHI corpus (DICOM + answer key) on disk.

Seeds the marker-gated dataset dir consumed by the redaction recall gate, or a
folder for manual inspection. All logic lives in
``services.pixel_deid_eval`` — this is a thin, deterministic wrapper.

    bvphoenix-gen-deid-fixtures --out /tmp/deid-corpus --count 50
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bvphoenix.services.pixel_deid_eval import synthesize_case


@click.command()
@click.option("--out", "out_dir", required=True, type=click.Path(), help="Output directory.")
@click.option("--count", default=20, show_default=True, help="Number of synthetic cases.")
@click.option("--seed", default=0, show_default=True, help="Base seed (deterministic).")
@click.option(
    "--modality",
    default="US",
    show_default=True,
    help="Modality for the synthetic frames (US/SC/OT...).",
)
def main(out_dir: str, count: int, seed: int, modality: str) -> None:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    answer_key: dict[str, list[dict]] = {}
    for i in range(count):
        case = synthesize_case(seed=seed + i, modality=modality)
        name = f"synthetic_{i:04d}.dcm"
        (root / name).write_bytes(case.dicom)
        answer_key[name] = [
            {"x": g.x, "y": g.y, "w": g.w, "h": g.h, "text": g.text, "category": g.category}
            for g in case.gt
        ]
    (root / "answer_key.json").write_text(json.dumps(answer_key, indent=2))
    click.echo(f"wrote {count} synthetic cases + answer_key.json to {root}")


if __name__ == "__main__":
    main()
