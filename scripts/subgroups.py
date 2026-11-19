"""
Module: src/skillsig/s7_evaluate/subgroups.py
Description: Pipeline module for subgroup threshold sensitivity evaluations.
"""

from pathlib import Path
import json
import duckdb
import pandas as pd


def evaluate_subgroup_thresholds(
    data_dir: Path,
    results_dir: Path,
    thresholds: list[float] = None,
) -> pd.DataFrame:
    """Evaluates threshold stability across commit history and repo star subgroups."""
    if thresholds is None:
        thresholds = [0.60, 0.70, 0.80, 0.85, 0.90, 0.95]

    results_dir.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect()

    # Query features and repository profiles
    query = f"""
        SELECT 
            sf.skill_id,
            sf.has_commit_history,
            sf.cosine_sim_max,
            COALESCE(rp.stargazers_count, 0) AS stargazers_count
        FROM read_parquet('{data_dir / "skill_features.parquet"}') sf
        LEFT JOIN read_parquet('{data_dir / "repo_profile.parquet"}') rp
        ON sf.repo_id = rp.repo_id
    """
    skills_df = db.execute(query).df()

    # Bin stargazers into tiers
    skills_df["star_tier"] = pd.cut(
        skills_df["stargazers_count"],
        bins=[-1, 5, 100, float("inf")],
        labels=["low_stars_0_5", "mid_stars_6_100", "high_stars_100plus"],
    )

    records = []

    # 1. Sweep by Commit History Availability
    for has_hist in [True, False]:
        sub_df = skills_df[skills_df["has_commit_history"] == has_hist]
        total_sub = len(sub_df)
        if total_sub == 0:
            continue

        for t in thresholds:
            near_twins = (sub_df["cosine_sim_max"] >= t).sum()
            records.append({
                "subgroup_type": "history_availability",
                "subgroup_value": f"has_history_{has_hist}",
                "threshold": t,
                "total_skills": total_sub,
                "near_duplicate_count": int(near_twins),
                "near_duplicate_share": round(float(near_twins / total_sub), 4),
            })

    # 2. Sweep by Repository Star Tiers
    for tier in ["low_stars_0_5", "mid_stars_6_100", "high_stars_100plus"]:
        sub_df = skills_df[skills_df["star_tier"] == tier]
        total_sub = len(sub_df)
        if total_sub == 0:
            continue

        for t in thresholds:
            near_twins = (sub_df["cosine_sim_max"] >= t).sum()
            records.append({
                "subgroup_type": "repo_star_tier",
                "subgroup_value": tier,
                "threshold": t,
                "total_skills": total_sub,
                "near_duplicate_count": int(near_twins),
                "near_duplicate_share": round(float(near_twins / total_sub), 4),
            })

    # Save outputs to results/
    res_df = pd.DataFrame(records)
    res_df.to_csv(results_dir / "threshold_sensitivity_subgroups.csv", index=False)
    with open(results_dir / "threshold_sensitivity_subgroups.json", "w") as f:
        json.dump(records, f, indent=2)

    return res_df
