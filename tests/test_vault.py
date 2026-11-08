import stat

import pytest
from vault_kb import vault
from vault_kb.vault import (
    ConfigError,
    VaultNotFound,
    category_sort_key,
    check_folder_name,
    init_vault,
    is_ignored,
    is_supported,
    load_vault,
    next_category_name,
    resolve_category,
    save_config,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".a", True),
        ("~$b.docx", True),
        ("Thumbs.db", True),
        ("index.md", True),
        ("LOG.md", True),
        ("memo.md", False),
        ("見積.xlsx", False),
    ],
    ids=["隠し", "Office一時", "OS", "index", "LOG", "通常のmd", "通常"],
)
def test_is_ignoredは隠し_Office一時_OS_予約名を無視する(name, expected):
    assert is_ignored(name) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("見積.XLSX", True),
        ("a.txt", True),
        ("a.md", True),
        ("app.log", True),
        ("my.cfg", True),
        ("nginx.CONF", True),
        ("report.html", True),
        ("report.HTM", True),
        ("index.md", False),
        ("~$a.docx", False),
        ("a.csv", False),
    ],
    ids=[
        "大文字拡張子",
        "txt",
        "md",
        "log",
        "cfg",
        "大文字conf",
        "html",
        "大文字htm",
        "予約名",
        "一時",
        "未対応",
    ],
)
def test_is_supportedは対応拡張子で無視しないものだけ(name, expected):
    assert is_supported(name) is expected


@pytest.mark.parametrize(
    "name",
    ["index", "LOG", "a:b", "", " . "],
    ids=["index", "LOG", "禁止文字", "空", "空白とドット"],
)
def test_check_folder_nameはindexとlogと禁止文字を拒む(name):
    with pytest.raises(ValueError, match="フォルダ名"):
        check_folder_name(name)


def test_check_folder_nameは普通の名前を通す():
    check_folder_name("営業 A")


def test_category_sort_keyは番号_番号なし_99の順():
    names = ["99_その他", "見積", "01_見積", "10_設計"]

    assert sorted(names, key=category_sort_key) == ["01_見積", "10_設計", "見積", "99_その他"]


def test_resolve_categoryは番号なしで一致し複数一致はエラー():
    existing = ["01_見積", "02_設計"]

    assert resolve_category(existing, "見積") == "01_見積"
    assert resolve_category(existing, "01_見積") == "01_見積"
    assert resolve_category(existing, "ない") is None
    with pytest.raises(ValueError, match="複数"):
        resolve_category([*existing, "03_見積"], "見積")


@pytest.mark.parametrize(
    ("existing", "raw", "expected"),
    [
        ([], "見積", "01_見積"),
        (["01_見積", "99_その他"], "設計", "02_設計"),
        (["05_x"], "07_y", "07_y"),
        (["98_x"], "y", ValueError),
    ],
    ids=["空", "99を除く", "番号つきはそのまま", "98超"],
)
def test_next_category_nameは次の番号を付ける(existing, raw, expected):
    if expected is ValueError:
        with pytest.raises(ValueError, match="98"):
            next_category_name(existing, raw)
    else:
        assert next_category_name(existing, raw) == expected


def test_init_vaultは00_inbox_20_projects_80_archive_利用者の領域_30_shared_99_その他を作る(
    tmp_path,
):
    root = tmp_path / "仕事 vault"
    vault = init_vault(root)

    assert vault.root == vault.root == root
    for name in ("00_inbox", "20_projects", "80_archive"):
        assert (root / name).is_dir()
    for name in ("10_daily", "50_prompts", "90_attachments"):
        assert (root / name).is_dir()
    assert (root / "30_shared" / "99_その他").is_dir()
    assert (root / "40_knowledge" / "99_その他").is_dir()
    assert not (root / "vault-kb").exists()
    assert not (root / "共通").exists()
    assert vault.path("30_shared") == root / "30_shared"
    assert vault.rel(root / "30_shared" / "99_その他") == "30_shared/99_その他"


def test_project_foldersは20_projects_80_archiveの順で共有2フォルダを案件shared_knowledgeにする(
    vault,
):
    for rel in ("20_projects/B", "20_projects/A", "80_archive/C", "20_projects/.hidden"):
        vault.path(rel).mkdir(parents=True)

    found = vault.project_folders()

    assert [(name, vault.rel(folder)) for name, folder in found] == [
        ("A", "20_projects/A"),
        ("B", "20_projects/B"),
        ("C", "80_archive/C"),
        ("shared", "30_shared"),
        ("knowledge", "40_knowledge"),
    ]


def test_load_vaultは未設定ならConfigError_フォルダがなければVaultNotFound(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(ConfigError, match="未設定") as unset:
        load_vault(data)
    assert not isinstance(unset.value, VaultNotFound)

    save_config(data, tmp_path / "ない")
    with pytest.raises(VaultNotFound):
        load_vault(data)

    root = tmp_path / "v"
    root.mkdir()
    save_config(data, root)
    assert load_vault(data).root == root


def test_load_vaultはvaultが空文字の設定を壊れた設定として扱う(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text('{"vault": ""}', encoding="utf-8")

    with pytest.raises(ConfigError, match="壊れています"):
        load_vault(data)  # Path("") は現在のフォルダを指してしまう


def test_save_configは書き込みが途中で失敗しても前の設定を壊さない(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    save_config(data, old)

    def interrupted(src, dst):
        raise OSError("置き換えの直前に中断")

    monkeypatch.setattr(vault.os, "replace", interrupted)
    with pytest.raises(OSError, match="中断"):
        save_config(data, new)
    monkeypatch.undo()

    assert load_vault(data).root == old
    assert list(data.iterdir()) == [data / "config.json"]  # 一時ファイルを残さない


def test_hide_notes_dirはnotes_dirを作り隠し属性を付け_2回目も壊さない(vault):
    vault.hide_notes_dir()
    vault.hide_notes_dir()

    assert vault.notes_dir == vault.root / ".notes"
    assert vault.notes_dir.is_dir()
    assert vault.notes_dir.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN
    assert not vault.root.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN


def test_hide_wiki_dirはwiki_dirを作り隠し属性を付け_2回目も壊さない(vault):
    vault.hide_wiki_dir()
    vault.hide_wiki_dir()

    assert vault.wiki_dir == vault.root / ".wiki"
    assert vault.wiki_dir.is_dir()
    assert vault.wiki_dir.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN
    assert not vault.root.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN


def test_note_pathは原本と同じ相対パスにmdを足したnotes_dirの下を返す(vault):
    original = vault.root / "20_projects" / "A社" / "01_見積" / "sub" / "見積.xlsx"

    assert vault.note_path(original) == (
        vault.notes_dir / "20_projects" / "A社" / "01_見積" / "sub" / "見積.xlsx.md"
    )


@pytest.mark.parametrize("name", ["CON", "aux", "Nul.txt", "COM1", "lpt9"])
def test_check_folder_nameはWindowsの予約名を拒む(name):
    with pytest.raises(ValueError, match="予約名"):
        check_folder_name(name)
