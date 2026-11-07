import os
import shutil
from datetime import datetime

import pytest
from helpers import make_note, put
from vault_kb.bundle import (
    append_log,
    candidates,
    canonical_client,
    categories,
    clients,
    inbox_files,
    is_original_file,
    link,
    location,
    notes,
    originals,
    project_root_files,
    projects,
    read_frontmatter,
    write_indexes,
    write_note,
)
from vault_kb.vault import Vault


def test_note_textはNOTE_KEYSの順にJSONで書き_read_frontmatterで同じ値に戻る(vault):
    meta = {
        "readable": False,
        "date": None,
        "tags": ["見積", "A社"],
        "type": "document",
        "resource": "見積.xlsx",
        "size": 12,
        "title": '見積 "A"',
    }
    path = vault.base / "20_projects" / "A" / "01_x" / "見積.xlsx.md"
    write_note(path, meta, "# 見出し\n本文\n")

    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "---"
    assert [line.split(":")[0] for line in lines[1:7]] == [
        "type",
        "title",
        "resource",
        "tags",
        "date",
        "size",
    ]
    assert lines[4] == 'tags: ["見積", "A社"]'
    assert lines[5] == "date: null"
    assert read_frontmatter(path) == meta
    assert path.read_text(encoding="utf-8").endswith("---\n\n# 見出し\n本文\n")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\ntitle: x\ntags:\n- a\n- b\ndate:\n---\nbody",
            {"title": "x", "tags": ["a", "b"], "date": None},
        ),
        ('---\r\ntitle: "q"\r\nsize: 3\r\n---\r\n', {"title": "q", "size": 3}),
        ("# 見出しだけ\n", {}),
        ("---\ntitle: x\n", {}),
    ],
    ids=["Obsidian書き直し", "CRLFと引用符", "frontmatterなし", "閉じていない"],
)
def test_read_frontmatterは書き直された形も読み_壊れた形は空を返す(tmp_path, text, expected):
    path = tmp_path / "n.md"
    path.write_bytes(text.encode("utf-8"))

    assert read_frontmatter(path) == expected


def test_read_frontmatterは64KiBを超える先頭でも閉じ線まで読む(tmp_path):
    path = tmp_path / "n.md"
    path.write_text(
        "---\ntitle: x\nmemo: " + "あ" * 40000 + "\nsize: 5\n---\n本文", encoding="utf-8"
    )

    assert read_frontmatter(path)["size"] == 5


def test_notesはtypeがdocumentでresourceのあるmdだけを返す(vault):
    good = make_note(vault, "A", "01_x", "見積.xlsx")
    memo = vault.notes_dir / "20_projects" / "A" / "01_x" / "memo-meta.md"
    memo.write_text("---\ntitle: x\n---\n", encoding="utf-8")
    plain = vault.notes_dir / "20_projects" / "A" / "01_x" / "memo.md"
    plain.write_text("ただのメモ", encoding="utf-8")
    index = vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md"
    index.write_text('---\ntype: document\nresource: "a"\n---\n', encoding="utf-8")

    found = notes(vault)

    assert [n.path for n in found] == [good]
    assert found[0].original == vault.base / "20_projects" / "A" / "01_x" / "見積.xlsx"
    assert found[0].meta["resource"] == "20_projects/A/01_x/見積.xlsx"


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("20_projects/A/01_x/f.xlsx", ("A", "01_x")),
        ("20_projects/A/01_x/sub/f.xlsx", ("A", "01_x")),
        ("20_projects/A/f.xlsx", None),
        ("00_inbox/f.xlsx", None),
        ("f.xlsx", None),
        ("80_archive/A/01_x/f.xlsx", ("A", "01_x")),
        ("30_shared/01_x/f.xlsx", ("shared", "01_x")),
        ("40_knowledge/01_x/f.xlsx", ("knowledge", "01_x")),
        ("30_shared/f.xlsx", None),
    ],
    ids=[
        "区分直下",
        "サブフォルダ",
        "案件直下",
        "inbox直下",
        "base直下",
        "archive",
        "shared",
        "knowledge",
        "shared直下",
    ],
)
def test_locationは深さ2以上で案件と区分を返し_浅いとNone(vault, rel, expected):
    assert location(vault, vault.path(rel)) == expected


def test_locationはbaseの外ならNone(vault):
    assert location(vault, vault.root / "other" / "A" / "01_x" / "f.xlsx") is None


