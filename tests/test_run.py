import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "vault-kb"
RUN = PLUGIN / "run.py"
# 開発用 venv の外の Python。固定版のパッケージが入っていない（= システムの Python が空の状態）
BASE_PYTHON = sys._base_executable
UNREACHABLE_PROXY = "http://127.0.0.1:9"  # ディスカード用のポート。接続はすぐ拒否される


def run(
    *argv,
    env_home=None,
    cwd=None,
    utf8_mode=True,
    python=None,
    env_extra=None,
    flags=None,
    offline=True,
):
    """run.py を実行する。python を省くと、ライブラリの入っていない Python（システムが空の状態）。

    flags の既定は、python を省いたときは -S（site を読まない。グローバルに入っている
    ライブラリも見えない）、python を渡したときは -s（ユーザー領域のライブラリを見ない）。
    offline の既定は、届かないプロキシを指定して、実ネットワークに出ないようにする
    （実インストールを行う slow テストだけ False にする）。
    """
    if python is None:
        python, flags = BASE_PYTHON, ("-S",) if flags is None else flags
    elif flags is None:
        flags = ("-s",)
    env = {k: v for k, v in os.environ.items() if k not in ("VAULT_KB_HOME", "PYTHONUTF8")}
    if utf8_mode:
        env["PYTHONUTF8"] = "1"
    if env_home is not None:
        env["VAULT_KB_HOME"] = str(env_home)
    if offline:
        env.update(HTTPS_PROXY=UNREACHABLE_PROXY, HTTP_PROXY=UNREACHABLE_PROXY)
    env.update(env_extra or {})
    return subprocess.run(
        [python, *flags, str(RUN), *argv],
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


def empty_venv_python(directory: Path) -> str:
    """ライブラリの入っていない仮想環境の Python（どの環境で動かしても、空の状態）。"""
    directory.mkdir(exist_ok=True)
    make_light_env(directory)
    return str(directory / "venv" / "Scripts" / "python.exe")


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
    assert BASE_PYTHON in result.stderr


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


def configure_vault(data: Path, vault_dir: Path) -> None:
    vault_dir.mkdir(exist_ok=True)
    (data / "config.json").write_text(json.dumps({"vault": str(vault_dir)}), encoding="utf-8")


def test_UTF8モードでなくても標準出力と標準エラーはUTF8で出る(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("VAULT_KB_HOME", "PYTHONUTF8")}
    env["PYTHONIOENCODING"] = "cp932"  # 日本語 Windows のコンソール相当を強制する

    doctor = subprocess.run(
        [BASE_PYTHON, "-S", str(RUN), "--data", str(tmp_path / "data"), "doctor"],
        capture_output=True,
        env=env,
        check=False,
    )
    scan = subprocess.run(
        [BASE_PYTHON, "-S", str(RUN), "--data", str(tmp_path / "data"), "status"],
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
    configure_vault(data, tmp_path / "vault")

    result = run("doctor", "--data", str(data))

    assert result.returncode == 0, result.stdout
    assert "NG" not in result.stdout
    assert "PyPI" not in result.stdout  # 作るときに要るものだけを確かめる
    assert "空き容量" not in result.stdout


@pytest.mark.slow
def test_setupを承認すると専用環境ができて委譲したstatusが動く(tmp_path):
    data = tmp_path / "data"
    vault_dir = tmp_path / "仕事 vault"
    vault_dir.mkdir()

    setup = run("setup", "--yes", "--data", str(data), offline=False)
    run("configure", "--vault", str(vault_dir), "--data", str(data), offline=False)
    (vault_dir / "00_inbox" / "memo.txt").write_text(
        "第3四半期の販売計画と予算の見直しについて、各部門の数値を確認する", encoding="utf-8"
    )
    status = run("status", "--data", str(data), offline=False)
    doctor = run("doctor", "--data", str(data), offline=False)

    assert setup.returncode == 0, setup.stderr
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["inbox"] == 1
    assert doctor.returncode == 0, doctor.stdout


def python_first(python) -> dict:
    """`python` コマンドが、指定の Python を指すように PATH を整える。"""
    return {"PATH": os.pathsep.join([str(Path(python).parent), os.environ["PATH"]])}


def fake_python(directory: Path, body: str) -> dict:
    """`python` コマンドを、指定の動作のバッチファイルに差し替える PATH を返す。"""
    directory.mkdir(exist_ok=True)
    (directory / "python.bat").write_text(body, encoding="utf-8")
    return {"PATH": os.pathsep.join([str(directory), os.environ["PATH"]])}


def load_run_module():
    """run.py の関数を、サブプロセスを介さずに確かめるために読み込む。"""
    spec = importlib.util.spec_from_file_location("run_under_test", RUN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_固定版と同じ版が入っていればパッケージの問題はない(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"pytest=={metadata.version('pytest')}\n", encoding="utf-8")

    assert load_run_module()._packages_problem(requirements) is None


def test_固定版と違う版は入っている版を示して使えない扱いにする(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("pytest==0.0.1\n", encoding="utf-8")

    problem = load_run_module()._packages_problem(requirements)

    assert "pytest" in problem
    assert "0.0.1" in problem
    assert metadata.version("pytest") in problem


def test_未導入のパッケージは名前を示して使えない扱いにする(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("no-such-package-xyz==1.0\n", encoding="utf-8")

    problem = load_run_module()._packages_problem(requirements)

    assert "no-such-package-xyz" in problem
    assert "未導入" in problem


def test_空き容量が足りないとき空きを示す_未作成の置き場は存在する親で測る(tmp_path, monkeypatch):
    module = load_run_module()
    measured = []

    def disk_usage(path):
        measured.append(Path(path))
        return SimpleNamespace(free=100 * 1024 * 1024)

    monkeypatch.setattr(module.shutil, "disk_usage", disk_usage)

    problem = module._disk_problem(tmp_path / "未作成" / "data")

    assert "100 MB" in problem
    assert measured == [tmp_path]


def test_空き容量が足りていれば問題はない(tmp_path, monkeypatch):
    module = load_run_module()
    monkeypatch.setattr(
        module.shutil, "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3)
    )

    assert module._disk_problem(tmp_path) is None


def test_PyPIに届かないときプロキシを案内する(monkeypatch):
    module = load_run_module()

    def unreachable(*args, **kwargs):
        raise URLError("接続できません")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)

    assert "プロキシ" in module._pypi_problem()


def test_PyPIに届けば問題はない(monkeypatch):
    module = load_run_module()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())

    assert module._pypi_problem() is None


def test_doctorは専用環境が使えないとき2通りの対処コマンドを実在のパスで出す(tmp_path):
    data = tmp_path / "置き場 data"

    result = run("--data", str(data), "doctor")

    assert result.returncode == 3
    for mode in ("venv", "system"):
        assert f'python "{RUN}" --data "{data}" setup --mode {mode} --yes' in result.stdout


def test_doctorはpythonコマンドがPATHにないときNGと対処を出す(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    result = run("--data", str(tmp_path / "data"), "doctor", env_extra={"PATH": str(empty)})

    assert result.returncode == 3
    assert "NG  python コマンド" in result.stdout
    assert "PATH" in result.stdout


def test_doctorはvaultが未設定のとき終了コード4で対処を出す(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)

    result = run("doctor", "--data", str(data))

    assert result.returncode == 4, result.stdout
    assert "NG  vault" in result.stdout
    assert "/vault-kb:init" in result.stdout
    assert "configure --vault" in result.stdout


def test_doctorはvaultのフォルダがないとき終了コード4でパスを示す(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)
    missing = tmp_path / "別PCにないフォルダ"
    (data / "config.json").write_text(json.dumps({"vault": str(missing)}), encoding="utf-8")

    result = run("doctor", "--data", str(data))

    assert result.returncode == 4, result.stdout
    assert "見つかりません" in result.stdout
    assert str(missing) in result.stdout


def test_doctorはvaultの設定が壊れているとき終了コード4になる(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)
    (data / "config.json").write_text("{壊れた", encoding="utf-8")

    result = run("doctor", "--data", str(data))

    assert result.returncode == 4, result.stdout
    assert "NG  vault" in result.stdout


requires_pinned_system = pytest.mark.skipif(
    load_run_module()._packages_problem() is not None,
    reason="開発用 venv に requirements.txt の固定版が揃っていない（開発環境を作り直す）",
)


@requires_pinned_system
def test_システムのPythonに固定版が揃っていれば専用環境なしで使える(tmp_path):
    data = tmp_path / "data"
    vault_dir = tmp_path / "仕事 vault"
    vault_dir.mkdir()
    system = {"python": sys.executable, "env_extra": python_first(sys.executable)}

    configured = run("configure", "--vault", str(vault_dir), "--data", str(data), **system)
    doctor = run("doctor", "--data", str(data), **system)
    status = run("status", "--data", str(data), **system)

    assert configured.returncode == 0, configured.stderr
    assert doctor.returncode == 0, doctor.stdout
    assert "OK  専用環境（システムの Python）" in doctor.stdout
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["inbox"] == 0
    assert not (data / "venv").exists()


def test_venvとシステムの両方が使えるときはvenvを使う(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)

    result = run("doctor", "--data", str(data), python=sys.executable)

    assert "OK  専用環境（venv）" in result.stdout


def test_system指定のsetupはyesなしで警告を出して何も入れない(tmp_path):
    data = tmp_path / "data"

    result = run("--data", str(data), "setup", "--mode", "system")

    assert result.returncode == 3
    assert BASE_PYTHON in result.stdout
    assert "--user" in result.stdout
    assert "他のツール" in result.stdout
    assert "--yes" in result.stdout
    assert not data.exists()


def test_setupのmodeが不正なとき終了コード2でvenvとsystemを案内する(tmp_path):
    result = run("--data", str(tmp_path / "data"), "setup", "--mode", "conda", "--yes")

    assert result.returncode == 2
    assert "venv" in result.stderr
    assert "system" in result.stderr
    assert not (tmp_path / "data").exists()


@pytest.mark.slow
def test_systemを承認すると実行中のPythonのユーザー領域に入り専用環境なしで使える(tmp_path):
    data = tmp_path / "data"
    userbase = tmp_path / "userbase"  # 開発者自身の Python を汚さないよう、入れ先を分ける
    vault_dir = tmp_path / "仕事 vault"
    vault_dir.mkdir()
    options = {
        "env_extra": {"PYTHONUSERBASE": str(userbase), **python_first(BASE_PYTHON)},
        "flags": (),
        "offline": False,
    }

    setup = run("setup", "--mode", "system", "--yes", "--data", str(data), **options)
    configured = run("configure", "--vault", str(vault_dir), "--data", str(data), **options)
    doctor = run("doctor", "--data", str(data), **options)
    status = run("status", "--data", str(data), **options)

    assert setup.returncode == 0, setup.stderr
    assert any(userbase.rglob("markitdown"))
    assert configured.returncode == 0, configured.stderr
    assert doctor.returncode == 0, doctor.stdout
    assert "OK  専用環境（システムの Python）" in doctor.stdout
    assert status.returncode == 0, status.stderr
    assert not (data / "venv").exists()


def test_setupは_mode_のイコール形式など不明な引数を終了コード2で止める(tmp_path):
    result = run("--data", str(tmp_path / "data"), "setup", "--mode=system", "--yes")

    assert result.returncode == 2
    assert "--mode" in result.stderr
    assert not (tmp_path / "data").exists()


def test_仮想環境の中ではsystem指定のsetupを終了コード3で断る(tmp_path):
    result = run(
        "--data",
        str(tmp_path / "data"),
        "setup",
        "--mode",
        "system",
        "--yes",
        python=empty_venv_python(tmp_path / "light"),
    )

    assert result.returncode == 3
    assert "仮想環境" in result.stderr
    assert not (tmp_path / "data").exists()


def test_空き容量を調べられない置き場でも例外にならず問題として示す(tmp_path, monkeypatch):
    module = load_run_module()

    def no_such_drive(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(module.shutil, "disk_usage", no_such_drive)

    assert "調べられません" in module._disk_problem(tmp_path)


def test_doctorはvaultが空の設定のとき終了コード4になる(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_light_env(data)
    (data / "config.json").write_text(json.dumps({"vault": ""}), encoding="utf-8")

    result = run("doctor", "--data", str(data))

    assert result.returncode == 4, result.stdout
    assert "NG  vault" in result.stdout


def test_requirementsの全行が名前と固定版の形で_packages_problemが読める():
    lines = (PLUGIN / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[0-9A-Za-z.]+$")

    assert [line for line in lines if line.strip() and not pattern.match(line)] == []


def test_run_pyを読み込むだけでは重い標準ライブラリを読み込まない(tmp_path):
    # approve は全ツール呼び出しで走るので、doctor だけが使う import を先頭に置かない
    code = (
        "import importlib.util as u, sys; s = u.spec_from_file_location('r', sys.argv[1]); "
        "m = u.module_from_spec(s); s.loader.exec_module(m); "
        "print([n for n in ('urllib.request', 'importlib.metadata') if n in sys.modules])"
    )

    result = subprocess.run(
        [BASE_PYTHON, "-S", "-c", code, str(RUN)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        check=False,
    )

    assert result.stdout.strip() == "[]", result.stderr


def test_doctorはPyPIに届かないときNGとプロキシとミラーを案内する(tmp_path):
    result = run("--data", str(tmp_path / "data"), "doctor")

    assert "NG  PyPI への接続" in result.stdout
    assert "プロキシ" in result.stdout
    assert "ミラー" in result.stdout


def test_doctorはpythonコマンドが起動できないときNGと対処を出す(tmp_path):
    body = """@exit /b 9009
"""

    result = run(
        "--data", str(tmp_path / "data"), "doctor", env_extra=fake_python(tmp_path / "bin", body)
    )

    assert result.returncode == 3
    assert "NG  python コマンド" in result.stdout
    assert "起動できません" in result.stdout


@requires_pinned_system
def test_システムのPythonを使うときフックのpythonが別ならNGで両方のパスを示す(
    tmp_path, tmp_path_factory
):
    data = tmp_path / "data"
    data.mkdir()
    configure_vault(data, tmp_path / "vault")
    # .bat の中のパスに日本語が入ると、cmd が読めない。ASCII だけのフォルダに作る
    other = empty_venv_python(tmp_path_factory.mktemp("light"))  # sys.executable とは別の Python
    body = f"""@"{other}" %*
"""

    result = run(
        "doctor",
        "--data",
        str(data),
        python=sys.executable,
        env_extra=fake_python(tmp_path_factory.mktemp("bin"), body),
    )

    assert result.returncode == 3
    assert "NG  python コマンド" in result.stdout
    assert "フックが使う python は" in result.stdout
    assert sys.executable in result.stdout


def test_doctorの対処コマンドは相対パスの置き場を絶対パスにする(tmp_path):
    result = run("--data", "rel-data", "doctor", cwd=tmp_path)

    assert f'--data "{tmp_path / "rel-data"}"' in result.stdout


def test_pipが成功しても読み込めなければsetupは失敗を報告する(tmp_path, monkeypatch, capsys):
    module = load_run_module()
    real_run = subprocess.run

    def pip_succeeds(command, *args, **kwargs):
        # 外部への導入（pip）だけを成功に置き換える。導入後の確認は本物のプロセスで行う
        if "pip" in command:
            return SimpleNamespace(returncode=0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", pip_succeeds)
    empty = empty_venv_python(tmp_path / "light")  # 固定版の入っていない Python
    monkeypatch.setattr(module.sys, "executable", empty)
    monkeypatch.setattr(module.sys, "prefix", module.sys.base_prefix)  # 仮想環境の外とみなす

    code = module._setup_system(tmp_path / "data", approved=True)

    assert code == 1
    assert "使えません" in capsys.readouterr().err


@pytest.mark.parametrize("version", [(3, 10, 0), (3, 11, 9), (3, 15, 0)])
def test_固定版のライブラリが入らない版のPythonは使える範囲を示して問題にする(version, monkeypatch):
    module = load_run_module()
    fake_sys = SimpleNamespace(
        version_info=version, version=".".join(map(str, version)) + " (test)"
    )
    monkeypatch.setattr(module, "sys", fake_sys)

    problem = module._python_problem(need_venv=False)

    assert "3.12" in problem
    assert "3.14" in problem


@pytest.mark.parametrize("version", [(3, 12, 0), (3, 13, 5), (3, 14, 3)])
def test_固定版のライブラリが入る版のPythonは問題にしない(version, monkeypatch):
    module = load_run_module()
    fake_sys = SimpleNamespace(
        version_info=version, version=".".join(map(str, version)) + " (test)"
    )
    monkeypatch.setattr(module, "sys", fake_sys)

    assert module._python_problem(need_venv=False) is None


def test_requirementsが要求するPythonの範囲と_run_pyの範囲が一致している():
    # PyPI で確かめた値: numpy 2.5.4 は >=3.12、markitdown 0.1.9 は <3.15
    module = load_run_module()

    assert module.MIN_PYTHON == (3, 12)
    assert module.MAX_PYTHON == (3, 15)
