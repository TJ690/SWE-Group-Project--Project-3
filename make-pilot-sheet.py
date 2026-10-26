"""Draw a pilot sample and write BLIND labeling sheets.

Run from the repo root, with the venv active and `pip install -e .` done:
    python make_pilot_sheet.py --n 12

What it does
- Finds data via the team's own config (skillsig.config / skillsig.paths),
  so it follows cfg.paths.raw and cfg.paths.derived instead of hardcoded paths.
- If s3_labels_v2.parquet exists, draws an equal number of skills per
  heuristic label (so the pilot is not ~90% insufficient_evidence).
  Otherwise falls back to a plain random draw from the raw artifacts.
- Writes two separate folders:
    pilot/          SHARE THIS with labelers. Skill text only.
    pilot_private/  KEEP THIS. Holds the heuristic label, basis, confidence.
  Labelers must not see pilot_private/ until after they have labeled.

Labelers see ONLY: file_sha, name, description, skill text.

Written without access to the real data; untested. If something fails,
paste the full error.
"""
import argparse
import csv
import pathlib
import re

import duckdb

from skillsig.config import load_config
from skillsig.paths import resolve

LABELS = ("highly_templated", "agent_assisted", "likely_human", "insufficient_evidence")
SHA_OK = re.compile(r"^[0-9a-fA-F]+$")


def q(path) -> str:
    """Quote a path for DuckDB SQL."""
    return "'" + pathlib.Path(path).as_posix().replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12, help="total skills (split across labels)")
    ap.add_argument("--seed", type=int, default=None,
                    help="default: the project seed from the config")
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--out", default="pilot")
    ap.add_argument("--private-out", default="pilot_private")
    args = ap.parse_args()

    cfg = load_config()
    seed = cfg.seed if args.seed is None else args.seed
    artifacts = resolve(cfg, cfg.paths.raw) / "artifacts" / "*.parquet"
    labels_path = resolve(cfg, cfg.paths.derived) / "s3_labels_v2.parquet"

    con = duckdb.connect()
    key = {}  # file_sha -> (label, basis, confidence), kept private

    if labels_path.is_file():
        per = max(1, args.n // len(LABELS))
        rows = con.execute(f"""
            SELECT file_sha, label, templated_basis, confidence FROM (
                SELECT file_sha, label, templated_basis, confidence,
                       row_number() OVER (
                           PARTITION BY label
                           ORDER BY hash(file_sha || '{int(seed)}')
                       ) AS rn
                FROM read_parquet({q(labels_path)})
            ) WHERE rn <= {per}
        """).fetchall()
        for sha, label, basis, conf in rows:
            key[sha] = (label, basis or "", conf)
        print(f"Stratified draw: {per} per label from {labels_path}")
    else:
        print(f"{labels_path} not found. Falling back to a plain random draw.\n"
              "Heuristic labels will be blank in pilot_private/.")
        rows = con.execute(f"""
            SELECT file_sha FROM (
                SELECT file_sha,
                       row_number() OVER (ORDER BY hash(file_sha || '{int(seed)}')) AS rn
                FROM read_parquet({q(artifacts)})
                WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
                  AND coalesce(TRY_CAST(content_fetched AS INTEGER), 0) = 1
                  AND body_chars >= 200
            ) WHERE rn <= {int(args.n)}
        """).fetchall()
        for (sha,) in rows:
            key[sha] = ("", "", "")

    shas = [s for s in key if SHA_OK.match(s)]
    if not shas:
        raise SystemExit("No skills drawn. Check the data paths above.")
    listed = ", ".join("'" + s + "'" for s in shas)

    # Pull only the visible columns for the chosen skills.
    content_rows = con.execute(f"""
        SELECT file_sha, name, description, content
        FROM read_parquet({q(artifacts)})
        WHERE coalesce(TRY_CAST(dedup_primary AS INTEGER), 0) = 1
          AND file_sha IN ({listed})
    """).fetchall()

    out = pathlib.Path(args.out)
    priv = pathlib.Path(args.private_out)
    (out / "skills").mkdir(parents=True, exist_ok=True)
    priv.mkdir(parents=True, exist_ok=True)

    seen, sheet, private = set(), [], []
    for sha, name, desc, content in content_rows:
        if sha in seen:
            continue
        seen.add(sha)
        text = content or ""
        note = ""
        if len(text) > args.max_chars:
            text = text[: args.max_chars]
            note = f"\n\n[TRUNCATED at {args.max_chars} characters]"
        (out / "skills" / f"{sha[:10]}.md").write_text(
            f"file_sha: {sha}\nname: {name}\ndescription: {desc}\n"
            f"\n----- SKILL TEXT -----\n{text}{note}\n",
            encoding="utf-8",
        )
        sheet.append([sha, "", "", ""])
        private.append([sha, *key[sha]])

    with open(out / "pilot_sheet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # label: highly_templated | agent_assisted | likely_human | insufficient_evidence
        w.writerow(["file_sha", "label", "visible_features", "unsure"])
        w.writerows(sheet)

    with open(priv / "heuristic_key.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_sha", "v2_label", "templated_basis", "confidence"])
        w.writerows(private)

    print(f"Wrote {len(sheet)} skills to {out}/ (share this)")
    print(f"Wrote the heuristic key to {priv}/ (do NOT share until labeling is done)")


if __name__ == "__main__":
    main()