def test_originalsは区分より深いサブフォルダとmdも含み_ノートと無視する名前を除く(vault):
    a = put(vault, "20_projects/A/01_x/見積.xlsx")
    b = put(vault, "20_projects/A/01_x/sub/deep/b.pdf")
    m = put(vault, "20_projects/A/01_x/メモ.md", "ただのメモ".encode())
    make_note(vault, "A", "01_x", "見積.xlsx")
    put(vault, "20_projects/A/01_x/index.md")
    put(vault, "20_projects/A/01_x/~$見積.xlsx")
    put(vault, "20_projects/A/01_x/.hidden.pdf")
    put(vault, "20_projects/A/01_x/Thumbs.db")
    put(vault, "20_projects/A/01_x/a.zip")
    put(vault, "20_projects/A/直下.xlsx")
    put(vault, "00_inbox/受信.xlsx")

    assert sorted(originals(vault)) == sorted([a, b, m])


def test_is_original_fileは開けないmdを原本にせず_次の機会に回す(vault, monkeypatch):
    memo = put(vault, "20_projects/A/01_x/メモ.md", "ただのメモ".encode())

    def locked(_path):
        raise PermissionError

    monkeypatch.setattr("vault_kb.bundle.read_frontmatter", locked)

    assert is_original_file(memo) is False


def test_is_original_fileは変わっていないmdを読み直さず_変わったら読み直す(vault, monkeypatch):
    memo = put(vault, "20_projects/A/01_x/メモ.md", "ただのメモ".encode())
    reads = []
    real = read_frontmatter
    monkeypatch.setattr(
        "vault_kb.bundle.read_frontmatter", lambda path: reads.append(path) or real(path)
    )

    assert is_original_file(memo) is True
    assert is_original_file(memo) is True
    assert len(reads) == 1

    note = make_note(vault, "A", "01_x", "見積.xlsx")
    assert is_original_file(note) is False
    note.write_text("ただのメモに書き直した", encoding="utf-8")
    assert is_original_file(note) is True


def test_candidatesは区分より下の無視しないファイルを拡張子を問わず返す(vault):
    a = put(vault, "20_projects/A/01_x/見積.xlsx")
    z = put(vault, "20_projects/A/01_x/sub/a.zip")
    make_note(vault, "A", "01_x", "見積.xlsx")
    put(vault, "20_projects/A/01_x/~$見積.xlsx")
    put(vault, "20_projects/A/01_x/Thumbs.db")
    put(vault, "20_projects/A/直下.zip")
    put(vault, "00_inbox/受信.zip")
    put(vault, ".obsidian/p/x.zip")
    put(vault, "20_projects/A/.trash/b.pdf")
    put(vault, "20_projects/A/01_x/.git/c.pdf")

    assert sorted(candidates(vault)) == sorted([a, z])


def test_inbox_filesは直下だけで_mdは含み_予約名と隠しファイルを除く(vault):
    z = put(vault, "00_inbox/a.zip")
    x = put(vault, "00_inbox/受信 1.xlsx")
    m = put(vault, "00_inbox/memo.md")
    put(vault, "00_inbox/index.md")
    put(vault, "00_inbox/.hidden")
    put(vault, "00_inbox/~$tmp.xlsx")
    put(vault, "20_projects/A/01_x/f.xlsx")

    assert sorted(inbox_files(vault)) == sorted([z, x, m])


def test_inbox_filesはvault直下のファイルを含めない(vault):
    z = put(vault, "00_inbox/a.zip")
    put(vault, "直下.xlsx")

    assert inbox_files(vault) == [z]


def test_project_root_filesは案件フォルダ直下のファイルだけを案件名つきで返す(vault):
    f = put(vault, "20_projects/A/直下.xlsx")
    put(vault, "20_projects/A/.x")
    put(vault, "20_projects/A/01_x/深い.xlsx")
    put(vault, "00_inbox/受信.xlsx")

    assert project_root_files(vault) == [("A", f)]


def test_projectsとcategoriesはフォルダ順に返す(vault):
    put(vault, "20_projects/B/02_b/f.xlsx")
    put(vault, "20_projects/B/01_a/f.xlsx")
    put(vault, "20_projects/B/99_その他/f.xlsx")
    put(vault, "20_projects/B/名前だけ/f.xlsx")
    put(vault, "20_projects/A/直下.xlsx")

    assert projects(vault) == ["A", "B", "knowledge", "shared"]
    assert categories(vault, "B") == ["01_a", "02_b", "名前だけ", "99_その他"]


