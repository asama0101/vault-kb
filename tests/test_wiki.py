from datetime import UTC, datetime

import pytest
from helpers import make_note, put
from okf_check import check_bundle
from vault_kb import __version__, bundle, watch, wiki
from vault_kb.lock import acquire
from vault_kb.wiki import (
    FULL_READ_LIMIT,
    READ_LIMIT,
    _replace_moved,
    apply,
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


def test_project_entriesはpoolを渡しても渡さなくても同じで_名前が前方一致する別案件を混ぜない(
    vault,
):
    note(vault, "a.pdf")
    note(vault, "x.pdf", project="A社2", sha="x")  # 案件 A社 と前方一致する別の案件
    note(vault, "s.pdf", project="shared", sha="s")
    pool = wiki._Pool(vault, wiki._entries(vault))

    for name in (P, "A社2", "shared", "なし"):
        assert wiki.project_entries(vault, name, pool) == wiki.project_entries(vault, name)
    assert [e["resource"] for e in wiki.project_entries(vault, P, pool)] == [r("a.pdf")]


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


def query(vault, evidence, scope=None, key="sources"):
    scope = scope if scope is not None else [P]
    meta = {"type": "query", key: evidence, "scope": scope}
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


def test_check_queryは旧い鍵evidenceで保存された回答も同じ判定で読む(vault):
    note(vault, "a.pdf")
    r = f"20_projects/{P}/01_見積/a.pdf"
    ask = {"kind": "query", "file": "q.md"}

    query(vault, [{"resource": r, "sha256": "a" * 64}], key="evidence")
    assert check(vault, ask)["reusable"] is True

    query(vault, [{"resource": r, "sha256": "f" * 64}], key="evidence")
    assert check(vault, ask)["changed"] == [r]


def test_check_queryはsourcesがあればevidenceより優先する(vault):
    note(vault, "a.pdf")
    r = f"20_projects/{P}/01_見積/a.pdf"
    meta = {
        "type": "query",
        "sources": [{"resource": r, "sha256": "a" * 64}],
        "evidence": [{"resource": r, "sha256": "f" * 64}],  # 古い鍵は見ない
        "scope": [P],
        "scope_digest": scope_digest(vault, [P]),
    }
    write_page(vault, "queries/q.md", meta)

    assert check(vault, {"kind": "query", "file": "q.md"})["reusable"] is True


def test_check_queryは同じ範囲に資料が追加されるとscope_changedでreusableでない(vault):
    note(vault, "a.pdf")
    r = f"20_projects/{P}/01_見積/a.pdf"
    query(vault, [{"resource": r, "sha256": "a" * 64}])
    note(vault, "b.pdf", sha="b")

    result = check(vault, {"kind": "query", "file": "q.md"})

    assert result["scope_changed"] is True
    assert result["changed"] == []
    assert result["reusable"] is False


def test_check_queryは原本を同じ範囲で動かしただけなら移動として再利用できる(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    query(vault, [{"resource": r("a.pdf"), "sha256": "a" * 64}])
    move(vault, "a.pdf", "a2.pdf", "a")  # sha256 は同じで、パスだけ変わった

    result = check(vault, {"kind": "query", "file": "q.md"})

    assert result["moved"] == [{"from": r("a.pdf"), "to": r("a2.pdf")}]
    assert result["missing"] == []
    assert (result["changed"], result["scope_changed"]) == ([], False)
    assert result["reusable"] is True


def test_check_queryは移動と同時に中身が変わった資料や消えた資料は移動にしない(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    query(
        vault,
        [
            {"resource": r("a.pdf"), "sha256": "a" * 64},
            {"resource": r("b.pdf"), "sha256": "b" * 64},
        ],
    )
    move(vault, "a.pdf", "a2.pdf", "f")  # 動かしつつ中身も変わった
    (vault.notes_dir / f"{r('b.pdf')}.md").unlink()  # 消えた

    result = check(vault, {"kind": "query", "file": "q.md"})

    assert result["moved"] == []
    assert sorted(result["missing"]) == [r("a.pdf"), r("b.pdf")]
    assert result["reusable"] is False


def test_check_queryは別の案件へ動かした資料を移動と報告するが再利用はしない(vault):
    note(vault, "a.pdf")
    query(vault, [{"resource": r("a.pdf"), "sha256": "a" * 64}])
    (vault.notes_dir / f"{r('a.pdf')}.md").unlink()
    note(vault, "a.pdf", project="別", sha="a")  # 範囲（案件 A社）の外へ

    result = check(vault, {"kind": "query", "file": "q.md"})

    assert result["moved"] == [{"from": r("a.pdf"), "to": r("a.pdf", "別")}]
    assert result["scope_changed"] is True
    assert result["reusable"] is False


def test_check_queryは移動がなければmovedを空で返す(vault):
    note(vault, "a.pdf")
    query(vault, [{"resource": r("a.pdf"), "sha256": "a" * 64}])

    assert check(vault, {"kind": "query", "file": "q.md"})["moved"] == []


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
        ({"kind": "query", "file": "C:x.md"}, "file"),
        ({"kind": "other"}, "kind"),
        ({}, "kind"),
    ],
)
def test_checkは区切り文字を含む名前_型の不正なrequestをValueErrorにする(vault, request_, message):
    with pytest.raises(ValueError, match=message):
        check(vault, request_)


# ---- apply ----

NOW = datetime(2026, 11, 8, 9, 30, 15, tzinfo=UTC)
STAMP = "20261108-093015"
GENERATED = {"by": f"vault-kb/{__version__}", "at": NOW.isoformat()}


def r(name, project=P):
    return f"20_projects/{project}/01_見積/{name}"


def pop(vault, **kw):
    """案件の統合ページを書く op（expected_digest は今の指紋）。"""
    op = {"op": "page", "kind": "project", "name": P, "body": "本文"} | kw
    if "expected_digest" not in op:
        op["expected_digest"] = digest(project_entries(vault, op["name"]))
    return op


def qop(**kw):
    return {
        "op": "query",
        "question": "保守費は？",
        "answer": "月額 20 万円",
        "evidence": [],
        "scope": [P],
        "slug": "保守費",
    } | kw


def run(vault, data_dir, *ops):
    return apply(vault, data_dir, {"ops": list(ops)}, NOW)


def page_of(vault, rel=f"projects/{P}.md"):
    return read_page(vault.wiki_dir / rel)


def move(vault, old, new, sha):
    """原本を改名したときのノートの状態にする（古いノートを消し、新しいノートを置く）。"""
    (vault.notes_dir / f"{r(old)}.md").unlink()
    note(vault, new, sha=sha)


def snapshot(vault):
    """.wiki/ と log.md を除く、vault の全ファイルのバイト列。"""
    return {
        p: p.read_bytes()
        for p in vault.root.rglob("*")
        if p.is_file() and vault.wiki_dir not in p.parents and p.name != "log.md"
    }


def log_text(vault):
    path = vault.root / "log.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def two(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")


