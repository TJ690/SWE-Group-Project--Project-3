"""Tests for comparing two skill texts.

Hand-made strings only. No dataset files.
"""

from __future__ import annotations

import time

from skill_genealogy.compare_texts import (
    build_section_map,
    classify_change_state,
    compare_front_matter,
    compare_texts,
    count_steps,
    diff_body_lines,
    find_commands,
    find_headings,
    find_network_refs,
    find_project_names_added,
    find_script_mentions,
    share_of_registry_lines_kept,
)


def _numbered_body(line_count: int, changed_prefix: int = 0) -> str:
    """Return ``line_count`` body lines, with the first ones rewritten."""
    lines: list[str] = []
    for index in range(line_count):
        if index < changed_prefix:
            lines.append(f"changed line {index}")
        else:
            lines.append(f"line {index}")
    return "\n".join(lines)


def test_identical_texts_are_unchanged() -> None:
    """The same text has no line edits and keeps every body line."""
    text = "---\nname: demo\n---\n# Use\n\nHello\n"
    change = compare_texts(text, text)
    assert change.state == "unchanged"
    assert change.lines_added == 0
    assert change.lines_removed == 0
    assert change.body_change_pct == 0.0
    assert change.share_of_old_lines_kept == 1.0
    assert change.headings_added == []
    assert change.names_added == []
    assert change.to_dict()["state"] == "unchanged"


def test_quotes_and_line_endings_do_not_count_as_edits() -> None:
    """Normalising runs before the diff, so curly quotes are the same text."""
    change = compare_texts("“Hello”  \r\n", '"Hello"\n')
    assert change.state == "unchanged"
    assert change.lines_added == 0
    assert change.lines_removed == 0


def test_one_line_is_cosmetic_and_ten_lines_are_substantive() -> None:
    """One replaced line in 100 is under 5%. Ten replaced lines are not."""
    original = _numbered_body(100)
    one = compare_texts(original, _numbered_body(100, changed_prefix=1))
    ten = compare_texts(original, _numbered_body(100, changed_prefix=10))
    assert one.state == "cosmetic"
    assert one.lines_added == 1
    assert one.lines_removed == 1
    assert one.body_change_pct == 2.0
    assert one.share_of_old_lines_kept == 0.99
    assert ten.state == "substantive"
    assert ten.lines_added == 10
    assert ten.lines_removed == 10
    assert ten.body_change_pct == 20.0
    assert ten.share_of_old_lines_kept == 0.9


def test_change_state_boundary_is_five_percent() -> None:
    """Four added lines are cosmetic. Five added lines are substantive."""
    original = _numbered_body(100)
    four = original + "\n" + "\n".join(f"extra {index}" for index in range(4))
    five = original + "\n" + "\n".join(f"extra {index}" for index in range(5))
    assert compare_texts(original, four).state == "cosmetic"
    assert compare_texts(original, four).body_change_pct == 4.0
    assert compare_texts(original, five).state == "substantive"
    assert compare_texts(original, five).body_change_pct == 5.0
    assert classify_change_state(4, 100, False) == "cosmetic"
    assert classify_change_state(5, 100, False) == "substantive"


def test_added_deploy_section_fills_heading_script_and_network() -> None:
    """A new Deploy section reports the heading, the script path and curl."""
    old = "# Use\n\nRead the file.\n"
    new = "# Use\n\nRead the file.\n\n## Deploy\n\nRun scripts/deploy.sh\ncurl https://example.com/hook\n"
    change = compare_texts(old, new)
    assert change.headings_added == ["## Deploy"]
    assert change.headings_removed == []
    assert change.scripts_mentioned_added == ["scripts/deploy.sh"]
    assert change.network_refs_added == ["curl", "https://example.com/hook"]


def test_library_js_names_are_not_script_mentions() -> None:
    """Node.js and Next.js are libraries. A path, and a PascalCase .ts file, stay."""
    text = "Use Next.js, Node.js and scripts/build.js"
    assert find_script_mentions(text) == ["scripts/build.js"]
    assert find_script_mentions("Open Helper.ts next to Next.js") == ["Helper.ts"]


