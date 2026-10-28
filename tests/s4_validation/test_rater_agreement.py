"""Independent annotations must be paired by ID and never silently discarded."""

from __future__ import annotations

import pandas as pd
import pytest
from sklearn.metrics import cohen_kappa_score

from skillsig.s4_validation import rater_agreement as ra


def test_pairs_use_ids_with_shuffled_rows_and_partial_answers() -> None:
    a = pd.DataFrame(
        {
            "sample_id": ["1", "2", "3", "4"],
            "your_label": [
                "agent_assisted",
                "agent_assisted",
                "likely_human",
                "insufficient_evidence",
            ],
        }
    )
    b = pd.DataFrame(
        {
            "sample_id": ["3", "2", "1", "4"],
            "your_label": [
                "likely_human",
                "likely_human",
                "agent_assisted",
                "",
            ],
        }
    )
    ids = set(a["sample_id"])
    summary, confusion, pairs = ra.compare(ra.validate(a, ids, "A"), ra.validate(b, ids, "B"))
    assert summary["n_overlap"] == 3
    assert summary["n_agree"] == 2
    assert summary["n_disagree"] == 1
    assert summary["raw_agreement"] == pytest.approx(2 / 3)
    assert summary["kappa"] == pytest.approx(
        cohen_kappa_score(
            ["agent_assisted", "agent_assisted", "likely_human"],
            ["agent_assisted", "likely_human", "likely_human"],
        )
    )
    assert len(confusion) == 16 and confusion["n"].sum() == 3
    assert pairs.loc[~pairs["agree"], "sample_id"].tolist() == ["2"]
    assert a["your_label"].iloc[1] == "agent_assisted"


@pytest.mark.parametrize(
    "ids,labels,allowed,error",
    [
        (["1", "1"], ["likely_human"] * 2, {"1"}, "duplicate"),
        (["2"], ["likely_human"], {"1"}, "unexpected"),
        (["1"], ["human"], {"1"}, "invalid labels"),
        ([""], ["likely_human"], {"1"}, "blank"),
    ],
)
def test_validation_rejects_ambiguous_or_invalid_answers(ids, labels, allowed, error) -> None:
    sheet = pd.DataFrame({"sample_id": ids, "your_label": labels})
    with pytest.raises(ValueError, match=error):
        ra.validate(sheet, allowed, "B")


def test_run_score_writes_overlap_and_private_review(tmp_cfg) -> None:
    from skillsig.s4_validation import spot_check as sc

    directory = sc._io.annotation(tmp_cfg, "key.parquet").parent
    directory.mkdir(parents=True)
    key = pd.DataFrame(
        {
            "sample_id": ["1", "2", "3"],
            "file_sha": ["x", "y", "z"],
            "label_v21": ["agent_assisted", "likely_human", "likely_human"],
            "label_v2": ["agent_assisted", "likely_human", "likely_human"],
            "reasons_v21": [["trailer"], ["user"], ["user"]],
            "reasons_v2": [["trailer"], ["user"], ["user"]],
            "in_b": [True, True, False],
        }
    )
    key.to_parquet(directory / "key.parquet", index=False)
    a = pd.DataFrame(
        {
            "sample_id": ["1", "2", "3"],
            "your_label": [
                "agent_assisted",
                "likely_human",
                "likely_human",
            ],
            "notes": ["A1", "A2", "A3"],
        }
    )
    b = pd.DataFrame(
        {
            "sample_id": ["2", "1"],
            "your_label": [
                "insufficient_evidence",
                "agent_assisted",
            ],
            "notes": ["B2", "B1"],
        }
    )
    a.to_csv(directory / "spot_A_done.csv", index=False)
    b.to_csv(directory / "spot_B_done.csv", index=False)
    before = (directory / "spot_A_done.csv").read_bytes()
    result = sc.run_score(tmp_cfg)
    assert len(result.outputs) == 7
    confusion = pd.read_csv(sc._io.results(tmp_cfg, "s4_spot_rater_confusion.csv"))
    assert confusion["n"].sum() == 2
    review = pd.read_csv(directory / "rater_disagreements.csv")
    assert review["sample_id"].astype(str).tolist() == ["2"]
    assert review["notes_a"].tolist() == ["A2"]
    assert review["notes_b"].tolist() == ["B2"]
    precision = pd.read_csv(sc._io.results(tmp_cfg, "s4_spot_overlap_precision.csv"))
    rows = precision[(precision.version == "label_v21") & (precision.label == "likely_human")]
    assert dict(zip(rows.rater, rows.precision, strict=True)) == {"A": 1.0, "B": 0.0}
    assert (directory / "spot_A_done.csv").read_bytes() == before


