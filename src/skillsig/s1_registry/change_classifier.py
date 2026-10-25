"""
STAGE: s1_registry
MODULE: change_classifier
PURPOSE: Measure and classify a version-to-version skill change as substantive or cosmetic.
INPUTS: two SKILL.md texts and their bundled file lists
OUTPUTS: none (in-memory ChangeMetrics)
RQ LINK: RQ2 (substantive vs cosmetic edits; commands, network refs, scripts, steps)
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from skillsig.config import Config
from skillsig.s2_features.structure import split_front_matter

_HWS = re.compile(r"[^\S\n]+")
_ALL_WS = re.compile(r"\s+")
_HEADING = re.compile(r"^(#{1,6}) (.+)$")
_NUMBERED = re.compile(r"(?m)^[ \t]*[0-9]+[.)][ \t]")
_PROMPT = re.compile(r"^(?:[$%>]|ps>)\s+(\S.*)$")
_TOKEN = re.compile(r"^([a-z][\w.+-]*)\b")
_INLINE = re.compile(r"\$\s+([A-Za-z][^`\n]*)")
_NETWORK = re.compile(
    r"https?://|\bcurl\b|\bwget\b|\bfetch\s*\(|\brequests\.get\b|\bhttp\.client\b"
)
_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".js", ".ts", ".ps1"})
_CLIS = frozenset(
    {
        "apt",
        "apt-get",
        "aws",
        "az",
        "bash",
        "brew",
        "bundle",
        "bun",
        "bunx",
        "cargo",
        "composer",
        "conda",
        "curl",
        "deno",
        "docker",
        "gem",
        "gh",
        "git",
        "go",
        "gcloud",
        "gradle",
        "helm",
        "kubectl",
        "make",
        "mvn",
        "node",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pipx",
        "pnpm",
        "podman",
        "poetry",
        "pwsh",
        "python",
        "python3",
        "sh",
        "sudo",
        "terraform",
        "uv",
        "wget",
        "yarn",
        "zsh",
    }
)


@dataclass(frozen=True)
class ChangeMetrics:
    body_change_pct: float
    desc_similarity: float
    headings_added: int
    headings_removed: int
    files_delta: int
    commands_added: int = 0
    commands_removed: int = 0
    network_added: int = 0
    network_removed: int = 0
    scripts_added: int = 0
    scripts_removed: int = 0
    steps_delta: int = 0
    description_changed: bool = False
    files_changed: bool = False


def diff_versions(
    old_text: str, new_text: str, old_files: list[str], new_files: list[str]
) -> ChangeMetrics:
    """Metrics for one version step. Texts may include YAML front matter."""
    old_desc, old_body = _split(old_text)
    new_desc, new_body = _split(new_text)
    old_norm, new_norm = _norm_text(old_desc), _norm_text(new_desc)
    headings_added, headings_removed = _added_removed(_headings(old_body), _headings(new_body))
    commands_added, commands_removed = _added_removed(_commands(old_body), _commands(new_body))
    network_added, network_removed = _added_removed(
        _network_lines(old_body), _network_lines(new_body)
    )
    scripts_added, scripts_removed = _added_removed(
        _script_names(old_files), _script_names(new_files)
    )
    old_paths, new_paths = _file_set(old_files), _file_set(new_files)
    return ChangeMetrics(
        body_change_pct=_body_change_pct(_norm_lines(old_body), _norm_lines(new_body)),
        desc_similarity=_similarity(old_norm, new_norm),
        headings_added=headings_added,
        headings_removed=headings_removed,
        files_delta=len(new_paths) - len(old_paths),
        commands_added=commands_added,
        commands_removed=commands_removed,
        network_added=network_added,
        network_removed=network_removed,
        scripts_added=scripts_added,
        scripts_removed=scripts_removed,
        steps_delta=_step_count(new_body) - _step_count(old_body),
        description_changed=old_norm != new_norm,
        files_changed=old_paths != new_paths,
    )


def classify_change(m: ChangeMetrics, cfg: Config) -> str:
    """'substantive' when the body, description, or bundled files changed enough; else cosmetic."""
    description_changed = m.description_changed or m.desc_similarity < 1.0
    files_changed = m.files_changed or m.files_delta != 0
    large_body = m.body_change_pct >= cfg.rq2.substantive_body_change_pct
    if large_body or description_changed or files_changed:
        return "substantive"
    return "cosmetic"


def content_signals(text: str, files: list[str]) -> dict[str, int]:
    """Count commands, network refs, bundled scripts, and numbered body steps."""
    _description, body = _split(text)
    return {
        "commands": len(_commands(body)),
        "network": len(_network_lines(body)),
        "scripts": len(_script_names(files)),
        "steps": _step_count(body),
    }


def _split(text: str) -> tuple[str, str]:
    loaded, body, ok = split_front_matter(text)
    return _description(loaded, ok=ok), body


def _description(loaded: dict[str, Any], *, ok: bool) -> str:
    if not ok:
        return ""
    description = loaded.get("description")
    if isinstance(description, str):
        return description
    if description is None:
        return ""
    return str(description)


def _norm_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.lower().splitlines():
        collapsed = _HWS.sub(" ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return lines


def _norm_text(text: str) -> str:
    return _ALL_WS.sub(" ", text.lower()).strip()


def _body_change_pct(old: list[str], new: list[str]) -> float:
    if old == new:
        return 0.0
    ratio = difflib.SequenceMatcher(a=old, b=new, autojunk=False).ratio()
    return 100.0 * (1.0 - ratio)


def _similarity(old: str, new: str) -> float:
    if old == new:
        return 1.0
    return difflib.SequenceMatcher(a=old, b=new, autojunk=False).ratio()


def _headings(body: str) -> list[str]:
    return [line for line in _norm_lines(body) if _HEADING.fullmatch(line)]


def _commands(body: str) -> list[str]:
    items: list[str] = []
    pending: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            items.extend(_flush_commands(pending, fenced=fenced))
            pending = []
            fenced = not fenced
            continue
        pending.append(line)
    items.extend(_flush_commands(pending, fenced=fenced))
    return items


def _flush_commands(lines: list[str], *, fenced: bool) -> list[str]:
    if fenced:
        return [cmd for line in lines if (cmd := _fenced_command(line))]
    return _inline_commands("\n".join(lines))


def _fenced_command(line: str) -> str | None:
    text = _norm_text(line)
    if not text:
        return None
    prompted = _PROMPT.match(text)
    if prompted:
        return _norm_text(prompted.group(1))
    token = _TOKEN.match(text)
    if token and token.group(1) in _CLIS:
        return text
    return None


def _inline_commands(text: str) -> list[str]:
    found: list[str] = []
    for match in _INLINE.finditer(text):
        command = _norm_text(match.group(1))
        if command:
            found.append(command)
    return found


def _network_lines(body: str) -> list[str]:
    return [line for line in _norm_lines(body) if _NETWORK.search(line)]


def _step_count(body: str) -> int:
    return len(_NUMBERED.findall(body))


def _script_names(files: list[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in files:
        path = _clean_path(name)
        if not path or path in seen:
            continue
        seen.add(path)
        if PurePosixPath(path).suffix.lower() in _SCRIPT_SUFFIXES:
            names.append(path)
    return names


def _file_set(files: list[str]) -> set[str]:
    return {path for name in files if (path := _clean_path(name))}


def _clean_path(path: str) -> str:
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _added_removed(old: list[str], new: list[str]) -> tuple[int, int]:
    before, after = Counter(old), Counter(new)
    return sum((after - before).values()), sum((before - after).values())
