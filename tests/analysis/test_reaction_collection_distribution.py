from pathlib import Path

import pytest

from analysis.sampling.reaction_collection_distribution import analyze


def test_report_uses_explicit_small_collection_and_preserves_existing_output(tmp_path: Path) -> None:
    ground_truth = tmp_path / "ground-truth"
    project = tmp_path / "project"
    ground_truth.mkdir()
    project.mkdir()
    header = "ene_id,diene_id,mapped_reaction_smiles,activation_free_energy_kcal_mol\n"
    ground_truth.joinpath("reaction-collection.csv").write_text(
        header + "e1,d1,C>>C,10\ne1,d1,C>>C,12\ne2,d1,CC>>CC,20\n"
    )
    project.joinpath("reaction-collection.csv").write_text(header + "e1,d1,C>>C,13\n")
    resources = "ene_id,diene_id,ts_file_count,ts_compute_seconds_sum\n"
    ground_truth.joinpath("ts-time-aggregation.csv").write_text(
        resources + "e1,d1,1,3600\ne2,d1,1,7200\n"
    )
    project.joinpath("ts-time-aggregation.csv").write_text(resources + "e1,d1,1,1800\n")
    output = tmp_path / "report"
    audit = analyze(ground_truth_dir=ground_truth, projects={"selected": project},
                    output_dir=output, expected_pair_count=2)
    result = audit["projects"]["selected"]
    assert result["successful_label_substrate_pair_coverage"] == 0.5
    assert result["matched_ground_truth_reaction_collection_row_coverage"] == 2 / 3
    assert result["baseline_mae_kcal_mol"] == 2.0
    report = (output / "README.md").read_text()
    assert "requested 2-pair space" in report
    assert "denominator (3 rows)" in report
    with pytest.raises(FileExistsError):
        analyze(ground_truth_dir=ground_truth, projects={"selected": project},
                output_dir=output, expected_pair_count=2)
    assert (output / "README.md").read_text() == report
    with pytest.raises(ValueError, match="expected 3"):
        analyze(ground_truth_dir=ground_truth, projects={"selected": project},
                output_dir=tmp_path / "invalid", expected_pair_count=3)
    assert not (tmp_path / "invalid").exists()