def test_canonical_clientは全角半角_大文字小文字_空白を畳んで既存の表記に寄せる():
    assert canonical_client(["A社"], "Ａ 社") == "A社"
    assert canonical_client(["A社"], "B社") == "B社"
    assert canonical_client(["Foo Inc"], " fooinc ") == "Foo Inc"


def test_clientsは空を除いて初出順に重複なく返す(vault):
    make_note(vault, "A", "01_x", "a.xlsx", client="B社")
    make_note(vault, "A", "01_x", "b.xlsx", client=None)
    make_note(vault, "A", "01_x", "c.xlsx", client="A社")
    make_note(vault, "A", "01_x", "d.xlsx", client="B社")

    assert clients(notes(vault)) == ["B社", "A社"]


def test_write_indexesは根にokf_version_案件と区分の一覧_区分はdateの新しい順でnullは最後(vault):
    make_note(vault, "A", "01_x", "jan.xlsx", title="一月", date="2026-01-01", description="d1")
    make_note(vault, "A", "01_x", "mar.xlsx", title="三月", date="2026-03-01", description="d3")
    make_note(
        vault,
        "A",
        "01_x",
        "none.xlsx",
        title="日付なし",
        date=None,
        description="dn",
        readable=False,
    )
    make_note(vault, "A", "02_y", "o.xlsx", title="他")

    write_indexes(vault)

    root = (vault.base / "index.md").read_text(encoding="utf-8")
    assert root.startswith('---\nokf_version: "0.2"\n---\n# vault-kb\n\n')
    assert "- [A](<.notes/20_projects/A/index.md>) — 4 件\n" in root
    assert "- [shared](<.notes/30_shared/index.md>) — 0 件\n" in root
    project = (vault.notes_dir / "20_projects" / "A" / "index.md").read_text(encoding="utf-8")
    assert project == (
        "# A\n\n- [01_x](<01_x/index.md>) — 3 件\n- [02_y](<02_y/index.md>) — 1 件\n"
    )
    category = (vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md").read_text(
        encoding="utf-8"
    )
    assert category == (
        "# A / 01_x\n\n"
        "- [三月](<mar.xlsx.md>) — d3"
        "（原本: [mar.xlsx](<../../../../20_projects/A/01_x/mar.xlsx>)、2026-03-01）\n"
        "- [一月](<jan.xlsx.md>) — d1"
        "（原本: [jan.xlsx](<../../../../20_projects/A/01_x/jan.xlsx>)、2026-01-01）\n"
        "- [日付なし](<none.xlsx.md>) — dn"
        "（原本: [none.xlsx](<../../../../20_projects/A/01_x/none.xlsx>)、日付なし）"
        "（文字を取り出せていない）\n"
    )
    assert not (vault.base / "20_projects" / "A" / "index.md").exists()
    assert not (vault.base / "20_projects" / "A" / "01_x" / "index.md").exists()


def test_write_indexesはサブフォルダのノートを区分からの相対パスで載せる(vault):
    make_note(vault, "A", "01_x", "sub/deep.pdf", title="深い")

    write_indexes(vault)

    category = (vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md").read_text(
        encoding="utf-8"
    )
    assert (
        "- [深い](<sub/deep.pdf.md>) — 説明"
        "（原本: [deep.pdf](<../../../../20_projects/A/01_x/sub/deep.pdf>)、2026-10-15）"
    ) in category