def test_commands_come_from_shell_fences_and_prompts() -> None:
    """bash fences and $ lines count. A prompt inside a python fence does not."""
    text = "```bash\nnpm test\n```\n$ echo hi\n```python\n$ skip\n```\n"
    assert find_commands(text) == ["npm test", "$ echo hi"]
    old = "```bash\nnpm test\n```\n"
    new = "```bash\nnpm test\nnpm build\n```\n"
    assert compare_texts(old, new).commands_added == ["npm build"]


def test_project_name_acme_is_added_and_skills_is_not() -> None:
    """acme is a project word. skills, agent and tools are on the stop list."""
    old = "Install the tool."
    new = "Install the acme tool. Use skills and tools."
    repo = "acme/agent-tools"
    path = "skills/pdf/SKILL.md"
    assert find_project_names_added(old, new, repo, path) == ["acme"]
    assert compare_texts(old, new, repo=repo, path=path).names_added == ["acme"]
    assert find_project_names_added("Hello", "Hello skills", "acme/skills", "skills/SKILL.md") == []
    described = "---\nname: demo\ndescription: for acme\n---\nBody\n"
    plain = "---\nname: demo\n---\nBody\n"
    assert find_project_names_added(plain, described, repo, path) == ["acme"]


def test_section_map_kept_removed_and_new() -> None:
    """One registry section stays, one is gone, and the copy adds one."""
    registry = "# Usage\n\nDo the thing.\n\n# Old\n\nGone now.\n"
    copy = "# Usage\n\nDo the thing.\n\n# Extra\n\nBrand new.\n"
    assert build_section_map(registry, copy) == [
        {"heading": "# Usage", "share_kept": 1.0, "status": "kept"},
        {"heading": "# Old", "share_kept": 0.0, "status": "removed"},
        {"heading": "# Extra", "share_kept": None, "status": "new"},
    ]


def test_section_share_boundaries() -> None:
    """Nine of ten lines is kept. Eight of ten is changed. A preamble is a section."""
    kept_lines = "\n".join(f"line {index}" for index in range(9))
    registry = f"# Usage\n{kept_lines}\n"
    changed = "\n".join(f"edited {index}" for index in range(2))
    mostly = f"# Usage\n{kept_lines.replace('line 0', 'edited 0', 1)}\n"
    more = f"# Usage\n{changed}\n" + "\n".join(f"line {index}" for index in range(2, 9)) + "\n"
    assert build_section_map(registry, mostly)[0]["status"] == "kept"
    assert build_section_map(registry, mostly)[0]["share_kept"] == 0.9
    assert build_section_map(registry, more)[0]["status"] == "changed"
    assert build_section_map(registry, more)[0]["share_kept"] == 0.8
    same = "Intro line.\n\n# Usage\n\nDo this."
    preamble = build_section_map(same, same)
    assert preamble[0] == {"heading": "", "share_kept": 1.0, "status": "kept"}
    fenced = "~~~bash\n# comment\n~~~\n# Use"
    assert find_headings(fenced) == ["# Use"]


def test_front_matter_allowed_tools_and_keys() -> None:
    """Adding Bash is a permission change. A list form of the same field matches."""
    old = "---\nname: demo\nallowed-tools: Read\n---\nBody\n"
    new = "---\nname: demo\ndescription: hi\nallowed-tools: Read, Bash\n---\nBody\n"
    result = compare_front_matter(old, new)
    assert result["allowed_tools_added"] == ["Bash"]
    assert result["allowed_tools_removed"] == []
    assert result["permissions_changed"] is True
    assert result["name_changed"] is False
    assert result["description_changed"] is True
    assert result["fm_keys_added"] == ["description"]
    assert result["fm_keys_removed"] == []
    listed = "---\nallowed-tools:\n  - Read\n  - Bash\n---\nBody\n"
    assert compare_front_matter(old, listed)["allowed_tools_added"] == ["Bash"]
    assert compare_front_matter(old, listed)["permissions_changed"] is True


def test_tool_order_alone_is_not_a_permission_change() -> None:
    """Read, Bash and Bash, Read are the same set of tools."""
    old = "---\nallowed-tools: Read, Bash\n---\nBody\n"
    new = "---\nallowed-tools: Bash, Read\n---\nBody\n"
    result = compare_front_matter(old, new)
    assert result["permissions_changed"] is False
    assert result["allowed_tools_added"] == []
    assert result["allowed_tools_removed"] == []


