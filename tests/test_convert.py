from pathlib import Path

import pytest
from markitdown import MarkItDown
from sample_docs import make_docx, make_pdf, make_pptx, make_txt, make_xlsx
from vault_kb.convert import extract, sha256_of, stat_of

TEXT = "Quarterly budget review for the sales team"
JP_TEXT = "第3四半期の販売計画と予算の見直しについて、各部門の数値を確認する"


@pytest.mark.parametrize(
    ("name", "make", "text"),
    [
        ("a.pptx", make_pptx, JP_TEXT),
        ("a.xlsx", make_xlsx, JP_TEXT),
        ("a.docx", make_docx, JP_TEXT),
        ("a.pdf", make_pdf, TEXT),
        ("a.txt", make_txt, JP_TEXT),
    ],
    ids=["pptx", "xlsx", "docx", "pdf", "txt"],
)
def test_extractは各形式の本文を取り出す(tmp_path: Path, name, make, text) -> None:
    path = tmp_path / name
    make(path, text)

    body, reason = extract(path)

    assert text in body
    assert reason is None


def test_extractはMarkItDownを資料ごとに作り直さない(tmp_path: Path, monkeypatch) -> None:
    created = []
    original_init = MarkItDown.__init__

    def counting_init(self, *args, **kwargs) -> None:
        created.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(MarkItDown, "__init__", counting_init)
    for name in ("a.txt", "b.txt"):
        make_txt(tmp_path / name, JP_TEXT)
        assert extract(tmp_path / name)[1] is None

    # 前のテストで作られていれば 0 回。毎回作り直す実装なら 2 回になる
    assert len(created) <= 1


def test_extractは文字のないPDFを理由つきで空にする(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    make_pdf(path, "")

    assert extract(path) == ("", "文字を取り出せません（0 字）")


def test_extractは壊れたOfficeファイルを理由つきで空にする(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

    body, reason = extract(path)

    assert body == ""
    assert reason is not None
    assert reason.startswith("FileConversionException")


def test_extractは未対応の拡張子を理由つきで空にする(tmp_path: Path) -> None:
    path = tmp_path / "data.zip"
    path.write_bytes(b"PK")

    assert extract(path) == ("", "対応しない形式です")


def test_stat_ofはサイズとmtime_nsを返す(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"abc")

    assert stat_of(path) == (3, path.stat().st_mtime_ns)


def test_sha256_ofは内容のハッシュを返す(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"abc")

    assert sha256_of(path) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def _raise_on_convert(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fail(self: MarkItDown, source: str) -> None:
        raise error

    monkeypatch.setattr(MarkItDown, "convert", fail)


def test_extractは想定外の例外を開けない理由にする(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.txt"
    make_txt(path, JP_TEXT)
    _raise_on_convert(monkeypatch, RuntimeError("locked"))

    body, reason = extract(path)

    assert body == ""
    assert reason == "開けません（パスワード付き・破損・使用中の可能性）: RuntimeError: locked"


def test_extractはOSErrorを呼び手に渡す(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.txt"
    make_txt(path, JP_TEXT)
    _raise_on_convert(monkeypatch, PermissionError("in use"))

    with pytest.raises(PermissionError, match="in use"):
        extract(path)


def test_extractは20字未満の本文を文字数つきの理由で空にする(tmp_path: Path) -> None:
    path = tmp_path / "short.txt"
    make_txt(path, "あいうえお")

    assert extract(path) == ("", "文字を取り出せません（5 字）")


LOG = (
    "2026-10-15 10:00:00 INFO サーバーを起動しました。ポートは 8080 です。"
    "\n2026-10-15 10:00:05 WARN メモリ使用率が高い状態です。"
)
CONF = "[server]\nhost = 127.0.0.1\nport = 8080\n# 本番の接続先を後で設定する\n"
HTML = (
    "<html><head><title>障害報告</title><script>var secret = 1;</script></head>"
    "<body><h1>障害報告</h1><p>原因はディスクの空き容量不足でした。</p></body></html>"
)


@pytest.mark.parametrize(
    ("name", "text", "encoding", "expected"),
    [
        ("a.log", LOG, "utf-8", "サーバーを起動しました"),
        ("b.log", LOG, "cp932", "サーバーを起動しました"),
        ("c.cfg", CONF, "utf-8", "本番の接続先を後で設定する"),
        ("d.conf", CONF, "cp932", "本番の接続先を後で設定する"),
        ("e.html", HTML, "utf-8", "原因はディスクの空き容量不足でした"),
        ("f.htm", HTML, "utf-8", "原因はディスクの空き容量不足でした"),
    ],
    ids=["log-utf8", "log-cp932", "cfg", "conf-cp932", "html", "htm"],
)
def test_extractはログ_設定_HTMLの本文を取り出す(
    tmp_path: Path, name, text, encoding, expected
) -> None:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))

    body, reason = extract(path)

    assert expected in body
    assert reason is None


def test_extractはHTMLのscriptを本文に含めない(tmp_path: Path) -> None:
    path = tmp_path / "e.html"
    path.write_text(HTML, encoding="utf-8")

    body, _ = extract(path)

    assert "原因はディスクの空き容量不足でした" in body
    assert "secret" not in body