def test_apply_pageは統合ページを作りfrontmatterを機械的に付けSynthesizedを書く(vault, data_dir):
    two(vault)
    note(vault, "c.pdf", project="別", sha="c")

    result = run(vault, data_dir, pop(vault, body="## 概要\n要約"))

    assert result == {"written": [f".wiki/projects/{P}.md"], "logged": 1}
    sources = [
        {"resource": r("a.pdf"), "sha256": "a" * 64},
        {"resource": r("b.pdf"), "sha256": "b" * 64},
    ]
    assert page_of(vault) == (
        {
            "type": "synthesis",
            "title": P,
            "description": f"{P}の統合ページ（ノート 2 件）",
            "project": P,
            "generated": GENERATED,
            "sources_digest": digest(sources),
            "sources": sources,
            "unread": [],
        },
        "## 概要\n要約",
    )
    assert f"- 09:30 **Synthesized** {P}（ノート 2 件）\n" in log_text(vault)
    assert check(vault, req())["action"] == "none"
    assert not (data_dir / "reconcile.lock").exists()

    selected = [r("a.pdf"), r("c.pdf", "別")]
    op = {"op": "page", "kind": "theme", "name": "保守", "selected": selected, "body": "b"}
    op["expected_digest"] = check(vault, theme(selected=selected))["digest"]
    assert run(vault, data_dir, op)["written"] == [".wiki/themes/保守.md"]
    meta, _ = page_of(vault, "themes/保守.md")
    assert meta["theme"] == "保守"
    assert (meta["title"], meta["description"]) == (
        "保守",
        "テーマ「保守」の統合ページ（ノート 2 件）",
    )
    assert "project" not in meta
    assert [s["resource"] for s in meta["sources"]] == sorted(selected)
    assert "- 09:30 **Synthesized** テーマ「保守」（ノート 2 件、2 案件）\n" in log_text(vault)


