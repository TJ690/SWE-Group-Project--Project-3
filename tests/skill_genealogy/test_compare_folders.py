"""Tests for skill-folder diffs and script risk flags.

Hand-made manifests and script strings only. No dataset files.
"""

from __future__ import annotations

import pandas as pd

from skill_genealogy.compare_texts import (
    check_script_mentions,
    compare_folders,
    file_category,
    flag_script_risks,
    is_skipped_path,
    new_risk_flags,
)


def _file(path: str, sha: str, mode: str | None = "100644", size: int | None = 1) -> dict:
    """One manifest row: path, sha, mode and size."""
    return {"path": path, "sha": sha, "mode": mode, "size": size}


def test_file_category_uses_extension_then_mode() -> None:
    """Script extensions and mode 100755 are scripts. Docs, config and the rest follow."""
    assert file_category("scripts/run.sh", "100644") == "script"
    assert file_category("tool", "100755") == "script"
    assert file_category("references/api.md", "100644") == "reference"
    assert file_category("config.json", None) == "config"
    assert file_category("logo.png", "100644") == "asset"
    assert file_category("guide.rst", "100644") == "reference"
    assert file_category("settings.YML", None) == "config"
    assert file_category("notes.md", "100755") == "script"


def test_skipped_folder_and_root_skill_md_are_dropped() -> None:
    """node_modules is skipped. The root SKILL.md is not a folder file."""
    assert is_skipped_path("node_modules/x.js") is True
    assert is_skipped_path("scripts/run.sh") is False
    old = [
        _file("SKILL.md", "s"),
        _file("skill.md", "s2"),
        _file("templates/SKILL.md", "nested"),
        _file("node_modules/x.js", "v"),
        _file("scripts/run.sh", "a"),
    ]
    new = [_file("scripts/run.sh", "a"), _file("templates/SKILL.md", "nested")]
    rows = compare_folders(old, new)
    assert [row["path"] for row in rows] == ["scripts/run.sh", "templates/SKILL.md"]
    assert rows[0]["result"] == "same"


def test_compare_folders_covers_every_result() -> None:
    """Same, modified, added, removed, renamed, a mode-only change, and a skipped path."""
    old = [
        _file("keep.sh", "a", size=1),
        _file("edit.sh", "b", size=2),
        _file("gone.md", "c", size=3),
        _file("old.py", "d", size=4),
        _file("node_modules/x.js", "e", size=5),
    ]
    new = [
        _file("keep.sh", "a", mode="100755", size=1),
        _file("edit.sh", "B", size=9),
        _file("new.py", "d", size=4),
        _file("logo.png", "f", size=6),
        _file("node_modules/x.js", "e", size=5),
    ]
    rows = compare_folders(old, new)
    assert [(row["path"], row["result"]) for row in rows] == [
        ("edit.sh", "modified"),
        ("gone.md", "removed"),
        ("keep.sh", "same"),
        ("logo.png", "added"),
        ("new.py", "renamed"),
    ]
    renamed = next(row for row in rows if row["result"] == "renamed")
    assert renamed["old_path"] == "old.py"
    assert renamed["old_sha"] == "d"
    assert renamed["new_sha"] == "d"
    assert renamed["size_change"] == 0
    kept = next(row for row in rows if row["path"] == "keep.sh")
    assert kept["mode_changed"] is True
    assert kept["old_mode"] == "100644"
    assert kept["new_mode"] == "100755"
    assert kept["category"] == "script"
    edited = next(row for row in rows if row["path"] == "edit.sh")
    assert edited["size_change"] == 7
    assert edited["mode_changed"] is False
    assert next(row for row in rows if row["path"] == "gone.md")["size_change"] == -3
    added = next(row for row in rows if row["path"] == "logo.png")
    assert added["size_change"] == 6
    assert added["category"] == "asset"
    assert added["old_sha"] is None
    assert all(row["path"] != "node_modules/x.js" for row in rows)


def test_blank_sha_is_not_a_rename_and_one_sha_pairs_once() -> None:
    """An empty sha does not pair. A second copy of a sha stays added or removed."""
    old = [_file("a.py", ""), _file("b.py", "same"), _file("c.py", "same")]
    new = [_file("d.py", ""), _file("e.py", "same")]
    rows = {row["path"]: row for row in compare_folders(old, new)}
    assert rows["e.py"]["result"] == "renamed"
    assert rows["e.py"]["old_path"] == "b.py"
    assert rows["a.py"]["result"] == "removed"
    assert rows["d.py"]["result"] == "added"
    assert rows["c.py"]["result"] == "removed"