def test_リンクは特殊文字と空白を含む名前でも壊れない(vault):
    make_note(vault, "A", "01_x", "見積 [A]#1.xlsx")

    write_indexes(vault)

    category = (vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "(<見積 [A]#1.xlsx.md>)" in category
    assert "(<../../../../20_projects/A/01_x/見積 [A]#1.xlsx>)" in category


@pytest.mark.parametrize(
    ("target", "label", "expected"),
    [
        ("a b/c.md", "x", "[x](<a b/c.md>)"),
        ("a>b\nc", "x", "[x](<a%3Eb%0Ac>)"),
        ("a.md", "[A]", r"[\[A\]](<a.md>)"),
        ("a.md", "フォルダ\\", r"[フォルダ\\](<a.md>)"),
        ("a.md", "x\ny", "[x y](<a.md>)"),
    ],
    ids=["空白", "大なりと改行", "ラベルの角括弧", "ラベルの円記号", "ラベルの改行"],
)
def test_linkは_大なりと改行をエンコードしラベルの円記号と角括弧をエスケープし改行を空白にする(
    target, label, expected
):
    assert link(target, label) == expected


def test_write_indexesは案件フォルダがなくても根の索引を書く(tmp_path):
    vault = Vault(tmp_path / "v")
    (vault.base).mkdir(parents=True)

    write_indexes(vault)

    assert (vault.base / "index.md").read_text(encoding="utf-8") == (
        '---\nokf_version: "0.2"\n---\n# vault-kb\n\n'
    )


def test_write_indexesは消えた案件の索引を残さない(vault):
    make_note(vault, "A", "01_x", "f.xlsx")
    write_indexes(vault)
    assert ".notes/20_projects/A/index.md" in (vault.base / "index.md").read_text(encoding="utf-8")
    assert (vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md").exists()

    shutil.rmtree(vault.base / "20_projects" / "A")
    vault.note_path(
        vault.base / "20_projects" / "A" / "01_x" / "f.xlsx"
    ).unlink()  # 片づけが孤児を消した後
    write_indexes(vault)

    assert ".notes/20_projects/A/index.md" not in (vault.base / "index.md").read_text(
        encoding="utf-8"
    )
    assert not (vault.base / "20_projects" / "A").exists()
    assert not (vault.notes_dir / "20_projects" / "A").exists()


def test_append_logは同じ日の見出しの直後に新しいものを挿入し_別の日なら先頭に見出しを足す(vault):
    append_log(vault, ["**Filed** 一.xlsx → A/01_x"], datetime(2026, 10, 31, 9, 0))
    append_log(vault, ["**Filed** 二.xlsx → A/01_x"], datetime(2026, 10, 31, 18, 0))
    append_log(vault, ["**Moved** 三.xlsx"], datetime(2026, 11, 1, 8, 0))

    text = (vault.base / "log.md").read_text(encoding="utf-8")

    assert text == (
        "## 2026-11-01\n\n- **Moved** 三.xlsx\n\n"
        "## 2026-10-31\n\n- **Filed** 二.xlsx → A/01_x\n- **Filed** 一.xlsx → A/01_x\n"
    )


def _write_log(vault, text):
    (vault.base / "log.md").write_text(text, encoding="utf-8")


def test_append_logは90日より古い日付の見出しを行ごと消し_境界の日は残す(vault):
    _write_log(
        vault,
        "## 2026-10-31\n\n- 新\n\n## 2026-08-04\n\n- 境界\n\n## 2026-08-03\n\n- 古1\n- 古2\n",
    )

    append_log(vault, ["**Filed** 今日"], datetime(2026, 11, 2, 9, 0))

    assert (vault.base / "log.md").read_text(encoding="utf-8") == (
        "## 2026-11-02\n\n- **Filed** 今日\n\n## 2026-10-31\n\n- 新\n\n## 2026-08-04\n\n- 境界\n"
    )


def test_append_logは見出しより前の文章を残す(vault):
    _write_log(vault, "メモ\n\n## 2026-01-01\n\n- 古い\n")

    append_log(vault, ["今日"], datetime(2026, 11, 2, 9, 0))

    assert (vault.base / "log.md").read_text(encoding="utf-8") == (
        "## 2026-11-02\n\n- 今日\n\nメモ\n"
    )


def test_append_logは古い日付の見出しの下にある日付でない見出しを残す(vault):
    _write_log(vault, "## 2026-01-01NN- 古いNN## 私のメモNN- 大事N".replace("N", "\n"))

    append_log(vault, ["今日"], datetime(2026, 11, 2, 9, 0))

    assert (vault.base / "log.md").read_text(encoding="utf-8") == (
        "## 2026-11-02NN- 今日NN## 私のメモNN- 大事N".replace("N", "\n")
    )


def test_append_logはすべて古ければ今日の見出しだけになる(vault):
    _write_log(vault, "## 2026-01-01\n\n- 古い\n")

    append_log(vault, ["今日"], datetime(2026, 11, 2, 9, 0))

    assert (vault.base / "log.md").read_text(encoding="utf-8") == "## 2026-11-02\n\n- 今日\n"


def test_dateが文字列でないノートがあっても索引を書け_日付なしとして最後に並ぶ(vault):
    make_note(vault, "A", "01_x", "int.xlsx", title="整数", date=20260101)
    make_note(vault, "A", "01_x", "ok.xlsx", title="正常", date="2026-01-01")

    write_indexes(vault)

    path = vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[2].endswith("、2026-01-01）")
    assert lines[3].endswith("、日付なし）")


@pytest.mark.parametrize(
    "resource",
    [
        "C:\\x.xlsx",
        "C:x.xlsx",
        "/x.xlsx",
        "../x.xlsx",
        "A/../x.xlsx",
        "A\\x.xlsx",
        "..",
        "A//x",
        "",
    ],
    ids=[
        "絶対パス",
        "ドライブ指定",
        "ルート指定",
        "親へ出る",
        "途中の親",
        "円記号",
        "ドット2つ",
        "空の部分",
        "空",
    ],
)
def test_resourceがbaseからの相対パスでないmdはノートとして扱わず_索引も落ちない(vault, resource):
    good = make_note(vault, "A", "01_x", "ok.xlsx")
    make_note(vault, "A", "01_x", "bad.xlsx", resource=resource)

    assert [n.path for n in notes(vault)] == [good]
    write_indexes(vault)
    index = vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md"
    assert "bad" not in index.read_text(encoding="utf-8")


def test_projectsとcategoriesはドットで始まるフォルダを除く(vault):
    put(vault, ".obsidian/x.json")
    put(vault, "20_projects/A/.trash/x.xlsx")
    put(vault, "20_projects/A/01_x/f.xlsx")

    assert projects(vault) == ["A", "knowledge", "shared"]
    assert categories(vault, "A") == ["01_x"]


def test_append_logは一度に渡した複数の行を渡した順で並べる(vault):
    append_log(vault, ["一", "二", "三"], datetime(2026, 10, 31, 9, 0))

    assert (vault.base / "log.md").read_text(encoding="utf-8") == (
        "## 2026-10-31\n\n- 一\n- 二\n- 三\n"
    )


def test_write_noteはNOTE_KEYSにない鍵も残して書き_read_frontmatterで保たれる(vault):
    path = make_note(vault, "A", "01_x", "f.txt", aliases=["x"], cssclasses="wide")
    write_note(path, read_frontmatter(path), "本文")
    meta = read_frontmatter(path)
    assert meta["aliases"] == ["x"]
    assert meta["cssclasses"] == "wide"
    assert meta["resource"] == "20_projects/A/01_x/f.txt"


def test_notesはtypeとresourceだけの利用者のメモをノートと見なさない(vault):
    put(
        vault,
        ".notes/20_projects/A/01_x/会議メモ.md",
        "---\ntype: document\nresource: 議事録.docx\n---\n大事なメモ\n".encode(),
    )

    assert notes(vault) == []


@pytest.mark.parametrize(
    "override",
    [
        {"sha256": "abc"},
        {"sha256": "g" * 64},
        {"size": "1"},
        {"size": None},
        {"mtime_ns": True},
        {"mtime_ns": 1.5},
    ],
    ids=[
        "shaが短い",
        "shaが16進でない",
        "sizeが文字列",
        "sizeがnull",
        "mtime_nsが真偽値",
        "mtime_nsが小数",
    ],
)
def test_notesは指紋の型が違うmdをノートと見なさない(vault, override):
    good = make_note(vault, "A", "01_x", "ok.xlsx")
    make_note(vault, "A", "01_x", "memo.xlsx", **override)

    assert [n.path for n in notes(vault)] == [good]


def test_notesは予約名を大文字小文字の違いでもノートとして扱わない(vault):
    put(
        vault,
        ".notes/20_projects/A/01_x/Index.md",
        b'---\ntype: document\nresource: "a.txt"\n---\n',
    )
    assert notes(vault) == []


def test_write_indexesは中身が同じ索引を書き直さない(vault):
    make_note(vault, "A", "01_x", "a.txt")
    write_indexes(vault)
    indexes = [vault.base / "index.md", *vault.notes_dir.rglob("index.md")]
    assert len(indexes) >= 3
    for path in indexes:
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))

    write_indexes(vault)

    assert {p.stat().st_mtime_ns for p in indexes} == {1_000_000_000}


def test_write_indexesは中身が変われば索引を書き直す(vault):
    make_note(vault, "A", "01_x", "a.txt")
    write_indexes(vault)
    index = vault.notes_dir / "20_projects" / "A" / "01_x" / "index.md"
    os.utime(index, ns=(1_000_000_000, 1_000_000_000))

    make_note(vault, "A", "01_x", "b.txt")
    write_indexes(vault)

    assert "b.txt" in index.read_text(encoding="utf-8")


def test_write_indexesはUTF8で読めない索引も上書きして直す(vault):
    make_note(vault, "A", "01_x", "a.txt")
    write_indexes(vault)
    (vault.base / "index.md").write_bytes("# 壊れた".encode("cp932"))

    write_indexes(vault)

    assert "vault-kb" in (vault.base / "index.md").read_text(encoding="utf-8")