def test_apply_pageはunreadをsourcesから除きunreadに入れる(vault, data_dir):
    two(vault)
    note(vault, "c.pdf", sha="c")

    run(vault, data_dir, pop(vault, unread=[r("c.pdf")]))

    meta, _ = page_of(vault)
    every = {r("a.pdf"), r("b.pdf"), r("c.pdf")}
    assert meta["unread"] == [r("c.pdf")]
    assert {s["resource"] for s in meta["sources"]} == {r("a.pdf"), r("b.pdf")}
    assert {s["resource"] for s in meta["sources"]} | set(meta["unread"]) == every
    assert meta["sources_digest"] == check(vault, req())["digest"]
    assert check(vault, req())["action"] == "update"  # 未読があれば続きから読む
    with pytest.raises(ValueError, match="unread"):
        run(vault, data_dir, pop(vault, unread=[r("none.pdf")]))


def test_apply_pageはexpected_digestが違えば何も書かずValueError(vault, data_dir):
    two(vault)
    op = pop(vault)
    note(vault, "c.pdf", sha="c")  # 確認の後に資料が増えた

    with pytest.raises(ValueError, match="指紋"):
        run(vault, data_dir, op)
    with pytest.raises(ValueError, match="指紋"):
        run(vault, data_dir, pop(vault, expected_digest="x"))

    assert not (vault.wiki_dir / "projects").exists()
    assert log_text(vault) == ""
    assert not (data_dir / "reconcile.lock").exists()


def test_apply_pageのbody省略は移動だけのときだけ通り_既存のunreadを引き継ぐ(vault, data_dir):
    two(vault)
    run(vault, data_dir, pop(vault, body=f"記述（出典: {r('a.pdf')}）", unread=[r("b.pdf")]))
    move(vault, "a.pdf", "a2.pdf", "a")
    op = pop(vault)
    del op["body"]

    assert run(vault, data_dir, op) == {"written": [f".wiki/projects/{P}.md"], "logged": 0}

    meta, body = page_of(vault)
    assert body == f"記述（出典: {r('a2.pdf')}）"
    assert meta["unread"] == [r("b.pdf")]
    assert meta["sources"] == [{"resource": r("a2.pdf"), "sha256": "a" * 64}]

    note(vault, "c.pdf", sha="c")  # 追加がある
    op = pop(vault)
    del op["body"]
    with pytest.raises(ValueError, match="body"):
        run(vault, data_dir, op)

    note(vault, "x.pdf", project="B")
    note(vault, "y.pdf", project="B", sha="b")
    op = pop(vault, name="B")
    del op["body"]
    with pytest.raises(ValueError, match="body"):  # 既存のページがない
        run(vault, data_dir, op)


def test_apply_pageは移動の出典パスを連鎖や接頭辞で壊さず置き換える(vault, data_dir):
    note(vault, "見積.xlsx")
    note(vault, "見積.xlsx.bak", sha="b")
    old, bak = r("見積.xlsx"), r("見積.xlsx.bak")
    body = "x（出典: {0}）\ny（出典: {1}）\nz {0}.\n{0}に記載\n詳細は{0}。\n{0}_old"
    run(vault, data_dir, pop(vault, body=body.format(old, bak)))
    move(vault, "見積.xlsx", "見積v2.xlsx", "a")
    op = pop(vault)
    del op["body"]

    run(vault, data_dir, op)

    new = r("見積v2.xlsx")
    # 日本語に接したパスは置き換え、ASCII の続き（.bak・_old）は別のパスとして残す
    expected = body.format(new, bak).removesuffix(f"{new}_old") + f"{old}_old"
    assert page_of(vault)[1] == expected
    swap = {"x/a.pdf": "x/b.pdf", "x/b.pdf": "x/a.pdf"}
    assert _replace_moved("x/a.pdf と x/b.pdf", swap) == "x/b.pdf と x/a.pdf"


def test_apply_pageはunreadの資料の移動を追加として扱い_bodyを求める(vault, data_dir):
    # unread は sha256 を持たず sources にも入らないので、移動の組にならない
    two(vault)
    run(vault, data_dir, pop(vault, unread=[r("b.pdf")]))
    move(vault, "b.pdf", "b2.pdf", "b")
    op = pop(vault)
    del op["body"]

    with pytest.raises(ValueError, match="body"):
        run(vault, data_dir, op)
    with pytest.raises(ValueError, match="unread"):  # 旧 resource はもう資料にない
        run(vault, data_dir, pop(vault, unread=[r("b.pdf")]))

    run(vault, data_dir, pop(vault, unread=[r("b2.pdf")]))

    assert page_of(vault)[0]["unread"] == [r("b2.pdf")]