def test_front_matter_only_edit_leaves_the_body_unchanged() -> None:
    """State follows the body. The name change is reported on its own."""
    old = "---\nname: a\n---\nBody\n"
    new = "---\nname: b\n---\nBody\n"
    assert compare_texts(old, new).state == "unchanged"
    result = compare_front_matter(old, new)
    assert result["name_changed"] is True
    assert result["permissions_changed"] is False


def test_broken_front_matter_is_an_empty_block() -> None:
    """A YAML error drops the old keys and does not raise."""
    old = "---\nname: demo\nallowed-tools: Read\n---\nBody\n"
    new = "---\nname: [\n---\nBody\n"
    result = compare_front_matter(old, new)
    assert result["name_changed"] is True
    assert result["fm_keys_removed"] == ["name", "allowed-tools"]
    assert result["allowed_tools_removed"] == ["Read"]
    assert result["permissions_changed"] is True


def test_empty_old_and_new_text() -> None:
    """Two empty texts are unchanged, and an empty registry keeps nothing."""
    change = compare_texts("", "")
    assert change.state == "unchanged"
    assert change.lines_added == 0
    assert change.lines_removed == 0
    assert change.body_change_pct == 0.0
    assert change.share_of_old_lines_kept == 0.0
    assert change.headings_added == []
    assert change.commands_added == []
    assert change.scripts_mentioned_added == []
    assert change.network_refs_added == []
    assert change.steps_added == 0
    assert change.names_added == []
    assert diff_body_lines("", "") == []
    assert build_section_map("", "") == []
    empty_front = compare_front_matter("", "")
    assert empty_front["name_changed"] is False
    assert empty_front["permissions_changed"] is False
    assert empty_front["fm_keys_added"] == []


def test_empty_old_text_with_a_new_body_is_substantive() -> None:
    """There is no registry body to keep, and every new line counts."""
    change = compare_texts("", "Hello\nWorld\n")
    assert change.state == "substantive"
    assert change.lines_added == 2
    assert change.lines_removed == 0
    assert change.body_change_pct == 200.0
    assert change.share_of_old_lines_kept == 0.0
    assert share_of_registry_lines_kept("", "Hello") == 0.0


def test_steps_added_can_fall_when_items_are_removed() -> None:
    """steps_added is the new count minus the old count."""
    old = "1. Open\n2. Save\n"
    new = "1. Open\n"
    assert count_steps(old) == 2
    assert count_steps(new) == 1
    assert compare_texts(old, new).steps_added == -1


def test_repetitive_table_diff_stays_fast() -> None:
    """One changed row in a 3,000-line table keeps 2,999 lines and finishes quickly."""
    old_lines: list[str] = []
    new_lines: list[str] = []
    for index in range(1500):
        old_lines.append(f"| row | {index} |")
        if index == 750:
            new_lines.append("| row | changed |")
        else:
            new_lines.append(f"| row | {index} |")
        old_lines.append("| --- | --- |")
        new_lines.append("| --- | --- |")
    old = "\n".join(old_lines)
    new = "\n".join(new_lines)
    started = time.perf_counter()
    diff = diff_body_lines(old, new)
    change = compare_texts(old, new)
    elapsed = time.perf_counter() - started
    kept = sum(operation == " " for operation, _line in diff)
    assert elapsed < 2
    assert kept == 2999
    assert change.share_of_old_lines_kept == kept / 3000


def test_line_diff_ignores_front_matter_and_blank_lines() -> None:
    """A name change is not a body edit. Blank lines are not body lines."""
    old = "---\nname: a\n---\nKeep\n\n\nDrop\n"
    new = "---\nname: b\n---\nKeep\nAdd\n"
    assert diff_body_lines(old, new) == [(" ", "Keep"), ("-", "Drop"), ("+", "Add")]
    assert find_headings(old) == []
    assert find_script_mentions("See helper.py and helper.py again.") == ["helper.py"]
    assert find_network_refs("no network") == []
