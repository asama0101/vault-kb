import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "vault-kb"
RUN = PLUGIN / "run.py"


def run(*argv, env_home=None, cwd=None, utf8_mode=True):
    env = {k: v for k, v in os.environ.items() if k not in ("VAULT_KB_HOME", "PYTHONUTF8")}
    if utf8_mode:
        env["PYTHONUTF8"] = "1"
    if env_home is not None:
        env["VAULT_KB_HOME"] = str(env_home)
    return subprocess.run(
        [sys.executable, str(RUN), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        check=False,
    )


def make_light_env(data: Path) -> None:
    """依存を入れない最小の専用環境（委譲の経路だけを確かめる）。"""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(data / "venv")], check=True)
    marker = hashlib.sha256((PLUGIN / "requirements.txt").read_bytes().replace(b"\r\n", b"\n"))
    (data / "venv-requirements.sha256").write_text(marker.hexdigest(), encoding="utf-8")


def test_置き場が決まらないとき終了コード2で_data_の付け方を案内する():
    result = run("doctor")

    assert result.returncode == 2
    assert "--data" in result.stderr
    assert "VAULT_KB_HOME" in result.stderr


def test_専用環境がないとき委譲するコマンドは終了コード3でsetupを案内する(tmp_path):
    data = tmp_path / "置き場 data"

    result = run("status", "--data", str(data))

    assert result.returncode == 3
    assert "setup" in result.stderr
    assert str(data) in result.stderr
    assert sys.executable in result.stderr


def test_notifyは専用環境がないとき_initを案内して成功する(tmp_path):
    result = run("--data", str(tmp_path / "data"), "notify")

    assert result.returncode == 0
    assert "/vault-kb:init" in result.stdout
    assert result.stderr == ""


def test_doctorは専用環境がないとき終了コード3で診断を出す(tmp_path):
    result = run("--data", str(tmp_path / "data"), "doctor")

    assert result.returncode == 3
    assert "専用環境" in result.stdout
    assert "NG" in result.stdout


def test_UTF8モードでなくても標準出力と標準エラーはUTF8で出る(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("VAULT_KB_HOME", "PYTHONUTF8")}
    env["PYTHONIOENCODING"] = "cp932"  # 日本語 Windows のコンソール相当を強制する

    doctor = subprocess.run(
        [sys.executable, str(RUN), "--data", str(tmp_path / "data"), "doctor"],
        capture_output=True,
        env=env,
        check=False,
    )
    scan = subprocess.run(
        [sys.executable, str(RUN), "--data", str(tmp_path / "data"), "status"],
        capture_output=True,
        env=env,
        check=False,
    )

    assert "専用環境が未作成です" in doctor.stdout.decode("utf-8")
    assert "専用環境が使えません" in scan.stderr.decode("utf-8")


def test_yesなしのsetupは作成先と容量を表示して終了コード3で止まり何も作らない(tmp_path):
    data = tmp_path / "data"

    result = run("--data", str(data), "setup")

    assert result.returncode == 3
    assert str(data / "venv") in result.stdout
    assert "300 MB" in result.stdout
    assert "--yes" in result.stdout
    assert not data.exists()


def test_環境変数が_data_より優先される(tmp_path):
    result = run("--data", str(tmp_path / "ignored"), "setup", env_home=tmp_path / "env-home")

    assert str(tmp_path / "env-home" / "venv") in result.stdout
    assert not (tmp_path / "ignored").exists()


def test_requirementsとずれた専用環境は終了コード3で止まる(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)
    (data / "venv-requirements.sha256").write_text("0" * 64, encoding="utf-8")

    result = run("status", "--data", str(data))

    assert result.returncode == 3
    assert "ずれ" in result.stderr


def test_委譲したコマンドの結果と終了コードがそのまま返る(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)
    vault_dir = tmp_path / "仕事 vault"
    vault_dir.mkdir()

    configured = run("configure", "--vault", str(vault_dir), "--data", str(data))
    notified = run("notify", "--data", str(data))
    failed = run("nosuch", "--data", str(data))

    assert configured.returncode == 0, configured.stderr
    assert json.loads(configured.stdout) == {"vault": str(vault_dir)}
    assert notified.returncode == 0
    assert notified.stderr == ""
    assert "未設定" not in notified.stdout  # 見張りの案内は claude の有無で変わる
    assert failed.returncode == 2
    assert "invalid choice" in failed.stderr


def test_カレントにある同名モジュールを本体より先に読み込まない(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)
    project = tmp_path / "他人のプロジェクト"
    project.mkdir()
    marker = tmp_path / "shadow-ran"
    (project / "json.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\nraise SystemExit(9)\n",
        encoding="utf-8",
    )

    result = run("status", "--data", str(data), cwd=project)

    assert result.returncode == 2, result.stderr
    assert not marker.exists()


def test_doctorは環境が整っているとき終了コード0になる(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)

    result = run("doctor", "--data", str(data))

    assert result.returncode == 0, result.stdout
    assert "NG" not in result.stdout


@pytest.mark.slow
def test_setupを承認すると専用環境ができて委譲したstatusが動く(tmp_path):
    data = tmp_path / "data"
    vault_dir = tmp_path / "仕事 vault"
    vault_dir.mkdir()

    setup = run("setup", "--yes", "--data", str(data))
    run("configure", "--vault", str(vault_dir), "--data", str(data))
    (vault_dir / "00_inbox" / "memo.txt").write_text(
        "第3四半期の販売計画と予算の見直しについて、各部門の数値を確認する", encoding="utf-8"
    )
    status = run("status", "--data", str(data))
    doctor = run("doctor", "--data", str(data))

    assert setup.returncode == 0, setup.stderr
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["inbox"] == 1
    assert doctor.returncode == 0, doctor.stdout
