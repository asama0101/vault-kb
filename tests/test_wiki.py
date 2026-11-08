import pytest
from helpers import make_note, put
from vault_kb import bundle
from vault_kb.wiki import (
    FULL_READ_LIMIT,
    READ_LIMIT,
    check,
    classify,
    digest,
    project_entries,
    read_page,
    scope_digest,
)


def e(resource: str, sha256: str) -> dict[str, str]:
    return {"resource": resource, "sha256": sha256}


def test_digestは順序に依存せず_sha256が変わると変わる():
    a, b = e("a", "h1"), e("b", "h2")

    assert digest([a, b]) == digest([b, a])
    assert digest([a, b]) != digest([a, e("b", "h3")])


def test_classifyは追加_変更_削除_移動を分ける():
    sources = [e("a", "h1"), e("b", "h2"), e("c", "h3")]
    current = [e("a", "h1x"), e("b2", "h2"), e("d", "h4")]

    assert classify(sources, current) == {
        "added": [e("d", "h4")],
        "changed": [e("a", "h1x")],
        "removed": ["c"],
        "moved": [{"from": "b", "to": "b2"}],
    }


def test_classifyは移動を削除と追加に分けない():
    result = classify([e("old", "h")], [e("new", "h")])

    assert result["moved"] == [{"from": "old", "to": "new"}]
    assert result["removed"] == []
    assert result["added"] == []


def test_classifyは複製を移動にしない():
    result = classify([e("a", "h")], [e("a", "h"), e("z", "h")])

    assert result["added"] == [e("z", "h")]
    assert result["moved"] == []


def test_classifyは変更と同時の追加を移動にしない():
    result = classify([e("a", "h1")], [e("a", "h1x"), e("z", "h1")])

    assert result["changed"] == [e("a", "h1x")]
    assert result["added"] == [e("z", "h1")]
    assert result["moved"] == []


def test_classifyは同じsha256が複数でも動いていない資料を移動にしない():
    result = classify([e("a1", "h"), e("a2", "h")], [e("a2", "h"), e("a3", "h")])

    assert result["moved"] == [{"from": "a1", "to": "a3"}]
    assert result["added"] == []
    assert result["removed"] == []
    assert result["changed"] == []


def test_classifyはsourcesが空なら全部added():
    current = [e("a", "h1"), e("b", "h2")]

    result = classify([], current)

    assert result == {"added": current, "changed": [], "removed": [], "moved": []}


# ---- check ----

P = "A社"


def note(vault, name, project=P, sha="a", **meta):
    """原本ありのノートを置く。sha は 1 文字（64 桁に伸ばす）。"""
    make_note(vault, project, "01_見積", name, sha256=sha * 64, **meta)
    put(vault, f"20_projects/{project}/01_見積/{name}")


def write_page(vault, rel, meta, body="本文"):
    path = vault.wiki_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bundle.note_text(meta, body), encoding="utf-8")


def src(vault, project=P):
    return [
        {"resource": x["resource"], "sha256": x["sha256"]} for x in project_entries(vault, project)
    ]


def project_page(vault, **meta):
    sources = src(vault)
    base = {
        "type": "synthesis",
        "project": P,
        "sources": sources,
        "sources_digest": digest(sources),
    }
    write_page(vault, f"projects/{P}.md", base | meta)


def req(**kw):
    return {"kind": "project", "name": P} | kw


def test_read_pageはCRLFと本文中のハイフン3つに耐える(tmp_path):
    path = tmp_path / "p.md"
    path.write_bytes('---\r\ntype: "synthesis"\r\n---\r\n\r\n上\r\n---\r\n下\r\n'.encode())

    assert read_page(path) == ({"type": "synthesis"}, "上\n---\n下")
    assert read_page(tmp_path / "none.md") is None


