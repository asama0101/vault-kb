"""OKF v0.2 に沿った書き方（#5）。resource は先頭 `/` の束相対で書き、読むときに外す。"""

import json

from helpers import make_note, put
from vault_kb import wiki
from vault_kb.bundle import is_note, note_text, notes, read_frontmatter, write_note

REL = "20_projects/A社/01_見積/見積書.pdf"


def raw_of(vault, rel=REL):
    return (vault.notes_dir / f"{rel}.md").read_text(encoding="utf-8")


def test_note_textはresourceを先頭スラッシュの束相対で書く():
    meta = {"type": "document", "resource": REL, "sha256": "0" * 64}

    text = note_text(meta, "本文")

    assert f'resource: "/{REL}"\n' in text


def test_note_textはsourcesの各resourceも先頭スラッシュで書く():
    items = [{"resource": REL, "sha256": "a" * 64}]

    text = note_text({"type": "synthesis", "sources": items}, "本文")

    assert text.count(f'"resource": "/{REL}"') == 1


def test_note_textは書いたmetaを書き換えず_すでに先頭スラッシュなら重ねない():
    meta = {"type": "document", "resource": f"/{REL}", "sources": [{"resource": REL}]}

    text = note_text(meta, "本文")

    assert f'resource: "/{REL}"\n' in text
    assert text.count(f'"/{REL}"') == 2  # resource と sources の 1 件目
    assert "//" not in text
    assert meta["sources"] == [{"resource": REL}]  # 呼び出し元の辞書は変えない


def test_read_frontmatterは先頭スラッシュを外し_書いた値がそのまま戻る(tmp_path):
    meta = {
        "type": "synthesis",
        "resource": REL,
        "sources": [{"resource": REL, "sha256": "a" * 64}],
    }
    path = tmp_path / "p.md"

    write_note(path, meta, "本文")

    assert f'"/{REL}"' in path.read_text(encoding="utf-8")
    assert read_frontmatter(path) == meta


def test_read_frontmatterは先頭スラッシュのない旧い形もそのまま読む(tmp_path):
    path = tmp_path / "old.md"
    path.write_text(
        f'---\ntype: "document"\nresource: "{REL}"\n'
        f'sources: [{{"resource": "{REL}"}}]\n---\n\n本文\n',
        encoding="utf-8",
    )

    assert read_frontmatter(path) == {
        "type": "document",
        "resource": REL,
        "sources": [{"resource": REL}],
    }


def test_read_frontmatterは先頭が2つのスラッシュやスラッシュだけの値を外さない(tmp_path):
    path = tmp_path / "odd.md"
    path.write_text('---\nresource: "//x"\nsources: [{"resource": "/"}]\n---\n', encoding="utf-8")

    meta = read_frontmatter(path)

    assert meta["resource"] == "//x"
    assert meta["sources"] == [{"resource": "/"}]


def test_先頭スラッシュで書いたノートも旧い形のノートもノートと認識される(vault):
    put(vault, REL)
    make_note(vault, "A社", "01_見積", "見積書.pdf")  # write_note 経由（先頭スラッシュ）
    assert f'resource: "/{REL}"' in raw_of(vault)

    assert is_note(read_frontmatter(vault.notes_dir / f"{REL}.md"))

    old = vault.notes_dir / f"{REL}.md"  # 同じノートを旧い形（先頭スラッシュなし）に書き直す
    old.write_text(raw_of(vault).replace(f'"/{REL}"', f'"{REL}"'), encoding="utf-8")
    assert f'resource: "{REL}"' in raw_of(vault)
    assert is_note(read_frontmatter(old))


def test_先頭スラッシュ1つのresourceはvault直下からの束相対として読む(vault):
    make_note(vault, "A", "01_x", "ok.xlsx", resource="/x.xlsx")  # 旧い検査では「ルート指定」で不可

    found = notes(vault)

    assert [n.meta["resource"] for n in found] == ["x.xlsx"]
    assert found[0].original == vault.root / "x.xlsx"


def test_旧い形_先頭スラッシュなし_の統合ページも指紋が一致すれば最新とみなす(vault):
    for name, sha in (("a.pdf", "a"), ("b.pdf", "b")):
        make_note(vault, "A社", "01_見積", name, sha256=sha * 64)
        put(vault, f"20_projects/A社/01_見積/{name}")
    sources = [
        {"resource": e["resource"], "sha256": e["sha256"]}
        for e in wiki.project_entries(vault, "A社")
    ]
    page = vault.wiki_dir / "projects" / "A社.md"
    page.parent.mkdir(parents=True)
    page.write_text(  # 2.2.0 までの書き方: resource に先頭スラッシュがない、generated がない
        '---\ntype: "synthesis"\nproject: "A社"\n'
        f"sources_digest: {json.dumps(wiki.digest(sources))}\n"
        f"sources: {json.dumps(sources, ensure_ascii=False)}\nunread: []\n---\n\n本文\n",
        encoding="utf-8",
    )

    checked = wiki.check(vault, {"kind": "project", "name": "A社"})

    assert checked["action"] == "none"
    assert checked["diff"] == {"added": 0, "changed": 0, "moved": [], "removed": []}