def test_apply_pageは上書きの前に旧ページを退避し_取り消し線が減る本文を拒否する(vault, data_dir):
    two(vault)
    run(vault, data_dir, pop(vault, body="~~古い~~（取り消し 2026-11-08: 誤り） 新しい"))
    first = (vault.wiki_dir / f"projects/{P}.md").read_bytes()

    run(vault, data_dir, pop(vault, body="~~古い~~（取り消し 2026-11-08: 誤り） 新しい 追記"))

    assert (vault.wiki_dir / f".bak/project/{P}.md").read_bytes() == first
    second = (vault.wiki_dir / f"projects/{P}.md").read_bytes()
    with pytest.raises(ValueError, match="取り消し線"):
        run(vault, data_dir, pop(vault, body="消した"))
    assert (vault.wiki_dir / f"projects/{P}.md").read_bytes() == second


def test_apply_pageは存在しない案件_ノート1件の案件_themeの未知selectedを拒否する(vault, data_dir):
    note(vault, "a.pdf")
    note(vault, "x.pdf", project="一件")

    with pytest.raises(ValueError, match="案件"):
        run(vault, data_dir, pop(vault, name="なし", expected_digest="x"))
    with pytest.raises(ValueError, match="2 件"):
        run(vault, data_dir, pop(vault, name="一件"))
    op = {
        "op": "page",
        "kind": "theme",
        "name": "保守",
        "selected": [r("a.pdf"), "x/y.pdf"],
        "expected_digest": "x",
        "body": "b",
    }
    with pytest.raises(ValueError, match="selected"):
        run(vault, data_dir, op)
    assert not (vault.wiki_dir / "projects").exists()
    assert not (vault.wiki_dir / "themes").exists()


def test_Synthesizedは本文つきのページだけに書き_移動だけの更新には書かない(vault, data_dir):
    two(vault)
    (vault.root / "log.md").write_text("## 2026-11-07\n\n- **Filed** 旧\n", encoding="utf-8")

    assert run(vault, data_dir, pop(vault))["logged"] == 1

    line = f"- 09:30 **Synthesized** {P}（ノート 2 件）\n"
    after = f"## 2026-11-08\n\n{line}\n## 2026-11-07\n\n- **Filed** 旧\n"
    assert log_text(vault) == after
    move(vault, "a.pdf", "a2.pdf", "a")
    op = pop(vault)
    del op["body"]
    assert run(vault, data_dir, op)["logged"] == 0
    assert log_text(vault) == after


def test_回答の保存はSavedを1行書き_上書きでも書く(vault, data_dir):
    two(vault)

    result = run(vault, data_dir, qop(evidence=[r("a.pdf")]))

    name = f"{STAMP}-保守費.md"
    assert result["logged"] == 1
    assert log_text(vault) == f"## 2026-11-08\n\n- 09:30 **Saved** {name}\n"
    run(vault, data_dir, qop(evidence=[r("a.pdf")], overwrite=name))
    assert log_text(vault).count(f"**Saved** {name}") == 2


def test_lintがtrueのページopはLintedを書き_Synthesizedの前に並ぶ(vault, data_dir):
    two(vault)

    result = run(vault, data_dir, pop(vault, lint=True))

    assert result["logged"] == 2
    log = log_text(vault)
    assert f"- 09:30 **Linted** {P}\n" in log
    assert log.index("**Synthesized**") < log.index("**Linted**")  # 同じ呼び出しの行は渡した順


def test_lintがtrueのテーマページopはテーマ名のLintedを書く(vault, data_dir):
    two(vault)
    selected = [r("a.pdf"), r("b.pdf")]
    op = {"op": "page", "kind": "theme", "name": "保守", "selected": selected, "body": "b"}
    op["expected_digest"] = check(vault, theme(selected=selected))["digest"]

    result = run(vault, data_dir, op | {"lint": True})

    assert result["logged"] == 2
    assert "- 09:30 **Linted** テーマ「保守」\n" in log_text(vault)


def test_lintがtrueでも移動だけの更新にはLintedを書かない(vault, data_dir):
    two(vault)
    run(vault, data_dir, pop(vault, body=f"記述（出典: {r('a.pdf')}）"))
    move(vault, "a.pdf", "a2.pdf", "a")
    op = pop(vault, lint=True)
    del op["body"]

    assert run(vault, data_dir, op)["logged"] == 0


def test_lintがtrueのページopはverifiedを書き_ふつうの更新では書かず_次の更新で外す(
    vault, data_dir
):
    two(vault)

    run(vault, data_dir, pop(vault))
    assert "verified" not in page_of(vault)[0]

    run(vault, data_dir, pop(vault, lint=True))
    assert page_of(vault)[0]["verified"] == GENERATED

    run(vault, data_dir, pop(vault))  # 本文を書き直すと、確認した内容ではなくなる
    assert "verified" not in page_of(vault)[0]


