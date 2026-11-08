from vault_kb.wiki import classify, digest


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
