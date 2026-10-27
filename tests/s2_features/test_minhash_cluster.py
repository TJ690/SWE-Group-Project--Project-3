"""MinHash/LSH near-duplicate clusters."""

from __future__ import annotations

import dataclasses

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skillsig.cli import resolve_runner
from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s0_intake.devsample import output_path as devsample_path
from skillsig.s2_features.minhash_cluster import cluster_shard, normalise, run, signature

_FRONT = (
    "---\nname: demo\ndescription: "
    + " ".join(f"template{i:04d}" for i in range(800))
    + "\n---\n"
)
_BODY_A = " ".join(f"alpha{i}" for i in range(80))
_BODY_B = " ".join(f"bravo{i}" for i in range(80))


def _long_text(changed: str | None = None) -> str:
    # Unique words, so one edit moves only a handful of 5-word shingles.
    words = [f"w{i:04d}" for i in range(700)]
    if changed is not None:
        words[10] = changed
    return " ".join(words)


def _stack(cfg: Config, texts: list[str]) -> np.ndarray:
    return np.stack([signature(text, cfg) for text in texts])


def test_near_identical_texts_cluster_at_0_8(cfg: Config) -> None:
    texts = [_long_text(), _long_text("changed")]
    sigs = _stack(cfg, texts)
    assert sigs.dtype == np.uint32
    assert sigs.shape == (2, cfg.minhash.num_perm)
    assert float(np.mean(sigs[0] == sigs[1])) >= 0.8
    clusters = dict(cluster_shard(["a", "b"], sigs, 0.8))
    assert clusters["a"] == clusters["b"]


def test_unrelated_texts_do_not_cluster(cfg: Config) -> None:
    texts = [
        " ".join(f"north{i}" for i in range(200)),
        " ".join(f"south{i}" for i in range(200)),
    ]
    clusters = dict(cluster_shard(["a", "b"], _stack(cfg, texts), 0.8))
    assert clusters["a"] != clusters["b"]


def test_identical_front_matter_different_bodies_do_not_cluster(cfg: Config) -> None:
    assert normalise("  Hello\n\nWorld  ", 100) == "hello world"
    assert normalise("abcdefghij", 4) == "abcd"
    assert normalise(_FRONT + _BODY_A, cfg.minhash.body_max_chars) == normalise(
        _BODY_A, cfg.minhash.body_max_chars
    )
    raw = [_FRONT + _BODY_A, _FRONT + _BODY_B]
    clusters = dict(cluster_shard(["a", "b"], _stack(cfg, raw), 0.8))
    assert clusters["a"] != clusters["b"]


def test_cluster_ids_are_deterministic_for_the_same_input_order(cfg: Config) -> None:
    ids = ["a", "b", "c"]
    texts = [_long_text(), _long_text("changed"), " ".join(f"other{i}" for i in range(200))]
    sigs = _stack(cfg, texts)
    first = cluster_shard(ids, sigs, 0.8)
    assert first == cluster_shard(ids, sigs, 0.8)
    labels = dict(first)
    assert labels["a"] == labels["b"] == 0
    assert labels["c"] == 2


def test_run_writes_both_thresholds_shards_and_skips(tmp_cfg: Config) -> None:
    _write_artifacts(
        tmp_cfg,
        [
            _row("bbb", _long_text(), 250, 1),
            _row("aaa", _long_text("changed"), 250, 1),
            _row("ccc", " ".join(f"other{i}" for i in range(200)), 250, 1),
            _row("sss", "skills/SKILL.md", 250, 1),
            _row("rrr", "Redirecting to https://example.com", 250, 1),
            _row("ppp", _long_text(), 250, 0),
            _row("lll", _long_text(), 10, 1),
        ],
    )
    _write_devsample(tmp_cfg, ["aaa", "bbb", "ccc", "sss", "rrr", "ppp", "lll"])

    output = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_clusters.parquet"
    first = run(tmp_cfg, dev=True)
    assert first.skipped is False
    assert first.rows == {"s2_clusters": 6}
    assert first.outputs == [output]

    table = pq.read_table(output)
    assert table.column_names == [
        "file_sha",
        "cluster_id",
        "cluster_size",
        "threshold",
        "nn_sim_est",
    ]
    got = table.to_pylist()
    assert [row["file_sha"] for row in got] == ["aaa", "bbb", "ccc", "aaa", "bbb", "ccc"]
    assert [row["threshold"] for row in got] == [0.6, 0.6, 0.6, 0.8, 0.8, 0.8]
    for threshold in (0.6, 0.8):
        block = [row for row in got if row["threshold"] == threshold]
        by_sha = {row["file_sha"]: row for row in block}
        assert by_sha["aaa"]["cluster_id"] == by_sha["bbb"]["cluster_id"]
        assert by_sha["aaa"]["cluster_size"] == 2
        assert by_sha["ccc"]["cluster_id"] != by_sha["aaa"]["cluster_id"]
        assert by_sha["ccc"]["cluster_size"] == 1
        assert by_sha["aaa"]["nn_sim_est"] >= 0.8
        assert by_sha["ccc"]["nn_sim_est"] is None

    shard = resolve(tmp_cfg, tmp_cfg.paths.derived) / "s2_minhash_shards" / "shard_00000.npy"
    stored = np.load(shard)
    assert stored.dtype == np.uint32
    assert stored.shape == (3, tmp_cfg.minhash.num_perm)
    assert np.array_equal(stored[0], signature(_long_text("changed"), tmp_cfg))

    second = run(tmp_cfg, dev=True)
    assert second.skipped is True
    assert second.rows == {"s2_clusters": 6}
    assert resolve_runner("dedup", None) is run