def test_移動だけの更新はverifiedを引き継ぐ(vault, data_dir):
    two(vault)
    run(vault, data_dir, pop(vault, body=f"記述（出典: {r('a.pdf')}）", lint=True))
    move(vault, "a.pdf", "a2.pdf", "a")
    op = pop(vault)
    del op["body"]

    run(vault, data_dir, op)

    assert page_of(vault)[0]["verified"] == GENERATED  # 内容は変わっていない


def test_移動だけの更新は辞書でないverifiedを引き継がない(vault, data_dir):
    two(vault)
    run(vault, data_dir, pop(vault, body=f"記述（出典: {r('a.pdf')}）"))
    meta, body = page_of(vault)
    write_page(vault, f"projects/{P}.md", meta | {"verified": None}, body)  # 壊れた形
    move(vault, "a.pdf", "a2.pdf", "a")
    op = pop(vault)
    del op["body"]

    run(vault, data_dir, op)

    assert "verified" not in page_of(vault)[0]  # null を書き写さない


def test_wiki_index_mdは質問の改行を空白にして1行に保つ(vault, data_dir):
    two(vault)

    run(vault, data_dir, qop(question="保守費は？\n月額で", evidence=[r("a.pdf")]))

    items = [x for x in wiki_index(vault).splitlines() if x.startswith("- ")]
    assert len(items) == 1
    assert items[0].endswith("— 質問「保守費は？ 月額で」への回答（出典 1 件）")


def test_lintが真偽値でなければValueErrorで何も書かない(vault, data_dir):
    two(vault)

    with pytest.raises(ValueError, match="lint"):
        run(vault, data_dir, pop(vault, lint="yes"))
    assert not (vault.wiki_dir / "projects").exists()


def wiki_index(vault):
    return (vault.wiki_dir / "index.md").read_text(encoding="utf-8")


def test_applyは書くたびにwiki直下のindex_mdを作り_ページと回答を説明つきで並べる(vault, data_dir):
    two(vault)

    run(vault, data_dir, pop(vault), qop(evidence=[r("a.pdf")]))

    assert wiki_index(vault) == (
        "# wiki\n\n"
        "## 統合ページ\n\n"
        f"- [{P}](<projects/{P}.md>) — {P}の統合ページ（ノート 2 件）\n\n"
        "## 保存した回答\n\n"
        f"- [保守費は？](<queries/{STAMP}-保守費.md>) — 質問「保守費は？」への回答（出典 1 件）\n"
    )
    assert check_bundle(vault.root) == []  # index.md に frontmatter がない


def test_wiki_index_mdはテーマページも載せ_説明のない旧いページは題だけ並べる(vault, data_dir):
    two(vault)
    write_page(vault, "projects/旧.md", {"type": "synthesis", "project": "旧", "sources": []})
    selected = [r("a.pdf"), r("b.pdf")]
    op = {"op": "page", "kind": "theme", "name": "保守", "selected": selected, "body": "b"}
    op["expected_digest"] = check(vault, theme(selected=selected))["digest"]

    run(vault, data_dir, op)

    text = wiki_index(vault)
    theme_line = "- [保守](<themes/保守.md>) — テーマ「保守」の統合ページ（ノート 2 件）\n"
    assert f"## テーマページ\n\n{theme_line}" in text
    assert "- [旧](<projects/旧.md>)\n" in text  # description がなければ「 — …」を付けない


def test_wiki_index_mdは何も書かなかったapplyでは作らない(vault, data_dir):
    two(vault)

    assert run(vault, data_dir) == {"written": [], "logged": 0}

    assert not (vault.wiki_dir / "index.md").exists()


def test_applyはロックが取れなければbusyを返し何も書かない(vault, data_dir):
    two(vault)
    acquire(data_dir, NOW)

    assert run(vault, data_dir, pop(vault), qop()) == {"busy": True}

    assert not (vault.wiki_dir / "projects").exists()
    assert not (vault.wiki_dir / "queries").exists()
    assert (data_dir / "reconcile.lock").exists()  # 他の実行のロックは解かない


