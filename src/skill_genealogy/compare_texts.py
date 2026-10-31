"""What changed between two skill texts.

What: line diff, share of lines kept, headings, commands, scripts,
network refs, steps, project names, change state, section map and
front matter.
Inputs: two skill texts (older or registry, newer or copy). Project
names also use the copy's repo and path.
Outputs: TextChange records and plain dicts. Nothing is written.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass

from skill_genealogy.settings import (
    SCRIPT_EXTENSIONS,
    SECTION_KEPT_MIN_SHARE,
    SUBSTANTIVE_BODY_CHANGE_PCT,
)
from skill_genealogy.text_helpers import (
    body_lines,
    normalize_text,
    parse_front_matter,
    split_front_matter,
)

# Generic words from a repo path. A project name has to be more specific.
# The list is the one in the G03 card.
_NAME_STOP_WORDS = frozenset(
    {
        "skill",
        "skills",
        "claude",
        "agents",
        "agent",
        "cursor",
        "codex",
        "github",
        "plugins",
        "plugin",
        "main",
        "master",
        "docs",
        "source",
        "repo",
        "tools",
        "commands",
        "config",
        "project",
        "template",
        "awesome",
        "local",
        "shared",
        "common",
        "core",
    }
)
_HEADING = re.compile(r"^#{1,6}[ \t]+\S")
_STEP = re.compile(r"^[0-9]+[.)][ \t]+\S")
_FENCE_MARKER = re.compile(r"^(`{3,}|~{3,})(.*)$")
_SHELL_INFO = re.compile(r"^(?:bash|sh|shell)\b", re.IGNORECASE)
_NAME_WORD = re.compile(r"[A-Za-z]{4,}")
_NETWORK = re.compile(r"https?://[^\s<>\]\)\"'`]+|\b(?:curl|wget)\b", re.IGNORECASE)
_SCRIPT_SUFFIX = "|".join(re.escape(extension) for extension in SCRIPT_EXTENSIONS)
_SCRIPT_PATH = re.compile(
    rf"(?<![\w./-])((?:\./|\.\./)?(?:[\w.-]+/)*[\w.-]+(?:{_SCRIPT_SUFFIX}))(?![\w])",
    re.IGNORECASE,
)
# Node.js, Next.js, Three.js. A path, or a PascalCase .ts file, is not one of these.
_LIBRARY_JS = re.compile(r"^[A-Z][A-Za-z0-9]*\.js$")
_ALLOWED_TOOLS = "allowed-tools"


@dataclass
class TextChange:
    """What changed between an older skill text and a newer one.

    ``lines_added`` and ``lines_removed`` count body lines. ``body_change_pct``
    is (added + removed) / max(old body lines, 1) * 100. ``share_of_old_lines_kept``
    is the share of old body lines still present. ``state`` is ``unchanged``,
    ``cosmetic`` or ``substantive``. ``steps_added`` is the new step count minus
    the old one, so it is negative when steps were removed.
    """

    lines_added: int
    lines_removed: int
    body_change_pct: float
    share_of_old_lines_kept: float
    headings_added: list[str]
    headings_removed: list[str]
    commands_added: list[str]
    scripts_mentioned_added: list[str]
    network_refs_added: list[str]
    steps_added: int
    names_added: list[str]
    state: str

    def to_dict(self) -> dict[str, object]:
        """Return every field as a plain dict.

        Example:
        >>> TextChange(0, 0, 0.0, 1.0, [], [], [], [], [], 0, [], "unchanged").to_dict()["state"]
        'unchanged'
        """
        return asdict(self)


def _common_edges(old_lines: list[str], new_lines: list[str]) -> tuple[int, int]:
    """Count the shared lines at the start and at the end, without overlap.

    Example:
    >>> _common_edges(["a", "b", "c"], ["a", "x", "c"])
    (1, 1)
    >>> _common_edges(["a", "b"], ["a", "b"])
    (2, 0)
    """
    prefix = 0
    shortest = min(len(old_lines), len(new_lines))
    while prefix < shortest and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    suffix = 0
    old_left = len(old_lines) - prefix
    new_left = len(new_lines) - prefix
    while (
        suffix < old_left
        and suffix < new_left
        and old_lines[-1 - suffix] == new_lines[-1 - suffix]
    ):
        suffix += 1
    return prefix, suffix


def _diff_middle(old_lines: list[str], new_lines: list[str]) -> list[tuple[str, str]]:
    """Diff the middle with SequenceMatcher. autojunk is off.

    The matcher finds shared stretches. It is not a true longest common
    subsequence, so very repetitive text can under-count kept lines.

    Example:
    >>> _diff_middle(["Drop"], ["Add"])
    [('-', 'Drop'), ('+', 'Add')]
    """
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    diff: list[tuple[str, str]] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            diff.extend((" ", line) for line in old_lines[old_start:old_end])
        elif tag == "delete":
            diff.extend(("-", line) for line in old_lines[old_start:old_end])
        elif tag == "insert":
            diff.extend(("+", line) for line in new_lines[new_start:new_end])
        else:
            diff.extend(("-", line) for line in old_lines[old_start:old_end])
            diff.extend(("+", line) for line in new_lines[new_start:new_end])
    return diff


# Algorithm A9, see CODE_FLOW.md
def _diff_line_lists(old_lines: list[str], new_lines: list[str]) -> list[tuple[str, str]]:
    """Turn two line lists into equal, removed and added pairs.

    Lines that match at the start and at the end are emitted as ``" "``
    first. ``difflib.SequenceMatcher`` then runs only on the middle, with
    ``autojunk`` off, so a short line that appears often still counts.
    ``"-"`` is only in the old list and ``"+"`` is only in the new list.

    Example:
    >>> _diff_line_lists(["Keep", "Drop"], ["Keep", "Add"])
    [(' ', 'Keep'), ('-', 'Drop'), ('+', 'Add')]
    >>> _diff_line_lists(["a", "b", "c"], ["a", "x", "c"])
    [(' ', 'a'), ('-', 'b'), ('+', 'x'), (' ', 'c')]
    """
    prefix, suffix = _common_edges(old_lines, new_lines)
    old_middle = old_lines[prefix : len(old_lines) - suffix]
    new_middle = new_lines[prefix : len(new_lines) - suffix]
    diff = [(" ", line) for line in old_lines[:prefix]]
    diff.extend(_diff_middle(old_middle, new_middle))
    if suffix:
        diff.extend((" ", line) for line in old_lines[len(old_lines) - suffix :])
    return diff


def _kept_count(old_lines: list[str], new_lines: list[str]) -> int:
    """Count old lines that the line diff marks as unchanged.

    Example:
    >>> _kept_count(["a", "b"], ["a", "c"])
    1
    """
    return sum(1 for operation, _line in _diff_line_lists(old_lines, new_lines) if operation == " ")


def _first_new_items(old_items: list[str], new_items: list[str]) -> list[str]:
    """Items that appear in ``new_items`` and not in ``old_items``, in order.

    A repeated new item is returned once.

    Example:
    >>> _first_new_items(["a", "b"], ["b", "c", "c"])
    ['c']
    """
    old_set = set(old_items)
    added: list[str] = []
    seen: set[str] = set()
    for item in new_items:
        if item in old_set or item in seen:
            continue
        seen.add(item)
        added.append(item)
    return added


def _body(text: str) -> str:
    """Return the normalised body, with front matter removed.

    Example:
    >>> _body("---\\nname: a\\n---\\nHi")
    'Hi'
    """
    _front, body = split_front_matter(normalize_text(text))
    return body


def _is_fence_marker(line: str) -> bool:
    """True when the line opens or closes a ``` or ~~~ fence.

    Example:
    >>> _is_fence_marker("```bash")
    True
    >>> _is_fence_marker("# Use")
    False
    """
    return _FENCE_MARKER.match(line.strip()) is not None


def _marked_body_lines(text: str) -> list[tuple[str, bool]]:
    """Return each body line and whether it sits inside a code fence.

    Fence markers are included and marked as not inside the fence. A heading
    or a numbered step inside a fence is code, not a section title.

    Example:
    >>> _marked_body_lines("Keep\\n```\\n# inside\\n```\\n# Out")
    [('Keep', False), ('```', False), ('# inside', True), ('```', False), ('# Out', False)]
    """
    marked: list[tuple[str, bool]] = []
    inside_fence = False
    for line in body_lines(text):
        if _is_fence_marker(line):
            marked.append((line, False))
            inside_fence = not inside_fence
            continue
        marked.append((line, inside_fence))
    return marked


def _fence_state(line: str, current: str) -> str | None:
    """Return the fence state after this line, or None when it is not a fence.

    ``""`` means outside a fence, ``"shell"`` means inside bash/sh/shell,
    and ``"other"`` means inside any other fence.

    Example:
    >>> _fence_state("```bash", "")
    'shell'
    >>> _fence_state("```", "shell")
    ''
    """
    match = _FENCE_MARKER.match(line.strip())
    if match is None:
        return None
    if current:
        return ""
    info = match.group(2).strip()
    if _SHELL_INFO.match(info):
        return "shell"
    return "other"


# Algorithm A9, see CODE_FLOW.md
def diff_body_lines(old_text: str, new_text: str) -> list[tuple[str, str]]:
    """List body lines that stayed, were removed, or were added.

    Both texts are normalised and the front matter is removed. Blank lines
    are dropped. The line lists are compared with
    ``difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)``,
    which finds the longest shared stretches and does not skip lines that
    appear often. Each stretch becomes ``" "`` for a line in both bodies,
    ``"-"`` for a line only in the old body, and ``"+"`` for a line only in
    the new body.

    Example:
    >>> diff_body_lines("---\\nname: a\\n---\\nKeep\\nDrop", "---\\nname: b\\n---\\nKeep\\nAdd")
    [(' ', 'Keep'), ('-', 'Drop'), ('+', 'Add')]
    >>> diff_body_lines("", "")
    []
    """
    return _diff_line_lists(body_lines(old_text), body_lines(new_text))


# Algorithm A10, see CODE_FLOW.md
def share_of_registry_lines_kept(registry_text: str, copy_text: str) -> float:
    """Share of registry body lines that the copy still has, unchanged.

    Count the ``" "`` lines from the line diff and divide by the number of
    registry body lines. An empty registry body is 0.0, because there is
    nothing to keep. The same empty result is used when both texts are empty.

    Example:
    >>> share_of_registry_lines_kept("keep\\ndrop", "keep\\nextra")
    0.5
    >>> share_of_registry_lines_kept("one\\ntwo", "one\\nextra\\ntwo")
    1.0
    >>> share_of_registry_lines_kept("", "one")
    0.0
    """
    registry_lines = body_lines(registry_text)
    if not registry_lines:
        return 0.0
    diff = diff_body_lines(registry_text, copy_text)
    kept = sum(1 for operation, _line in diff if operation == " ")
    return kept / len(registry_lines)


def find_headings(text: str) -> list[str]:
    """Return markdown heading lines from the body, in order.

    A heading is a body line outside a code fence that starts with one to
    six ``#`` marks and a space. The returned line includes the ``#`` marks.
    Front matter is not searched.

    Example:
    >>> find_headings("---\\nname: x\\n---\\n# Use\\n```bash\\n# comment\\n```\\n## Deploy")
    ['# Use', '## Deploy']
    >>> find_headings("no heading here")
    []
    """
    headings: list[str] = []
    for line, inside_fence in _marked_body_lines(text):
        stripped = line.strip()
        if not inside_fence and _HEADING.match(stripped):
            headings.append(stripped)
    return headings


def find_commands(text: str) -> list[str]:
    """Return shell command lines from the body.

    A command is a non-empty line inside a fence tagged bash, sh or shell,
    or any line outside a fence that starts with ``$ ``. Lines inside other
    fences are skipped. The ``$ `` prompt stays on the line.

    Example:
    >>> find_commands("```bash\\nnpm test\\n```\\n$ echo hi\\n```python\\n$ skip\\n```")
    ['npm test', '$ echo hi']
    >>> find_commands("```python\\nprint(1)\\n```")
    []
    """
    commands: list[str] = []
    fence = ""
    for line in body_lines(text):
        updated = _fence_state(line, fence)
        if updated is not None:
            fence = updated
            continue
        stripped = line.strip()
        if fence == "shell" or (fence == "" and stripped.startswith("$ ")):
            commands.append(stripped)
    return commands


def find_script_mentions(text: str) -> list[str]:
    """Return script paths mentioned in the body.

    A mention is a path whose ending is one of ``SCRIPT_EXTENSIONS``
    (.py .sh .bash .js .mjs .ts .ps1 .rb). Each path is returned once,
    in the order it first appears. A name with no slash that is only a
    library, such as Node.js or Next.js, is dropped. A PascalCase ``.ts``
    file is kept. Front matter is not searched.

    Example:
    >>> find_script_mentions("Run `scripts/deploy.sh` then notes.txt")
    ['scripts/deploy.sh']
    >>> find_script_mentions("Use Next.js, Node.js and scripts/build.js")
    ['scripts/build.js']
    >>> find_script_mentions("No scripts here")
    []
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _SCRIPT_PATH.finditer(_body(text)):
        path = match.group(1)
        if path in seen or ("/" not in path and _LIBRARY_JS.fullmatch(path)):
            continue
        seen.add(path)
        found.append(path)
    return found