def test_pairwise_covers_every_pair_and_keeps_answers() -> None:
    labels = {
        "A": ["agent_assisted", "likely_human", "likely_human"],
        "B": ["agent_assisted", "insufficient_evidence", ""],
        "C": ["agent_assisted", "likely_human", "insufficient_evidence"],
    }
    ids = {"1", "2", "3"}
    raters = {
        name: ra.validate(
            pd.DataFrame({"sample_id": ["1", "2", "3"], "your_label": answers}), ids, name
        )
        for name, answers in labels.items()
    }
    summaries, confusion, rows = ra.pairwise(raters)
    pairs = list(zip(summaries.rater_x, summaries.rater_y, strict=True))
    assert pairs == [("A", "B"), ("A", "C"), ("B", "C")]
    assert summaries.n_overlap.tolist() == [2, 3, 2]
    assert summaries.n_agree.tolist() == [1, 2, 1]
    assert confusion.groupby(["rater_x", "rater_y"]).n.sum().tolist() == [2, 3, 2]
    ac = rows[(rows.rater_x == "A") & (rows.rater_y == "C")]
    assert ac[["sample_id", "your_label_x", "your_label_y"]].values.tolist() == [
        ["3", "likely_human", "insufficient_evidence"]
    ]
    assert raters["A"]["your_label"].tolist() == labels["A"]


def test_run_score_adds_rater_c_without_changing_a_or_b(tmp_cfg) -> None:
    from skillsig.s4_validation import spot_check as sc

    directory = sc._io.annotation(tmp_cfg, "key.parquet").parent
    directory.mkdir(parents=True)
    pd.DataFrame(
        {
            "sample_id": ["1", "2", "3"],
            "file_sha": ["x", "y", "z"],
            "label_v21": ["agent_assisted", "likely_human", "likely_human"],
            "label_v2": ["agent_assisted", "likely_human", "likely_human"],
            "reasons_v21": [["trailer"], ["user"], ["user"]],
            "reasons_v2": [["trailer"], ["user"], ["user"]],
            "in_b": [True, True, False],
        }
    ).to_parquet(directory / "key.parquet", index=False)
    pd.DataFrame(
        {
            "sample_id": ["1", "2", "3"],
            "your_label": ["agent_assisted", "likely_human", "likely_human"],
        }
    ).to_csv(directory / "spot_A_done.csv", index=False)
    pd.DataFrame(
        {"sample_id": ["1", "2"], "your_label": ["agent_assisted", "insufficient_evidence"]}
    ).to_csv(directory / "spot_B_done.csv", index=False)
    sc.run_score(tmp_cfg)
    before = {
        name: sc._io.results(tmp_cfg, name).read_bytes()
        for name in ("s4_spot_precision.csv", "s4_spot_overlap_precision.csv")
    }
    review_before = (directory / "rater_disagreements.csv").read_bytes()
    # C labels only two of the three rows; the blank row must not count.
    pd.DataFrame(
        {
            "sample_id": ["3", "2", "1"],
            "your_label": ["insufficient_evidence", "likely_human", ""],
            "notes": ["C3", "C2", ""],
        }
    ).to_csv(directory / "spot_C_done.csv", index=False)
    result = sc.run_score(tmp_cfg)
    assert len(result.outputs) == 11
    for name, data in before.items():
        assert sc._io.results(tmp_cfg, name).read_bytes() == data
    assert (directory / "rater_disagreements.csv").read_bytes() == review_before
    pairs = pd.read_csv(sc._io.results(tmp_cfg, "s4_spot_rater_pairs.csv"))
    assert pairs[["rater_x", "rater_y", "n_overlap", "n_agree"]].values.tolist() == [
        ["A", "B", 2, 1],
        ["A", "C", 2, 1],
        ["B", "C", 1, 0],
    ]
    precision = pd.read_csv(sc._io.results(tmp_cfg, "s4_spot_rater_c_precision.csv"))
    human = precision[(precision.version == "label_v21") & (precision.label == "likely_human")]
    assert human[["rater", "n_predicted", "agree"]].values.tolist() == [["C", 2, 1]]
    review = pd.read_csv(directory / "rater_c_disagreements.csv")
    assert set(zip(review.rater_x, review.rater_y, review.sample_id, strict=True)) == {
        ("A", "C", 3),
        ("B", "C", 2),
    }
    assert set(review.label_v21) == {"likely_human"}