def test_apply_queryはevidenceのsha256とscope_digestをノートから取り_overwriteで上書きする(
    vault, data_dir
):
    two(vault)

    result = run(vault, data_dir, qop(evidence=[r("a.pdf")]))

    name = f"{STAMP}-保守費.md"
    assert result == {"written": [f".wiki/queries/{name}"], "logged": 1}
    meta, body = page_of(vault, f"queries/{name}")
    assert meta == {
        "type": "query",
        "title": "保守費は？",
        "description": "質問「保守費は？」への回答（出典 1 件）",
        "question": "保守費は？",
        "generated": GENERATED,
        "sources": [{"resource": r("a.pdf"), "sha256": "a" * 64}],
        "scope": [P],
        "scope_digest": scope_digest(vault, [P]),
    }
    assert body == "月額 20 万円"
    assert check(vault, {"kind": "query", "file": name})["reusable"] is True

    again = run(vault, data_dir, qop(answer="月額 25 万円", overwrite=name))

    assert again["written"] == [f".wiki/queries/{name}"]
    assert page_of(vault, f"queries/{name}")[1] == "月額 25 万円"
    assert len(list((vault.wiki_dir / "queries").iterdir())) == 1


def test_apply_queryは同名を連番にし_slugが空ならanswerにし_存在しないoverwriteを拒否する(
    vault, data_dir
):
    first = run(vault, data_dir, qop(), qop())["written"]
    third = run(vault, data_dir, qop())["written"]
    empty = run(vault, data_dir, qop(slug=""), qop(slug="///"))["written"]

    base = f".wiki/queries/{STAMP}-保守費"
    assert first + third == [f"{base}.md", f"{base}-2.md", f"{base}-3.md"]
    assert empty == [f".wiki/queries/{STAMP}-answer.md", f".wiki/queries/{STAMP}-answer-2.md"]
    with pytest.raises(ValueError, match="overwrite"):
        run(vault, data_dir, qop(overwrite="none.md"))


def test_apply_queryは存在しないresourceをValueErrorにして何も書かない(vault, data_dir):
    two(vault)

    with pytest.raises(ValueError, match="evidence"):
        run(vault, data_dir, pop(vault), qop(evidence=[r("a.pdf"), "x/none.pdf"]))

    assert not (vault.wiki_dir / "projects").exists()
    assert not (vault.wiki_dir / "queries").exists()
    assert log_text(vault) == ""


def test_applyは不正な名前_slug_overwriteをwiki_dirの外に出さない(vault, data_dir, tmp_path):
    two(vault)
    (vault.wiki_dir / "queries").mkdir(parents=True)
    (vault.wiki_dir / "queries" / "q.md").write_text("x", encoding="utf-8")
    (vault.wiki_dir / "log.md").write_text("x", encoding="utf-8")  # 外を指せば存在はする
    bad = [
        pop(vault, name="../x", expected_digest="x"),
        pop(vault, name="a/b", expected_digest="x"),
        pop(vault, name=str(tmp_path / "x"), expected_digest="x"),
        pop(vault, name="C:x", expected_digest="x"),
        {"op": "page", "kind": "theme", "name": "../t", "selected": [r("a.pdf")]},
        qop(overwrite="../log.md"),
        qop(overwrite="..\\log.md"),
        qop(overwrite="a/q.md"),
        qop(overwrite="C:q.md"),
        qop(overwrite=str(vault.wiki_dir / "queries" / "q.md")),
        qop(overwrite="q.txt"),
    ]
    before = snapshot(vault)
    for op in bad:
        with pytest.raises(ValueError, match="name|overwrite|フォルダ名"):
            run(vault, data_dir, op)
    assert snapshot(vault) == before
    assert (vault.wiki_dir / "log.md").read_text(encoding="utf-8") == "x"

    (written,) = run(vault, data_dir, qop(slug="../../C:\\x/y"))["written"]

    assert written == f".wiki/queries/{STAMP}-Cxy.md"
    assert (vault.root / written).resolve().is_relative_to(vault.wiki_dir.resolve())


def test_applyは長い日本語のslugを40字に切る(vault, data_dir):
    (written,) = run(vault, data_dir, qop(slug="あ" * 50))["written"]

    assert written == f".wiki/queries/{STAMP}-{'あ' * 40}.md"


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        ({}, "ops"),
        ({"ops": "x"}, "ops"),
        ({"ops": [1]}, "ops"),
        ({"ops": [{"op": "other"}]}, "op"),
        ({"ops": [{"op": "page", "kind": "x", "name": P}]}, "kind"),
        ({"ops": [{"op": "page", "kind": "project", "name": 1}]}, "name"),
        ({"ops": [{"op": "page", "kind": "project", "name": P}]}, "expected_digest"),
        ({"ops": [pop(None, expected_digest="x", body=1)]}, "body"),
        ({"ops": [qop(evidence="a")]}, "evidence"),
        ({"ops": [qop(scope=[1])]}, "scope"),
        ({"ops": [qop(question=None)]}, "question"),
    ],
)
def test_applyは型の不正なopをValueErrorにする(vault, data_dir, request_, message):
    two(vault)

    with pytest.raises(ValueError, match=message):
        apply(vault, data_dir, request_, NOW)
    assert not (data_dir / "reconcile.lock").exists()


