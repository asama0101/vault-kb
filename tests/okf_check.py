"""束（vault-kb/）が OKF の形になっているかを確かめる、テスト用の検査。"""

import re
from pathlib import Path

from vault_kb.bundle import read_frontmatter
from vault_kb.vault import NOTES_NAME, RESERVED_NAMES

_HEADING = re.compile(r"## (\d{4}-\d{2}-\d{2})")


def _check_log(path: Path) -> list[str]:
    dates = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            match = _HEADING.fullmatch(line)
            if not match:
                return [f"{path}: 見出しが `## YYYY-MM-DD` でない: {line}"]
            dates.append(match[1])
    if any(a <= b for a, b in zip(dates, dates[1:], strict=False)):
        return [f"{path}: 見出しの日付が降順でない: {dates}"]
    return []


def _check_index(path: Path, base: Path) -> list[str]:
    if not path.read_text(encoding="utf-8").startswith("---"):
        return []
    if path != base / "index.md":
        return [f"{path}: index.md に frontmatter がある"]
    if set(read_frontmatter(path)) != {"okf_version"}:
        return [f"{path}: 根の index.md の frontmatter は okf_version だけにする"]
    return []


def check_bundle(base: Path) -> list[str]:
    """OKF の決まりに反する点を 1 件 1 文で返す（なければ空）。"""
    problems: list[str] = []
    for path in sorted(base.rglob("*.md")):
        if path.name == "log.md":
            problems += _check_log(path)
        elif path.name == "index.md":
            problems += _check_index(path, base)
        elif path.name in RESERVED_NAMES:
            continue
        elif (base / NOTES_NAME / f"{path.relative_to(base).as_posix()}.md").exists():
            continue  # .md の原本（notes_dir にノートがある）は書き換えない
        elif not path.read_text(encoding="utf-8").replace("\r\n", "\n").startswith("---\n"):
            problems.append(f"{path}: frontmatter がない")
        elif not read_frontmatter(path).get("type"):
            problems.append(f"{path}: type が空、または frontmatter が閉じていない")
    return problems
