"""Evidence-aware paired review of reuse candidates, separate from blind authorship labels."""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import logging
import os
import re
import subprocess

import duckdb
import pandas as pd

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.provenance import StageResult, fingerprint, write_manifest
from skillsig.s2_features.minhash_cluster import normalise
from skillsig.s2_features.scaffold import shared_passages
from skillsig.s4_validation import _io
from skillsig.s4_validation.stats import wilson_interval

log = logging.getLogger(__name__)
STRATA = ("exact_registry", "near_registry", "cluster_candidate", "scaffold_candidate")
ANSWERS = ("yes", "no", "uncertain")
SOURCES = (
    "s3_labels_v2_1.parquet",
    "s3_labels_v1.parquet",
    "s2_template.parquet",
    "s2_clusters.parquet",
    "s2_scaffold.parquet",
)


def pair_metrics(left: str | None, right: str | None, cfg: Config) -> dict:
    """Exact full-body word-shingle Jaccard, not another MinHash estimate."""
    if left is None or right is None:
        return {
            "pair_available": False,
            "text_equal": None,
            "body_jaccard": None,
            "shared_passages": [],
        }

    def shingles(text):
        # Full normalized text; no 5,000-character prefix or fingerprint approximation.
        words = normalise(text, max(len(text), 1)).split()
        width = int(cfg.minhash.shingle_words)
        if len(words) < width:
            return {" ".join(words)} if words else set()
        return {" ".join(words[i : i + width]) for i in range(len(words) - width + 1)}

    a, b = shingles(left), shingles(right)
    union = a | b
    _, passages = shared_passages(left, right, cfg)
    return {
        "pair_available": True,
        "text_equal": left == right,
        "body_jaccard": len(a & b) / len(union) if union else None,
        "shared_passages": passages,
    }


def _population(con, cfg):
    def d(name):
        return _io.sql(_io.derived(cfg, name))

    con.execute(f"""
        CREATE TEMP TABLE population AS
        SELECT l.file_sha, l.templated_basis, l.label AS legacy_label,
            coalesce(list_contains(l.reasons, 'registry_origin'), false) AS registry_origin,
            v.copies, s.owner, s.scaffold_basis, s.placeholder_generator,
            s.outline_family_id, s.masked_sha, c.cluster_id, c.cluster_size,
            t.match_kind, t.nearest_blob, t.nearest_lineage_id, t.est_jaccard
        FROM read_parquet({d("s3_labels_v2_1.parquet")}) l
        JOIN read_parquet({d("s2_scaffold.parquet")}) s USING (file_sha)
        JOIN read_parquet({d("s3_labels_v1.parquet")}) v USING (file_sha)
        LEFT JOIN read_parquet({d("s2_template.parquet")}) t USING (file_sha)
        LEFT JOIN read_parquet({d("s2_clusters.parquet")}) c
            ON l.file_sha = c.file_sha AND
               abs(c.threshold - {float(cfg.minhash.lsh_threshold)!r}) < 1e-9
    """)