def test_wikiのページは片づけに数えられない(vault, data_dir):
    two(vault)
    bundle.write_indexes(vault)
    notes = bundle.notes(vault)
    pending = watch.status(vault, data_dir)["pending"]

    def indexes():  # 片づけ側の索引。.wiki/index.md は wiki-apply が作る別の索引
        return {
            p: p.read_bytes()
            for p in vault.root.rglob("index.md")
            if vault.wiki_dir not in p.parents
        }

    before = indexes()

    run(vault, data_dir, pop(vault), qop(evidence=[r("a.pdf")]))
    bundle.write_indexes(vault)

    assert bundle.notes(vault) == notes
    assert watch.status(vault, data_dir)["pending"] == pending
    assert indexes() == before


def test_applyは原本_ノート_indexを書き換えない(vault, data_dir):
    two(vault)
    bundle.write_indexes(vault)
    before = snapshot(vault)

    run(vault, data_dir, pop(vault), qop(evidence=[r("a.pdf")]))

    assert snapshot(vault) == before
    assert (vault.wiki_dir / f"projects/{P}.md").is_file()


def test_apply_pageのbodyがnullなら省略と同じ移動だけの更新でログを足さない(vault, data_dir):
    two(vault)
    run(vault, data_dir, pop(vault, body=f"記述（出典: {r('a.pdf')}）"))
    move(vault, "a.pdf", "a2.pdf", "a")
    op = pop(vault, body=None)

    assert run(vault, data_dir, op) == {"written": [f".wiki/projects/{P}.md"], "logged": 0}
    assert page_of(vault)[1] == f"記述（出典: {r('a2.pdf')}）"


def test_apply_pageのbody省略では消えた未読の資料を引き継がず通る(vault, data_dir):
    two(vault)
    note(vault, "c.pdf", sha="c")
    run(vault, data_dir, pop(vault, body=f"記述（出典: {r('a.pdf')}）", unread=[r("c.pdf")]))
    (vault.notes_dir / f"{r('c.pdf')}.md").unlink()
    op = pop(vault)
    del op["body"]

    run(vault, data_dir, op)

    assert page_of(vault)[0]["unread"] == []


def test_apply_pageの明示したunreadは対象外の資料を厳密に拒否する(vault, data_dir):
    two(vault)

    with pytest.raises(ValueError, match="unread"):
        run(vault, data_dir, pop(vault, unread=[r("gone.pdf")]))


# ---- lint（機械 Lint。AI を使わず、全案件を読み取りだけで検査する） ----


def lint_states(vault):
    return {p["name"]: p["state"] for p in wiki.lint(vault)["projects"]}


def page_for(vault, project, **meta):
    sources = src(vault, project)
    base = {
        "type": "synthesis",
        "project": project,
        "sources": sources,
        "sources_digest": digest(sources),
    }
    write_page(vault, f"projects/{project}.md", base | meta)


