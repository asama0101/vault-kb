import pytest
from vault_kb.vault import Vault, init_vault


@pytest.fixture
def vault(tmp_path) -> Vault:
    # 空白・日本語を含むパスでも動くことを常に確かめる
    return init_vault(tmp_path / "仕事 vault")


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data dir"
    path.mkdir()
    return path