def _network_token(raw: str) -> str:
    """Normalise one network match: a URL, or the lowercase word curl or wget.

    Example:
    >>> _network_token("HTTPS://Example.com.")
    'HTTPS://Example.com'
    >>> _network_token("CURL")
    'curl'
    """
    trimmed = raw.rstrip(".,;:!?")
    if trimmed.lower() in {"curl", "wget"}:
        return trimmed.lower()
    return trimmed


def find_network_refs(text: str) -> list[str]:
    """Return URLs and the words curl and wget from the body.

    Each http or https URL is kept as written, without a trailing punctuation
    mark. curl and wget are lowercase words. The same ref is returned once,
    in the order it first appears.

    Example:
    >>> find_network_refs("curl https://example.com/x and wget later")
    ['curl', 'https://example.com/x', 'wget']
    >>> find_network_refs("no network")
    []
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _NETWORK.finditer(_body(text)):
        token = _network_token(match.group(0))
        if not token or token in seen:
            continue
        seen.add(token)
        found.append(token)
    return found


def count_steps(text: str) -> int:
    """Count numbered list items in the body.

    An item is a line outside a code fence that starts with a number, then
    ``.`` or ``)``, then a space and some text. ``1.`` and ``2)`` both count.
    Front matter is ignored.

    Example:
    >>> count_steps("1. Open\\n```\\n2. not a step\\n```\\n2) Save")
    2
    >>> count_steps("")
    0
    """
    total = 0
    for line, inside_fence in _marked_body_lines(text):
        if inside_fence:
            continue
        if _STEP.match(line.strip()):
            total += 1
    return total


def _project_words(repo: str, path: str) -> list[str]:
    """Owner, repo and path words of 4 or more letters, stop words removed.

    Example:
    >>> _project_words("acme/agent-tools", "skills/pdf/SKILL.md")
    ['acme']
    """
    owner, _separator, repo_name = repo.partition("/")
    words: list[str] = []
    seen: set[str] = set()
    for piece in (owner, repo_name, path):
        for match in _NAME_WORD.finditer(piece):
            word = match.group(0).lower()
            if word in _NAME_STOP_WORDS or word in seen:
                continue
            seen.add(word)
            words.append(word)
    return words


def _has_word(text: str, word: str) -> bool:
    """True when ``word`` appears on its own, ignoring letter case.

    Example:
    >>> _has_word("Use ACME today", "acme")
    True
    >>> _has_word("acmes", "acme")
    False
    """
    pattern = rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


# Algorithm A11, see CODE_FLOW.md
def find_project_names_added(old_text: str, new_text: str, repo: str, path: str) -> list[str]:
    """Return project words that the new text adds.

    Words of 4 or more letters are taken from the owner, the repo name and
    the folder path, in that order, and lowercased. Generic words such as
    skill, tools and github are dropped. A remaining word is returned when
    it appears as a whole word in the new text and not in the old text.

    Example:
    >>> old = "Use the tool."
    >>> new = "Use the acme tool."
    >>> find_project_names_added(old, new, "acme/agent-tools", "skills/pdf/SKILL.md")
    ['acme']
    >>> find_project_names_added("Hello", "Hello skills", "acme/skills", "skills/SKILL.md")
    []
    """
    old_norm = normalize_text(old_text)
    new_norm = normalize_text(new_text)
    added: list[str] = []
    for word in _project_words(repo, path):
        if _has_word(new_norm, word) and not _has_word(old_norm, word):
            added.append(word)
    return added


# Algorithm A12, see CODE_FLOW.md
def classify_change_state(change_lines: int, old_line_count: int, body_identical: bool) -> str:
    """Label a body edit unchanged, cosmetic or substantive.

    Identical bodies are unchanged. Otherwise the edit is cosmetic when the
    changed lines are under ``SUBSTANTIVE_BODY_CHANGE_PCT`` percent of the
    old line count, and substantive at that percent or above. An old body
    with no lines uses 1 as the denominator, the same way as body_change_pct.
    Changed lines are lines added plus lines removed.

    Example:
    >>> classify_change_state(0, 10, True)
    'unchanged'
    >>> classify_change_state(1, 100, False)
    'cosmetic'
    >>> classify_change_state(5, 100, False)
    'substantive'
    """
    if body_identical:
        return "unchanged"
    baseline = max(old_line_count, 1)
    changed_pct = change_lines / baseline * 100
    if changed_pct < SUBSTANTIVE_BODY_CHANGE_PCT:
        return "cosmetic"
    return "substantive"


def compare_texts(old_text: str, new_text: str, repo: str = "", path: str = "") -> TextChange:
    """Summarise what changed from the old skill text to the new one.

    Line counts come from one body diff. The share kept is the unchanged
    lines in that diff divided by the old line count, or 0.0 when the old
    body is empty. Detectors list headings, commands, script paths and
    network refs that are in the new body and not the old. Names use the
    repo and path. State follows the body only: a front-matter edit with
    the same body is unchanged.

    Example:
    >>> change = compare_texts("Hello", "Hello")
    >>> (change.state, change.lines_added, change.lines_removed, change.share_of_old_lines_kept)
    ('unchanged', 0, 0, 1.0)
    """
    old_lines = body_lines(old_text)
    new_lines = body_lines(new_text)
    diff = _diff_line_lists(old_lines, new_lines)
    lines_added = sum(operation == "+" for operation, _line in diff)
    lines_removed = sum(operation == "-" for operation, _line in diff)
    kept_lines = sum(operation == " " for operation, _line in diff)
    old_count = len(old_lines)
    identical = old_lines == new_lines
    share_kept = 0.0 if old_count == 0 else kept_lines / old_count
    return TextChange(
        lines_added=lines_added,
        lines_removed=lines_removed,
        body_change_pct=(lines_added + lines_removed) / max(old_count, 1) * 100,
        share_of_old_lines_kept=share_kept,
        headings_added=_first_new_items(find_headings(old_text), find_headings(new_text)),
        headings_removed=_first_new_items(find_headings(new_text), find_headings(old_text)),
        commands_added=_first_new_items(find_commands(old_text), find_commands(new_text)),
        scripts_mentioned_added=_first_new_items(
            find_script_mentions(old_text), find_script_mentions(new_text)
        ),
        network_refs_added=_first_new_items(
            find_network_refs(old_text), find_network_refs(new_text)
        ),
        steps_added=count_steps(new_text) - count_steps(old_text),
        names_added=find_project_names_added(old_text, new_text, repo, path),
        state=classify_change_state(lines_added + lines_removed, old_count, identical),
    )


def _split_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split the body into (heading, lines) at markdown headings.

    The heading is the heading line with surrounding space removed. Lines
    before the first heading use an empty heading. A heading-shaped line
    inside a code fence stays in the current section.

    Example:
    >>> _split_sections("# Use\\n\\nDo this.")
    [('# Use', ['# Use', 'Do this.'])]
    """
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []
    seen_any = False
    for line, inside_fence in _marked_body_lines(text):
        stripped = line.strip()
        if not inside_fence and _HEADING.match(stripped):
            if seen_any:
                sections.append((heading, lines))
            heading = stripped
            lines = [line]
            seen_any = True
            continue
        lines.append(line)
        seen_any = True
    if seen_any:
        sections.append((heading, lines))
    return sections