def test_limit_sample_fraction_and_shard_resume(tmp_cfg: Config) -> None:
    rows = [
        _row("a1", _long_text(), 250, 1, "skills"),
        _row("a2", _long_text("changed"), 250, 1, "skills"),
        _row("a3", " ".join(f"other{i}" for i in range(80)), 250, 1, "skills"),
        _row("a4", " ".join(f"extra{i}" for i in range(80)), 250, 1, "skills"),
        _row("b1", " ".join(f"bravo{i}" for i in range(80)), 250, 1, "other"),
        _row("b2", " ".join(f"charlie{i}" for i in range(80)), 250, 1, "other"),
    ]
    _write_artifacts(tmp_cfg, rows)
    _write_devsample(tmp_cfg, [str(row["file_sha"]) for row in rows])
    cfg = dataclasses.replace(tmp_cfg, chunk_rows=1)

    limited = run(cfg, dev=True, limit=1)
    assert limited.rows == {"s2_clusters": 2}
    limited_rows = pq.read_table(limited.outputs[0]).to_pylist()
    assert {row["file_sha"] for row in limited_rows} == {"a1"}

    written = run(cfg, dev=True, force=True, limit=2)
    output = written.outputs[0]
    shard = resolve(cfg, cfg.paths.derived) / "s2_minhash_shards" / "shard_00000.npy"
    stamped = shard.stat().st_mtime_ns
    output.unlink()
    output.with_name(output.name + ".manifest.json").unlink()
    resumed = run(cfg, dev=True, limit=2)
    assert resumed.skipped is False
    assert shard.stat().st_mtime_ns == stamped
    assert {row["file_sha"] for row in pq.read_table(output).to_pylist()} == {"a1", "a2"}

    sampled = run(tmp_cfg, dev=True, force=True, sample_fraction=0.5)
    sampled_ids = {row["file_sha"] for row in pq.read_table(sampled.outputs[0]).to_pylist()}
    assert len(sampled_ids) == 3
    assert len([sha for sha in sampled_ids if str(sha).startswith("a")]) == 2
    assert len([sha for sha in sampled_ids if str(sha).startswith("b")]) == 1
    again = run(tmp_cfg, dev=True, sample_fraction=0.5)
    assert again.skipped is True


def test_missing_artifacts_names_download_command(tmp_cfg: Config) -> None:
    with pytest.raises(FileNotFoundError, match=r"download_data.py"):
        run(tmp_cfg, dev=True)


def test_missing_dev_sample_names_command(tmp_cfg: Config) -> None:
    _write_artifacts(tmp_cfg, [_row("aaa", "hello\n", 250, 1)])
    with pytest.raises(FileNotFoundError, match=r"data --dev-sample"):
        run(tmp_cfg, dev=True)


def _row(
    file_sha: str,
    content: str,
    body_chars: int,
    dedup_primary: int,
    location_class: str = "skills",
) -> dict[str, object]:
    return {
        "file_sha": file_sha,
        "content": content,
        "body_chars": body_chars,
        "dedup_primary": dedup_primary,
        "location_class": location_class,
    }


def _write_artifacts(cfg: Config, rows: list[dict[str, object]]) -> None:
    path = resolve(cfg, cfg.paths.raw) / "artifacts" / "part-00000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "file_sha": pa.array([row["file_sha"] for row in rows], type=pa.string()),
            "content": pa.array([row["content"] for row in rows], type=pa.string()),
            "body_chars": pa.array([row["body_chars"] for row in rows], type=pa.int64()),
            "dedup_primary": pa.array([row["dedup_primary"] for row in rows], type=pa.int64()),
            "location_class": pa.array([row["location_class"] for row in rows], type=pa.string()),
        }
    )
    pq.write_table(table, path, compression="zstd")


def _write_devsample(cfg: Config, file_shas: list[str]) -> None:
    path = devsample_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "file_sha": pa.array(file_shas, type=pa.string()),
            "stratum": pa.array(["x"] * len(file_shas), type=pa.string()),
        }
    )
    pq.write_table(table, path, compression="zstd")
