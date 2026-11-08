"""資料ファイルの Markdown 変換と、変更検知用の指紋（ハッシュ・サイズ・更新時刻）。"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path
from typing import Any

from .vault import SUPPORTED_EXTENSIONS

MIN_CHARS = 20  # これより短い本文は、文字を取り出せなかった（スキャン画像など）とみなす


def sha256_of(path: Path) -> str:
    """ファイルの内容の SHA-256（16 進）。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stat_of(path: Path) -> tuple[int, int]:
    """(サイズ, 更新時刻 ns)。"""
    info = path.stat()
    return info.st_size, info.st_mtime_ns


@functools.cache
def _converter() -> Any:
    """MarkItDown を 1 回だけ作る（作るたびに、ファイル種別を判定するモデルを読み込むため）。"""
    from markitdown import MarkItDown  # noqa: PLC0415 - 専用 venv にしかない

    return MarkItDown()


def extract(path: Path) -> tuple[str, str | None]:
    """(本文, None)。取り出せないときは ("", 理由)。OSError（使用中・権限）は呼び手に渡す。"""
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return "", "対応しない形式です"
    from markitdown import (  # noqa: PLC0415 - 専用 venv にしかない
        FileConversionException,
        MissingDependencyException,
        UnsupportedFormatException,
    )

    try:
        text = _converter().convert(str(path)).text_content.strip()
    except (FileConversionException, UnsupportedFormatException, MissingDependencyException) as e:
        return "", f"{type(e).__name__}: {e}"
    except OSError:
        raise
    except Exception as e:  # noqa: BLE001 - 変換ライブラリの想定外の失敗は理由にして続ける
        return "", f"開けません（パスワード付き・破損・使用中の可能性）: {type(e).__name__}: {e}"
    if len(text) < MIN_CHARS:
        return "", f"文字を取り出せません（{len(text)} 字）"
    return text, None