def _section_status(share_kept: float) -> str:
    """Label one registry section kept, changed or removed.

    Removed is a share of 0. Kept is a share at or above
    ``SECTION_KEPT_MIN_SHARE``. Anything between those is changed.

    Example:
    >>> _section_status(0.0)
    'removed'
    >>> _section_status(0.9)
    'kept'
    >>> _section_status(0.8)
    'changed'
    """
    if share_kept == 0:
        return "removed"
    if share_kept >= SECTION_KEPT_MIN_SHARE:
        return "kept"
    return "changed"


def _match_section(
    sections: list[tuple[str, list[str]]], used: list[bool], heading: str
) -> int | None:
    """Return the first unused section with this heading, or None.

    Example:
    >>> _match_section([("# Use", ["# Use"]), ("# Use", ["# Use"])], [True, False], "# Use")
    1
    """
    for index, (section_heading, _lines) in enumerate(sections):
        if not used[index] and section_heading == heading:
            return index
    return None


# Algorithm A13, see CODE_FLOW.md
def build_section_map(registry_text: str, copy_text: str) -> list[dict[str, object]]:
    """Show how much of each registry section the copy kept, then new sections.

    Both bodies are split at ``#`` headings. Each registry section gets
    ``heading``, ``share_kept`` and ``status``: kept at or above 0.9, removed
    at 0, otherwise changed. Copy sections whose heading was not matched are
    appended with ``share_kept`` None and status ``new``. Lines before the
    first heading are a section with an empty heading. Registry sections come
    first, in their own order.

    Example:
    >>> registry = "# Keep\\n\\nSame.\\n\\n# Drop\\n\\nGone."
    >>> copy = "# Keep\\n\\nSame.\\n\\n# New\\n\\nAdded."
    >>> [(row["heading"], row["status"]) for row in build_section_map(registry, copy)]
    [('# Keep', 'kept'), ('# Drop', 'removed'), ('# New', 'new')]
    """
    registry_sections = _split_sections(registry_text)
    copy_sections = _split_sections(copy_text)
    used = [False] * len(copy_sections)
    rows: list[dict[str, object]] = []
    for heading, lines in registry_sections:
        match_index = _match_section(copy_sections, used, heading)
        if match_index is None:
            share_kept = 0.0
        else:
            used[match_index] = True
            copy_lines = copy_sections[match_index][1]
            share_kept = 0.0 if not lines else _kept_count(lines, copy_lines) / len(lines)
        rows.append(
            {
                "heading": heading,
                "share_kept": share_kept,
                "status": _section_status(share_kept),
            }
        )
    for index, (heading, _lines) in enumerate(copy_sections):
        if not used[index]:
            rows.append({"heading": heading, "share_kept": None, "status": "new"})
    return rows


