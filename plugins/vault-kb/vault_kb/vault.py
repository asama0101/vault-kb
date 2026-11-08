"""vault の配置・設定と、フォルダ名・区分名・無視するファイルの規則。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

NOTES_NAME = ".notes"  # ノートと案件・区分の index.md を置く隠しフォルダ（vault の直下）
INBOX_NAME = "00_inbox"  # 仕分け前の資料を置くフォルダ
PROJECTS_NAME = "20_projects"  # <案件>/<区分>/<原本>
SHARED_NAME = "30_shared"  # どの案件にも属さない資料。<区分>/<原本>
ARCHIVE_NAME = "80_archive"  # 完了した案件。20_projects と同じ構造
SHARED_PROJECT = "shared"  # 30_shared の案件名
KNOWLEDGE_NAME = "40_knowledge"  # 案件に属さないノウハウ。<区分>/<原本>
KNOWLEDGE_PROJECT = "knowledge"  # 40_knowledge の案件名
PROJECT_ROOTS = (PROJECTS_NAME, ARCHIVE_NAME)
USER_FOLDERS = ("10_daily", "50_prompts", "90_attachments")  # 利用者の領域
OBSIDIAN_ASSETS = Path(__file__).resolve().parent.parent / "obsidian"  # テンプレートの原本
ASSET_FOLDERS = {
    "templates": "91_templates",
}  # 原本のフォルダ → vault 内の名前
DEFAULT_CATEGORY = "99_その他"
OKF_VERSION = "0.2"
SUPPORTED_EXTENSIONS = {
    ".pptx",
    ".xlsx",
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".log",
    ".cfg",
    ".conf",
    ".html",
    ".htm",
}
RESERVED_NAMES = {"index.md", "log.md"}
MAX_CATEGORY_NUMBER = 98

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_CATEGORY_NUMBER = re.compile(r"(\d+)_(.+)", re.DOTALL)
_IGNORED_NAMES = {"thumbs.db", "desktop.ini"}
_DEVICE_NAME = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", re.IGNORECASE)


class ConfigError(Exception):
    """設定がない・壊れている。"""


class VaultNotFound(ConfigError):
    """設定はあるが、vault のフォルダが見つからない（外付けドライブ・同期の切断など）。"""


def fold_name(name: str) -> str:
    """名前の比較用（Unicode の正規化と大文字小文字を畳む。Windows は大文字小文字を区別しない）。"""
    return unicodedata.normalize("NFC", name).casefold()


RESERVED_FOLDED = {fold_name(name) for name in RESERVED_NAMES}


def is_ignored(name: str) -> bool:
    """隠しファイル・Office の一時ファイル・OS が作るファイル・予約名は、資料として扱わない。"""
    return (
        name.startswith((".", "~$"))
        or name.lower() in _IGNORED_NAMES
        or fold_name(name) in RESERVED_FOLDED
    )


def is_supported(name: str) -> bool:
    """対応する拡張子で、無視しないファイルか。"""
    return not is_ignored(name) and Path(name).suffix.lower() in SUPPORTED_EXTENSIONS


_HIDDEN_ATTRIBUTE = 0x2


def walk(root: Path) -> tuple[list[Path], list[Path]]:
    """root の下の (フォルダ, ファイル)。"""
    folders: list[Path] = []
    files: list[Path] = []
    for here, names, found in os.walk(root):
        folders += [Path(here) / name for name in names]
        files += [Path(here) / name for name in found]
    return folders, files


def atomic_write(path: Path, text: str) -> None:
    """一時ファイルに書いてから置き換える（書き込み中の中断で、壊れたファイルを残さない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def move_file(src: Path, dst: Path) -> None:
    """ファイルを移す。失敗したら、移動先に何も残さない（コピーして元を消す方式は使わない）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)


def check_folder_name(name: str) -> None:
    """案件名・区分名として使えるか（空・使えない文字・index/log・Windows の予約名は不可）。"""
    if not name.strip(" ."):
        raise ValueError(f"フォルダ名にできません（空）: {name!r}")
    if _FORBIDDEN.search(name):
        raise ValueError(f"フォルダ名にできません（使えない文字を含む）: {name!r}")
    if fold_name(name) in {"index", "log"}:
        raise ValueError(f"フォルダ名にできません（予約語 index・log）: {name!r}")
    if _DEVICE_NAME.fullmatch(name):
        raise ValueError(f"フォルダ名にできません（Windows の予約名）: {name!r}")


def split_category(name: str) -> tuple[int | None, str]:
    """区分名 → (番号, 名前)。番号がなければ (None, 名前)。"""
    match = _CATEGORY_NUMBER.fullmatch(name.strip())
    return (int(match[1]), match[2]) if match else (None, name.strip())


def category_sort_key(name: str) -> tuple[int, int, str]:
    """番号つき（昇順）→ 番号なし（名前順）→ 99_その他。"""
    if name == DEFAULT_CATEGORY:
        return (2, 0, "")
    number, label = split_category(name)
    if number is None:
        return (1, 0, fold_name(label))
    return (0, number, fold_name(label))


def resolve_category(existing: list[str], raw: str) -> str | None:
    """指定（全名か番号なしの名前）に一致する既存の区分。なければ None。"""
    key = fold_name(raw)
    found = [c for c in existing if key in {fold_name(c), fold_name(split_category(c)[1])}]
    if len(found) > 1:
        raise ValueError(
            f"区分 {raw!r} は複数に一致します。番号つきの名前で指定してください: {found}"
        )
    return found[0] if found else None


def next_category_name(existing: list[str], raw: str) -> str:
    """新しい区分名。番号がなければ、99 を除く最大の番号の次を付ける。"""
    if split_category(raw)[0] is not None:
        return raw
    used = [n for n in (split_category(c)[0] for c in existing) if n is not None and n != 99]
    number = max(used, default=0) + 1
    if number > MAX_CATEGORY_NUMBER:
        raise ValueError(f"区分の番号は {MAX_CATEGORY_NUMBER} までです（99 は {DEFAULT_CATEGORY}）")
    return f"{number:02d}_{raw}"


@dataclass(frozen=True)
class Vault:
    """root は Obsidian の vault の根（OKF の束の根）。"""

    root: Path

    def project_folders(self) -> list[tuple[str, Path]]:
        """(案件名, フォルダ)。20_projects → 80_archive → 30_shared → 40_knowledge の順。"""
        found = []
        for name in PROJECT_ROOTS:
            folder = self.root / name
            if folder.is_dir():
                children = sorted(folder.iterdir())
                found += [
                    (f.name, f) for f in children if f.is_dir() and not f.name.startswith(".")
                ]
        for name, project in ((SHARED_NAME, SHARED_PROJECT), (KNOWLEDGE_NAME, KNOWLEDGE_PROJECT)):
            folder = self.root / name
            if folder.is_dir():
                found.append((project, folder))
        return found

    def project_dir(self, project: str) -> Path | None:
        """案件のフォルダ（同名は 20_projects を先にする）。なければ None。"""
        return next((folder for name, folder in self.project_folders() if name == project), None)

    @property
    def notes_dir(self) -> Path:
        return self.root / NOTES_NAME

    def note_path(self, original: Path) -> Path:
        """原本のノートの置き場所（notes_dir の下に、原本と同じ相対パス + .md）。"""
        return self.notes_dir / f"{original.relative_to(self.root).as_posix()}.md"

    def hide_notes_dir(self) -> None:
        """notes_dir を作り、Windows の隠し属性を付ける（エクスプローラーで隠す）。"""
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            attributes = kernel32.GetFileAttributesW(str(self.notes_dir))
            if attributes != -1 and not attributes & _HIDDEN_ATTRIBUTE:
                kernel32.SetFileAttributesW(str(self.notes_dir), attributes | _HIDDEN_ATTRIBUTE)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def path(self, rel: str) -> Path:
        return self.root / rel


def init_vault(root: Path) -> Vault:
    vault = Vault(root)
    for name in (INBOX_NAME, PROJECTS_NAME, ARCHIVE_NAME, *USER_FOLDERS):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in (SHARED_NAME, KNOWLEDGE_NAME):
        (root / name / DEFAULT_CATEGORY).mkdir(parents=True, exist_ok=True)
    return vault


def install_obsidian_assets(root: Path) -> None:
    """同梱のテンプレートを vault の 91_templates/ に置く（既存は残す）。"""
    for source in sorted(OBSIDIAN_ASSETS.glob("*/*")):
        target = root / ASSET_FOLDERS[source.parent.name] / source.name
        if not target.exists():
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)


def save_config(data_dir: Path, vault_root: Path) -> None:
    config = {"vault": str(vault_root)}
    atomic_write(data_dir / "config.json", json.dumps(config, ensure_ascii=False, indent=2))


def load_vault(data_dir: Path) -> Vault:
    path = data_dir / "config.json"
    if not path.exists():
        raise ConfigError("vault が未設定です。`configure --vault <パス>` を実行してください。")
    broken = ConfigError(f"{path} が壊れています。`configure` をやり直してください。")
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))["vault"]
        root = Path(configured)
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise broken from error
    if not configured:  # Path("") は「現在のフォルダ」になってしまう
        raise broken
    if not root.is_dir():
        raise VaultNotFound(
            f"vault のフォルダが見つかりません（外付けドライブ・同期の切断などの可能性）: {root}"
        )
    return Vault(root)