def test_lintは案件名なしで全案件の統合ページの状態を返す(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")  # A社: ノート 2 件でページなし
    note(vault, "x.pdf", project="別")  # 別: ノート 1 件でページなし
    note(vault, "x.pdf", project="C社")
    note(vault, "y.pdf", project="C社", sha="b")
    page_for(vault, "C社")  # C社: 最新
    note(vault, "x.pdf", project="D社")
    note(vault, "y.pdf", project="D社", sha="b")
    page_for(vault, "D社")
    note(vault, "z.pdf", project="D社", sha="c")  # D社: ページのあとにノートが増えた

    assert lint_states(vault) == {
        P: "missing",
        "別": "none",
        "C社": "ok",
        "D社": "stale",
        "shared": "none",  # 初期設定で必ずある案件
        "knowledge": "none",
    }
    stale = next(p for p in wiki.lint(vault)["projects"] if p["name"] == "D社")
    assert (stale["added"], stale["changed"], stale["removed"], stale["moved"]) == (1, 0, [], [])
    assert stale["note_count"] == 3


def test_lintは消えた資料を参照切れとして案件の差に返す(vault):
    for name, sha in (("a.pdf", "a"), ("b.pdf", "b"), ("c.pdf", "c")):
        note(vault, name, sha=sha)
    project_page(vault)
    (vault.notes_dir / f"{r('a.pdf')}.md").unlink()

    found = next(p for p in wiki.lint(vault)["projects"] if p["name"] == P)

    assert found["state"] == "stale"
    assert found["removed"] == [r("a.pdf")]


def test_lintは資料が1件以下に減ってもページが消えた資料を指すならstaleにする(vault):
    for name, sha in (("a.pdf", "a"), ("b.pdf", "b"), ("c.pdf", "c")):
        note(vault, name, sha=sha)
    project_page(vault)
    (vault.notes_dir / f"{r('a.pdf')}.md").unlink()
    (vault.notes_dir / f"{r('b.pdf')}.md").unlink()

    found = next(p for p in wiki.lint(vault)["projects"] if p["name"] == P)

    assert found["note_count"] == 1
    assert found["state"] == "stale"  # check は none（2 件未満）だが、参照切れは見せる
    assert found["removed"] == [r("a.pdf"), r("b.pdf")]


def test_lintは移動を差に返し_unreadを持つページはstaleにする(vault):
    two(vault)
    project_page(vault)
    move(vault, "a.pdf", "a2.pdf", "a")
    moved = next(p for p in wiki.lint(vault)["projects"] if p["name"] == P)
    assert moved["moved"] == [{"from": r("a.pdf"), "to": r("a2.pdf")}]
    assert moved["state"] == "stale"

    project_page(vault, unread=[r("b.pdf")], sources=src(vault)[:1])
    assert lint_states(vault)[P] == "stale"


def test_lintはsourcesが壊れた統合ページをbrokenにする(vault):
    two(vault)
    project_page(vault, sources="壊れた")

    assert lint_states(vault)[P] == "broken"


def test_lintはテーマページの参照切れと壊れたsourcesを返す(vault):
    note(vault, "a.pdf")
    write_page(
        vault,
        "themes/保守.md",
        {
            "type": "synthesis",
            "theme": "保守",
            "sources": [e(r("a.pdf"), "a" * 64), e(r("gone.pdf"), "g" * 64)],
        },
    )
    write_page(vault, "themes/壊れ.md", {"type": "synthesis", "theme": "壊れ", "sources": 1})

    themes = {t["name"]: t for t in wiki.lint(vault)["themes"]}

    assert themes["保守"] == {"name": "保守", "broken": False, "missing_sources": [r("gone.pdf")]}
    assert themes["壊れ"] == {"name": "壊れ", "broken": True, "missing_sources": []}


def test_lintはテーマページの資料が移動しただけなら参照切れにしない(vault):
    note(vault, "a.pdf")
    note(vault, "b.pdf", sha="b")
    write_page(
        vault,
        "themes/保守.md",
        {
            "type": "synthesis",
            "theme": "保守",
            "sources": [e(r("a.pdf"), "a" * 64), e(r("gone.pdf"), "g" * 64)],
        },
    )
    move(vault, "a.pdf", "a2.pdf", "a")  # sha256 は同じで、パスだけ変わった

    themes = {t["name"]: t for t in wiki.lint(vault)["themes"]}

    assert themes["保守"]["missing_sources"] == [r("gone.pdf")]  # 移動した a.pdf は含まない


def test_lintは全角半角や大文字小文字だけ違う取引先の表記を束ねて返す(vault):
    note(vault, "a.pdf", client="A社")
    note(vault, "b.pdf", client="Ａ社", sha="b")  # 全角の A
    note(vault, "c.pdf", client="B社", sha="c")
    note(vault, "d.pdf", client=None, sha="d")

    assert wiki.lint(vault)["client_variants"] == [["A社", "Ａ社"]]


def test_lintは資料がなければ既定の案件だけが_noneで_テーマと表記揺れは空(vault):
    result = wiki.lint(vault)

    assert {p["name"]: p["state"] for p in result["projects"]} == {
        "shared": "none",
        "knowledge": "none",
    }
    assert (result["themes"], result["client_variants"]) == ([], [])


def test_lintは統合ページの名前にできない案件フォルダがあっても止まらずinvalidを返す(vault):
    put(vault, "20_projects/index/01_見積/a.pdf")  # check_folder_name が拒む予約語

    states = lint_states(vault)

    assert states["index"] == "invalid"
    assert states["shared"] == "none"


def test_lintは何も書かない(vault):
    two(vault)
    project_page(vault)
    note(vault, "c.pdf", sha="c")
    before = {p: p.read_bytes() for p in vault.root.rglob("*") if p.is_file()}

    wiki.lint(vault)

    assert {p: p.read_bytes() for p in vault.root.rglob("*") if p.is_file()} == before