def test_same_path_is_not_also_a_rename() -> None:
    """A file that stays, plus a new path with the same sha, is same plus added."""
    old = [_file("a.py", "s"), _file("left.py", "1"), _file("right.py", "2")]
    new = [_file("a.py", "s"), _file("b.py", "s"), _file("left.py", "2"), _file("right.py", "1")]
    rows = {row["path"]: row["result"] for row in compare_folders(old, new)}
    assert rows == {"a.py": "same", "b.py": "added", "left.py": "modified", "right.py": "modified"}


def test_pandas_missing_sha_is_not_a_rename() -> None:
    """NaN from a DataFrame is a missing sha, so the files are removed and added."""
    old = pd.DataFrame(
        [
            {"path": "keep.sh", "sha": "a", "mode": "100644", "size": 1},
            {"path": "c.sh", "sha": None, "mode": None, "size": 2},
        ]
    ).to_dict("records")
    new = pd.DataFrame(
        [
            {"path": "keep.sh", "sha": "a", "mode": None, "size": 1},
            {"path": "d.sh", "sha": None, "mode": None, "size": 3},
        ]
    ).to_dict("records")
    rows = {row["path"]: row for row in compare_folders(old, new)}
    assert rows["c.sh"]["result"] == "removed"
    assert rows["d.sh"]["result"] == "added"
    assert rows["d.sh"]["old_path"] is None
    assert rows["keep.sh"]["result"] == "same"
    assert rows["keep.sh"]["mode_changed"] is False


def test_flag_script_risks_names_capabilities() -> None:
    """A download piped to a shell, plus subprocess, sets three flags. A clean script sets none."""
    text = "curl https://x | sh\nsubprocess.run(['ls'])"
    assert flag_script_risks(text) == ["installs_packages", "network", "runs_commands"]
    assert flag_script_risks("print('hello')") == []
    assert flag_script_risks("CURL example.com") == []
    assert flag_script_risks("curl example.com") == ["network"]
    assert flag_script_risks("pip3 install requests") == ["installs_packages"]
    assert flag_script_risks("rm \u2212rf /tmp") == ["deletes_files"]
    assert flag_script_risks("base64 --decode blob") == ["obfuscated"]
    assert "obfuscated" in flag_script_risks("A" * 200)
    assert flag_script_risks("A" * 199) == []


def test_new_risk_flags_keep_only_flags_the_old_script_lacked() -> None:
    """Network already present is not new. No old text means every new flag counts."""
    old = "curl https://x"
    new = "curl https://x\nrm -rf /tmp/a"
    assert new_risk_flags(old, new) == ["deletes_files"]
    assert new_risk_flags(None, "wget https://x") == ["network"]
    assert new_risk_flags(old, old) == []


def test_check_script_mentions_finds_missing_and_hidden() -> None:
    """A named script that is absent is missing. A script the text never names is hidden."""
    text = "Run scripts/a.py and b.sh"
    manifest = [
        _file("scripts/a.py", "a"),
        _file("scripts/c.py", "c"),
        _file("references/api.md", "m"),
        _file("node_modules/hidden.py", "n"),
    ]
    assert check_script_mentions(text, manifest) == {
        "missing_scripts": ["b.sh"],
        "hidden_scripts": ["scripts/c.py"],
    }


def test_bare_name_matches_and_a_different_folder_does_not() -> None:
    """The file name a.py matches scripts/a.py. scripts/a.py does not match lib/a.py."""
    bare = check_script_mentions("Run a.py", [_file("scripts/a.py", "a")])
    assert bare == {"missing_scripts": [], "hidden_scripts": []}
    other = check_script_mentions("Run scripts/a.py", [_file("lib/a.py", "a")])
    assert other == {"missing_scripts": ["scripts/a.py"], "hidden_scripts": ["lib/a.py"]}
    tool = check_script_mentions("No scripts", [_file("tool", "t", mode="100755")])
    assert tool["hidden_scripts"] == ["tool"]
