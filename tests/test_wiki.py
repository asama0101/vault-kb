from datetime import UTC, datetime

import pytest
from helpers import make_note, put
from vault_kb import bundle, watch
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
            "project": P,
            "timestamp": NOW.isoformat(),
            "sources_digest": digest(sources),
            "sources": sources,
            "unread": [],
        },
        "## 概要\n要約",
    )
    assert f"- **Synthesized** {P}（ノート 2 件）\n" in log_text(vault)
    assert check(vault, req())["action"] == "none"
    assert not (data_dir / "reconcile.lock").exists()

    selected = [r("a.pdf"), r("c.pdf", "別")]
    op = {"op": "page", "kind": "theme", "name": "保守", "selected": selected, "body": "b"}
    op["expected_digest"] = check(vault, theme(selected=selected))["digest"]
    assert run(vault, data_dir, op)["written"] == [".wiki/themes/保守.md"]
    meta, _ = page_of(vault, "themes/保守.md")
    assert meta["theme"] == "保守"
    assert "project" not in meta
    assert [s["resource"] for s in meta["sources"]] == sorted(selected)
    assert "- **Synthesized** テーマ「保守」（ノート 2 件、2 案件）\n" in log_text(vault)


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
    run(vault, data_dir, pop(vault, body=f"x（出典: {old}）\ny（出典: {bak}）\nz {old}."))
    move(vault, "見積.xlsx", "見積v2.xlsx", "a")
    op = pop(vault)
    del op["body"]

    run(vault, data_dir, op)

    new = r("見積v2.xlsx")
    assert page_of(vault)[1] == f"x（出典: {new}）\ny（出典: {bak}）\nz {new}."
    swap = {"x/a.pdf": "x/b.pdf", "x/b.pdf": "x/a.pdf"}
    assert _replace_moved("x/a.pdf と x/b.pdf", swap) == "x/b.pdf と x/a.pdf"


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


def test_Synthesizedは本文つきのページだけに書き_回答の保存と移動だけの更新には書かない(
    vault, data_dir
):
    two(vault)
    (vault.root / "log.md").write_text("## 2026-11-07\n\n- **Filed** 旧\n", encoding="utf-8")

    assert run(vault, data_dir, pop(vault), qop())["logged"] == 1

    line = f"- **Synthesized** {P}（ノート 2 件）\n"
    after = f"## 2026-11-08\n\n{line}\n## 2026-11-07\n\n- **Filed** 旧\n"
    assert log_text(vault) == after
    move(vault, "a.pdf", "a2.pdf", "a")
    op = pop(vault)
    del op["body"]
    assert run(vault, data_dir, op)["logged"] == 0
    assert run(vault, data_dir, qop())["logged"] == 0
    assert log_text(vault) == after


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
    assert result == {"written": [f".wiki/queries/{name}"], "logged": 0}
    meta, body = page_of(vault, f"queries/{name}")
    assert meta == {
        "type": "query",
        "question": "保守費は？",
        "timestamp": NOW.isoformat(),
        "evidence": [{"resource": r("a.pdf"), "sha256": "a" * 64}],
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
    indexes = {p: p.read_bytes() for p in vault.root.rglob("index.md")}

    run(vault, data_dir, pop(vault), qop(evidence=[r("a.pdf")]))
    bundle.write_indexes(vault)

    assert bundle.notes(vault) == notes
    assert watch.status(vault, data_dir)["pending"] == pending
    assert {p: p.read_bytes() for p in vault.root.rglob("index.md")} == indexes


def test_applyは原本_ノート_indexを書き換えない(vault, data_dir):
    two(vault)
    bundle.write_indexes(vault)
    before = snapshot(vault)

    run(vault, data_dir, pop(vault), qop(evidence=[r("a.pdf")]))

    assert snapshot(vault) == before
    assert (vault.wiki_dir / f"projects/{P}.md").is_file()