def test_project_entriesは同名案件の20_projects側だけを日付の新しい順で返す(vault):
    note(vault, "old.pdf", date="2026-01-01")
    note(vault, "new.pdf", date="2026-09-01")
    note(vault, "nodate.pdf", date=None)
    note(vault, "other.pdf", project="別")
    make_note(vault, P, "01_見積", "noorig.pdf")  # 原本なし
    put(vault, f"80_archive/{P}/01_見積/arch.pdf")
    make_note(vault, P, "01_見積", "arch.pdf", resource=f"80_archive/{P}/01_見積/arch.pdf")

    names = [e["resource"].rsplit("/", 1)[1] for e in project_entries(vault, P)]

    assert names == ["new.pdf", "old.pdf", "nodate.pdf"]
    assert project_entries(vault, "なし") == []


def test_checkはノート1件ならskip_2件ならcreate(vault):
    note(vault, "a.pdf")

    assert check(vault, req())["action"] == "skip"

    note(vault, "b.pdf", sha="b")
    result = check(vault, req())

    assert result["action"] == "create"
    assert result["exists"] is False
    assert result["note_count"] == 2
    assert len(result["read_list"]) == 2


def test_checkは指紋が同じでunreadなしならnone_違えばupdateでdiffを返す(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    project_page(vault)

    same = check(vault, req())
    assert same["action"] == "none"
    assert same["read_list"] == []

    note(vault, "c.pdf", sha="c")
    note(vault, "a.pdf", sha="d")
    changed = check(vault, req())

    assert changed["action"] == "update"
    assert changed["diff"] == {"added": 1, "changed": 1, "moved": [], "removed": []}
    assert {e["resource"].rsplit("/", 1)[1] for e in changed["read_list"]} == {"a.pdf", "c.pdf"}
    assert changed["digest"] != same["digest"]


def test_checkはunreadがあれば指紋が同じでもupdateになる(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    project_page(vault, unread=["20_projects/A社/01_見積/z.pdf"])

    result = check(vault, req())

    assert result["action"] == "update"
    assert result["unread"] == ["20_projects/A社/01_見積/z.pdf"]


def test_checkは移動だけならdiffのmovedに入り_指紋は変わる(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    project_page(vault)
    old = check(vault, req())["digest"]
    (vault.notes_dir / f"20_projects/{P}/01_見積/a.pdf.md").unlink()
    note(vault, "a2.pdf")

    result = check(vault, req())

    assert result["diff"]["moved"] == [
        {"from": f"20_projects/{P}/01_見積/a.pdf", "to": f"20_projects/{P}/01_見積/a2.pdf"}
    ]
    assert result["diff"]["added"] == 0
    assert result["read_list"] == []
    assert result["digest"] != old
    assert result["action"] == "update"


def test_checkはreadable_falseのノートもsourcesの対象にする(vault):
    note(vault, "a.pdf", readable=False)
    note(vault, "b.pdf", sha="b")

    assert check(vault, req())["note_count"] == 2


def test_checkのread_listは上限で切り_超えた分をdeferredに返す(vault):
    for i in range(READ_LIMIT + 1):
        note(vault, f"n{i:02d}.pdf", sha="a", date=f"2026-01-{i + 1:02d}")
    project_page(vault, sources=[])

    result = check(vault, req())

    assert len(result["read_list"]) == READ_LIMIT
    assert result["deferred"] == [f"20_projects/{P}/01_見積/n00.pdf"]  # 最も古い
    assert result["read_list"][0]["resource"].endswith("n30.pdf")
    full = check(vault, req(full=True))
    assert len(full["read_list"]) == READ_LIMIT + 1 <= FULL_READ_LIMIT
    assert full["deferred"] == []


@pytest.mark.parametrize("sources", ["文字列", [1, 2], [{"resource": "x"}], None])
def test_checkは壊れたページをbrokenにしsources空として全部addedにする(vault, sources):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    meta = {"type": "synthesis", "project": P, "sources_digest": "x"}
    if sources is not None:
        meta["sources"] = sources
    write_page(vault, f"projects/{P}.md", meta)

    result = check(vault, req())

    assert result["broken"] is True
    assert result["diff"]["added"] == 2
    assert result["action"] == "update"


def theme(**kw):
    return {"kind": "theme", "name": "保守", "selected": []} | kw


def test_check_themeは選定から外れた存在する資料をdeselectedにしremovedにしない(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    a, b = (f"20_projects/{P}/01_見積/{n}.pdf" for n in "ab")
    sources = [{"resource": a, "sha256": "a" * 64}, {"resource": b, "sha256": "b" * 64}]
    write_page(
        vault,
        "themes/保守.md",
        {"type": "synthesis", "theme": "保守", "sources": sources, "sources_digest": "x"},
    )

    result = check(vault, theme(selected=[a]))

    assert result["deselected"] == [b]
    assert result["diff"]["removed"] == []
    assert result["note_count"] == 1
    assert result["digest"] == digest([sources[0]])
    assert check(vault, theme())["action"] == "skip"


def test_check_themeはselectedの存在しないresourceをunknownに返す(vault):
    note(vault, "a.pdf")

    result = check(vault, theme(selected=[f"20_projects/{P}/01_見積/a.pdf", "x/y.pdf"]))

    assert result["unknown"] == ["x/y.pdf"]
    assert result["action"] == "create"


def query(vault, evidence, scope=None):
    scope = scope if scope is not None else [P]
    meta = {"type": "query", "evidence": evidence, "scope": scope}
    write_page(vault, "queries/q.md", meta | {"scope_digest": scope_digest(vault, scope)})


def test_check_queryはevidenceのsha256が違うとchanged_空ならreusableでない(vault):
    note(vault, "a.pdf")
    r = f"20_projects/{P}/01_見積/a.pdf"
    ask = {"kind": "query", "file": "q.md"}
    query(vault, [{"resource": r, "sha256": "a" * 64}])

    assert check(vault, ask)["reusable"] is True

    query(vault, [{"resource": r, "sha256": "f" * 64}, {"resource": "gone", "sha256": "1"}])
    bad = check(vault, ask)
    assert bad["changed"] == [r]
    assert bad["missing"] == ["gone"]
    assert bad["reusable"] is False

    query(vault, [])
    empty = check(vault, ask)
    assert empty["empty"] is True
    assert empty["reusable"] is False

    query(vault, "壊れ")
    assert check(vault, ask)["empty"] is True
    assert check(vault, {"kind": "query", "file": "none.md"})["exists"] is False


def test_check_queryは同じ範囲に資料が追加されるとscope_changedでreusableでない(vault):
    note(vault, "a.pdf")
    r = f"20_projects/{P}/01_見積/a.pdf"
    query(vault, [{"resource": r, "sha256": "a" * 64}])
    note(vault, "b.pdf", sha="b")

    result = check(vault, {"kind": "query", "file": "q.md"})

    assert result["scope_changed"] is True
    assert result["changed"] == []
    assert result["reusable"] is False


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        ({"kind": "project", "name": "../x"}, "フォルダ名"),
        ({"kind": "project", "name": "a/b"}, "フォルダ名"),
        ({"kind": "project", "name": 1}, "name"),
        ({"kind": "project", "name": P, "full": "yes"}, "full"),
        ({"kind": "theme", "name": "t", "selected": "a"}, "selected"),
        ({"kind": "query", "file": "../a.md"}, "file"),
        ({"kind": "query", "file": "a\\b.md"}, "file"),
        ({"kind": "query", "file": "a.txt"}, "file"),
        ({"kind": "other"}, "kind"),
        ({}, "kind"),
    ],
)
def test_checkは区切り文字を含む名前_型の不正なrequestをValueErrorにする(vault, request_, message):
    with pytest.raises(ValueError, match=message):
        check(vault, request_)