def _front_matter(text: str) -> dict[str, object]:
    """Parse the normalised front matter. A missing or bad block is {}.

    Example:
    >>> _front_matter("---\\nname: demo\\n---\\nBody")
    {'name': 'demo'}
    >>> _front_matter("Body only")
    {}
    """
    front, _body_text = split_front_matter(normalize_text(text))
    return parse_front_matter(front)


def _tool_list(value: object) -> list[str]:
    """Turn an allowed-tools string or list into tool names.

    A string is split on commas. Each name is returned once, in order.

    Example:
    >>> _tool_list("Read, Bash")
    ['Read', 'Bash']
    >>> _tool_list(["Read", "Bash"])
    ['Read', 'Bash']
    """
    parts: list[object]
    if value is None:
        parts = []
    elif isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.extend(item.split(","))
            elif item is not None:
                parts.append(item)
    else:
        parts = [value]
    tools: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tool = str(part).strip()
        if not tool or tool in seen:
            continue
        seen.add(tool)
        tools.append(tool)
    return tools


# Algorithm A14, see CODE_FLOW.md
def compare_front_matter(old_text: str, new_text: str) -> dict[str, object]:
    """Compare YAML front matter field by field.

    Returns whether ``name`` and ``description`` changed, which keys were
    added or removed, and which ``allowed-tools`` entries were added or
    removed. That field may be a string or a list. ``permissions_changed``
    is true when the key is added or removed, or the set of tool names
    changes. The same tools in a different order are not a change.
    A missing or unreadable block is an empty dict.

    Example:
    >>> old = "---\\nallowed-tools: Read\\n---\\nBody"
    >>> new = "---\\nallowed-tools: Read, Bash\\n---\\nBody"
    >>> result = compare_front_matter(old, new)
    >>> result["allowed_tools_added"], result["permissions_changed"]
    (['Bash'], True)
    >>> swapped = "---\\nallowed-tools: Bash, Read\\n---\\nBody"
    >>> compare_front_matter(new, swapped)["permissions_changed"]
    False
    """
    old_fm = _front_matter(old_text)
    new_fm = _front_matter(new_text)
    old_tools = _tool_list(old_fm.get(_ALLOWED_TOOLS))
    new_tools = _tool_list(new_fm.get(_ALLOWED_TOOLS))
    old_has_tools = _ALLOWED_TOOLS in old_fm
    new_has_tools = _ALLOWED_TOOLS in new_fm
    return {
        "name_changed": old_fm.get("name") != new_fm.get("name"),
        "description_changed": old_fm.get("description") != new_fm.get("description"),
        "fm_keys_added": _first_new_items(list(old_fm), list(new_fm)),
        "fm_keys_removed": _first_new_items(list(new_fm), list(old_fm)),
        "allowed_tools_added": _first_new_items(old_tools, new_tools),
        "allowed_tools_removed": _first_new_items(new_tools, old_tools),
        "permissions_changed": old_has_tools != new_has_tools or set(old_tools) != set(new_tools),
    }