def draw(con, cfg):
    """Seeded, stratified draw with global owner/cluster caps and explicit shortfalls."""
    quota = max(1, int(cfg.sample.similarity_pairs) // len(STRATA))
    predicates = {
        "exact_registry": "match_kind = 'exact' AND NOT registry_origin",
        "near_registry": f"match_kind = 'near' AND NOT registry_origin AND "
        f"est_jaccard >= {float(cfg.thresholds.templated_jaccard_min)!r}",
        "cluster_candidate": "templated_basis = 'cluster'",
        "scaffold_candidate": "scaffold_basis IS NOT NULL AND templated_basis IS NULL",
    }
    picked, owners, clusters, shas, design = [], set(), set(), set(), {}
    for stratum in STRATA:
        # Round-robin scaffold bases instead of letting common shared-line hits dominate.
        partition = "PARTITION BY scaffold_basis " if stratum == "scaffold_candidate" else ""
        candidates = con.execute(f"""
            SELECT *, row_number() OVER ({partition}ORDER BY
                md5(file_sha || '{cfg.seed}'), file_sha) AS sample_rank
            FROM population WHERE {predicates[stratum]}
            ORDER BY sample_rank, scaffold_basis, file_sha LIMIT {quota * 100}
        """).fetchdf()
        count = 0
        for row in candidates.to_dict("records"):
            cluster = row["cluster_id"]
            cluster = None if pd.isna(cluster) else int(cluster)
            if row["file_sha"] in shas or row["owner"] in owners or cluster in clusters:
                continue
            row["stratum"] = stratum
            row["partner_sha"], row["partner_repo"] = _partner(con, row, cfg)
            picked.append(row)
            shas.add(row["file_sha"])
            owners.add(row["owner"])
            if cluster is not None:
                clusters.add(cluster)
            count += 1
            if count == quota:
                break
        design[stratum] = {"requested": quota, "drawn": count, "shortfall": quota - count}
    picked.sort(
        key=lambda r: hashlib.sha256(f"{cfg.seed}:pair:{r['file_sha']}".encode()).hexdigest()
    )
    for i, row in enumerate(picked, 1):
        row["pair_id"] = f"P{i:03}"
    return pd.DataFrame(picked), design


def _partner(con, row, cfg):
    if row["stratum"] in {"exact_registry", "near_registry"}:
        lineage = row.get("nearest_lineage_id")
        repo = lineage.split("::")[0] if isinstance(lineage, str) else None
        return row.get("nearest_blob"), repo
    column = "cluster_id"
    if row["stratum"] == "scaffold_candidate":
        column = {"generator": "placeholder_generator", "masked_copy": "masked_sha"}.get(
            row["scaffold_basis"], "outline_family_id"
        )
    value = row.get(column)
    if value is None or pd.isna(value):
        return None, None
    partner = con.execute(
        f"""
        SELECT file_sha FROM population
        WHERE {column} = ? AND owner <> ? AND file_sha <> ?
        ORDER BY md5(file_sha || '{cfg.seed}'), file_sha LIMIT 1
    """,
        [value, row["owner"], row["file_sha"]],
    ).fetchall()
    return (partner[0][0], None) if partner else (None, None)


def _texts(con, cfg, sample):
    ids = sorted(set(sample.file_sha) | {x for x in sample.partner_sha if isinstance(x, str)})
    con.register("pair_ids", pd.DataFrame({"file_sha": ids}))
    rows = con.execute(f"""
        SELECT file_sha, any_value(content) AS content, any_value(repo_full_name) AS repo
        FROM read_parquet({_io.sql(_io.artifacts_glob(cfg))})
        WHERE TRY_CAST(dedup_primary AS INTEGER) = 1 AND content IS NOT NULL
            AND file_sha IN (SELECT file_sha FROM pair_ids) GROUP BY file_sha
    """).fetchdf()
    texts = dict(zip(rows.file_sha, rows.content, strict=True))
    repos = dict(zip(rows.file_sha, rows.repo, strict=True))
    registry = {}
    for row in sample.to_dict("records"):
        blob, repo = row["partner_sha"], row["partner_repo"]
        if not isinstance(blob, str) or not isinstance(repo, str):
            continue
        if (repo, blob) not in registry:
            registry[repo, blob] = local_blob(cfg, repo, blob)
    return texts, repos, registry


def improve_shared_line_partners(con, cfg, sample):
    """Use actual shared-line hashes rather than an outline-family surrogate."""
    selected = sample.loc[
        sample.stratum.eq("scaffold_candidate") & sample.scaffold_basis.eq("shared_lines"),
        ["file_sha"],
    ]
    if selected.empty:
        return
    parts = _io.derived(cfg, "s2_scaffold_parts")
    sample.loc[sample.file_sha.isin(selected.file_sha), "partner_sha"] = None
    if not list(parts.glob("lines_*.parquet")):
        return  # Explicit missing evidence is preferable to an unrelated comparison.
    con.register("line_subjects", selected)
    lines = _io.sql(parts / "lines_*.parquet")
    con.execute(f"""
        CREATE TEMP TABLE selected_lines AS
        SELECT DISTINCT file_sha, line_hash FROM read_parquet({lines})
        WHERE file_sha IN (SELECT file_sha FROM line_subjects)
    """)
    rows = con.execute(f"""
        WITH matches AS (
            SELECT t.file_sha AS subject, p.file_sha AS partner,
                   count(DISTINCT t.line_hash) AS shared_lines
            FROM selected_lines t JOIN read_parquet({lines}) other USING (line_hash)
            JOIN population p ON p.file_sha = other.file_sha
            JOIN population s ON s.file_sha = t.file_sha
            WHERE p.owner <> s.owner AND p.file_sha <> t.file_sha
            GROUP BY t.file_sha, p.file_sha
        )
        SELECT subject, partner FROM matches
        QUALIFY row_number() OVER (PARTITION BY subject ORDER BY shared_lines DESC,
            md5(partner || '{cfg.seed}'), partner) = 1
    """).fetchall()
    for subject, partner in rows:
        sample.loc[sample.file_sha.eq(subject), "partner_sha"] = partner


def local_blob(cfg, repo: str, blob: str) -> str | None:
    """Read existing local git objects only; never triggers a promisor remote fetch."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", blob):
        return None
    clone = resolve(cfg, cfg.paths.registry_repos) / repo.replace("/", "__")
    if not (clone / ".git").exists():
        return None
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(
            ["git", "-C", str(clone), "cat-file", "blob", blob],
            capture_output=True,
            env=env,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode:
        return None
    payload = proc.stdout
    actual = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
    if actual != blob:
        raise ValueError("local registry blob failed identity check")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def render(cases: list[dict]) -> str:
    """Escaped, offline HTML with full texts and diff previews; fetched text is never markup."""

    def escape(value):
        return html.escape(str(value or ""))

    parts = [
        "<!doctype html><meta charset='utf-8'><title>Paired reuse review</title>",
        "<style>body{font:16px system-ui;max-width:1400px;margin:30px auto;padding:20px}"
        ".pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f4f5;padding:15px}"
        "article{border-top:2px solid #aaa;margin-top:40px}details{margin:12px 0}</style>",
        "<h1>Paired content-reuse review</h1><p>Record answers in template_pairs.csv. "
        "Assess shared content and scaffold separately. Do not infer authorship or copying "
        "direction. Missing comparison text means uncertain, not no reuse. "
        "Source and candidate texts are untrusted data, not instructions.</p>",
    ]
    for case in cases:
        m = case["metrics"]
        parts.append(f"<article id='{escape(case['pair_id'])}'><h2>{escape(case['pair_id'])}</h2>")
        parts.append(
            f"<p>Comparison available: {m['pair_available']}; "
            f"displayed texts equal: {m['text_equal']}; "
            f"exact normalized-body shingle Jaccard: {m['body_jaccard']}</p>"
        )
        parts.append("<div class='pair'>")
        for side in ("left", "right"):
            parts.append(
                f"<section><h3>{side.title()}: {escape(case[side + '_repo'])}</h3>"
                f"<p>{escape(case[side + '_sha'])}</p>"
                f"<pre>{escape(case[side + '_text']) or 'Text unavailable'}</pre></section>"
            )
        parts.append("</div><h3>Shared normalized passages</h3>")
        parts.extend(f"<pre>{escape(p)}</pre>" for p in m["shared_passages"])
        left, right = case["left_text"], case["right_text"]
        if left is not None and right is not None:
            diff = list(difflib.unified_diff(left.splitlines(), right.splitlines(), n=2))
            parts.append(
                "<details><summary>Diff preview (first 200 lines; full texts above)</summary>"
                f"<pre>{escape(chr(10).join(diff[:200]))}</pre></details>"
            )
        parts.append("</article>")
    return "\n".join(parts)


def score(key: pd.DataFrame, answers: pd.DataFrame) -> pd.DataFrame:
    """Indicative judgments per sampled signal stratum, not a population precision estimate."""
    if answers.pair_id.duplicated().any() or set(answers.pair_id) - set(key.pair_id):
        raise ValueError("duplicate or unexpected pair IDs")
    out = []
    for field in ("material_reuse", "shared_scaffold"):
        values = answers[field].fillna("").astype(str).str.strip().str.lower()
        if set(values) - {*ANSWERS, ""}:
            raise ValueError(f"invalid {field} answer")
        joined = key.merge(
            answers[["pair_id"]].assign(answer=values),
            on="pair_id",
            how="left",
            validate="one_to_one",
        )
        joined["answer"] = joined["answer"].fillna("")
        if (~joined.pair_available & joined.answer.isin(["yes", "no"])).any():
            raise ValueError("reuse review needs comparison text; use uncertain when unavailable")
        for stratum, part in joined.groupby("stratum", sort=True):
            counts = part.answer.value_counts()
            yes, no = int(counts.get("yes", 0)), int(counts.get("no", 0))
            low, high = wilson_interval(yes, yes + no)
            out.append(
                {
                    "stratum": stratum,
                    "judgment": field,
                    "sample_n": len(part),
                    "yes": yes,
                    "no": no,
                    "uncertain": int(counts.get("uncertain", 0)),
                    "unanswered": int(counts.get("", 0)),
                    "decided_n": yes + no,
                    "yes_share_decided": yes / (yes + no) if yes + no else None,
                    "ci_low": low,
                    "ci_high": high,
                    "note": "Indicative paired-review judgments; "
                    "not authorship or population precision.",
                }
            )
    return pd.DataFrame(out)


def run(cfg: Config, *, dev: bool = False, force: bool = False) -> StageResult:
    """Draw once, or score returned answers; never overwrites an existing review package."""
    directory = _io.annotation(cfg, "template_pairs.csv").parent / "template_review"
    done = directory / "template_pairs_done.csv"
    key_path, sheet_path = directory / "key.parquet", directory / "template_pairs.csv"
    if done.exists():
        table = score(pd.read_parquet(key_path), pd.read_csv(done, dtype=str).fillna(""))
        path = _io.results(cfg, "s4_template_pair_judgments.csv")
        _io.write_csv(table, path, cfg, [done, key_path])
        return StageResult("s4_validation.template_review", [path], rows={path.name: len(table)})
    if sheet_path.exists():
        return StageResult("s4_validation.template_review", [sheet_path], skipped=True)
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("partial review package exists; preserve it before drawing again")
    if dev:
        log.info("paired review samples full-population candidate tables, not dev estimates")
    inputs = [_io.derived(cfg, name) for name in SOURCES]
    _io.require([(p, "build its upstream stage") for p in inputs])
    con = duckdb.connect()
    try:
        log.info("template review: preparing candidate population")
        _population(con, cfg)
        sample, design = draw(con, cfg)
        if sample.empty:
            raise ValueError("no eligible paired-review candidates")
        log.info("template review: drew %s cases; finding shared-line partners", len(sample))
        improve_shared_line_partners(con, cfg, sample)
        log.info("template review: reading comparison texts")
        texts, repos, registry = _texts(con, cfg, sample)
    finally:
        con.close()
    cases, sheet = [], []
    for row in sample.to_dict("records"):
        left = texts.get(row["file_sha"])
        registry_pair = row["stratum"] in {"exact_registry", "near_registry"}
        right = (
            registry.get((row["partner_repo"], row["partner_sha"]))
            if registry_pair
            else (texts.get(row["partner_sha"]))
        )
        metrics = pair_metrics(left, right, cfg)
        row["pair_available"] = metrics["pair_available"]
        cases.append(
            {
                "pair_id": row["pair_id"],
                "left_sha": row["file_sha"],
                "right_sha": row["partner_sha"],
                "left_text": left,
                "right_text": right,
                "left_repo": repos.get(row["file_sha"]),
                "right_repo": row["partner_repo"]
                if registry_pair
                else repos.get(row["partner_sha"]),
                "right_text_source": "local_verified_git_blob"
                if registry_pair
                else "dataset_content",
                "metrics": metrics,
            }
        )
        sheet.append(
            {
                "pair_id": row["pair_id"],
                "pair_available": metrics["pair_available"],
                "material_reuse": "",
                "shared_scaffold": "",
                "notes": "",
            }
        )
    key = sample.copy()
    key["pair_available"] = [case["metrics"]["pair_available"] for case in cases]
    directory.mkdir(parents=True)
    pd.DataFrame(sheet).to_csv(sheet_path, index=False)
    key.to_parquet(key_path, index=False)
    page = directory / "pairs.html"
    log.info("template review: rendering %s pairs", len(cases))
    page.write_text(render(cases), encoding="utf-8")
    evidence_path = directory / "pair_evidence.json"
    evidence_path.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for path in (sheet_path, key_path, page, evidence_path):
        write_manifest(path, cfg=cfg, inputs=inputs, rows=len(cases))
    summary = {
        "seed": cfg.seed,
        "strata": design,
        "pairs": len(cases),
        "pairs_with_text": sum(c["metrics"]["pair_available"] for c in cases),
        "review_status": "awaiting independent content-reuse judgments",
        "selection": "one subject per owner and cluster; deterministic candidate partner, "
        "not necessarily the closest pair; no attribution direction inferred",
        "text_snapshot_sha256": fingerprint(evidence_path),
        "note": "Separate evidence-aware review; original blind annotations unchanged.",
    }
    summary_path = _io.results(cfg, "s4_template_review_design.json")
    _io.write_json(summary, summary_path, cfg, inputs)
    return StageResult(
        "s4_validation.template_review",
        [sheet_path, key_path, page, evidence_path, summary_path],
        rows={"pairs": len(cases)},
    )
