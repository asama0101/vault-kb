"""コマンドライン。結果は JSON で標準出力に書く（watch だけは 1 行ずつの文字列）。

終了コード: 0 成功、1 実行時のエラー、2 引数・設定のエラー。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import bundle, conflict, lock, reconcile, watch
from .vault import (
    ConfigError,
    Vault,
    VaultNotFound,
    atomic_write,
    init_vault,
    install_obsidian_assets,
    load_vault,
    save_config,
)

_LOG_HINT = "処理した資料の記録は log.md にあります"


class _ApplyFailed(OSError):
    """reconcile.apply の中で起きた OSError（ログの場所を添えて報告する）。"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault_kb")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="vault を設定して標準のフォルダを作る")
    configure.add_argument("--vault", required=True)
    sub.add_parser("status", help="片づけ待ちの件数と、片づけの状態")
    sub.add_parser("notify", help="未設定・片づけ待ちを案内する（フック用）")
    sub.add_parser("prepare", help="追従・削除を反映し、判断が要る資料を変換して返す")
    apply = sub.add_parser("apply", help="計画（JSON）どおりに資料を置いてノートを書く")
    apply.add_argument("plan")
    sub.add_parser("release", help="片づけのロックを解く")
    sub.add_parser("reindex", help="index.md を作り直す")
    sub.add_parser("watch", help="vault の変化を 1 行ずつ出す（Ctrl+C か unwatch で終わる）")
    sub.add_parser("unwatch", help="見張りに停止の印を書き、確認事項を返す")
    sub.add_parser("record-stop", help="log.md に見張りの停止を記録する")
    sub.add_parser("conflicts", help="同名の確認待ちの一覧（フルパス・サイズ・更新日時）")
    resolve = sub.add_parser("resolve", help="同名の確認待ちを解決する")
    resolve.add_argument("source", help="conflicts の source（vault からの相対パス）")
    resolve.add_argument("action", choices=["overwrite", "discard"])
    return parser


def _data_dir() -> Path:
    home = os.environ.get("VAULT_KB_HOME")
    if not home:
        raise ConfigError("環境変数 VAULT_KB_HOME が未設定です（run.py の --data で渡します）")
    return Path(home)


def _notify() -> None:
    """フック用。どんな失敗でも、セッションの開始を妨げない（呼び出し側は必ず 0 を返す）。"""
    try:
        _notify_lines()
    except Exception as error:  # noqa: BLE001  フックは何があっても止めない
        print(f"[vault-kb] 通知を省略しました: {type(error).__name__}: {error}", file=sys.stderr)


def _notify_lines() -> None:
    try:
        data_dir = _data_dir()
        vault = load_vault(data_dir)
        lines = watch.notify_lines(watch.status(vault, data_dir))
    except VaultNotFound as error:  # 設定はあるが、vault が見えない（外付け・同期の切断など）
        lines = [str(error)]
    except ConfigError:
        lines = ["vault が未設定です。/vault-kb:init で設定してください。"]
    except OSError:
        return
    for line in lines:
        print(f"[vault-kb] {line}")


def _configure(data_dir: Path, vault_arg: str) -> dict[str, Any]:
    root = Path(vault_arg).resolve()
    if not root.is_dir():
        raise ValueError(f"vault のフォルダが見つかりません: {root}")
    data_dir.mkdir(parents=True, exist_ok=True)
    vault = init_vault(root)
    install_obsidian_assets(root)
    save_config(data_dir, vault.root)
    bundle.write_indexes(vault)
    log = vault.root / "log.md"
    if not log.exists():
        atomic_write(log, "")
    return {"vault": str(vault.root)}


def _plan_lock_token(vault: Vault) -> str | None:
    """計画ファイルの lock_token（apply の失敗後の release が、自分のロックだけを解くため）。

    計画がない・読めない・印がないときは None（従来どおり、無条件に解く）。
    """
    try:
        token = json.loads((vault.root / reconcile.PLAN_NAME).read_text(encoding="utf-8"))
        token = token.get("lock_token")
    except (OSError, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) else None


def _run(args: argparse.Namespace, data_dir: Path) -> dict[str, Any] | list[Any] | None:
    """サブコマンドを実行し、JSON にする結果を返す（watch は None）。"""
    if args.command == "configure":
        return _configure(data_dir, args.vault)
    vault = load_vault(data_dir)
    now = datetime.now().astimezone()
    if args.command == "status":
        return watch.status(vault, data_dir)
    if args.command == "prepare":
        try:
            return reconcile.prepare(vault, data_dir, now)
        except lock.Busy:
            return {"busy": True}
    if args.command == "apply":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        try:
            return reconcile.apply(vault, data_dir, plan, now)
        except OSError as error:  # 後処理の失敗。処理した資料は log.md に残っている
            raise _ApplyFailed(str(error)) from error
    if args.command == "release":
        lock.release(data_dir, _plan_lock_token(vault))
        return {"released": True}
    if args.command == "reindex":
        bundle.write_indexes(vault)
        return {"reindexed": True}
    if args.command == "unwatch":
        watch.request_stop(data_dir, now)
        return {"stop_requested": True, **watch.open_items(vault, data_dir)}
    if args.command == "record-stop":
        unresolved = watch.open_items(vault, data_dir)["unresolved"]
        suffix = f"（未解消 {unresolved} 件）" if unresolved else ""
        bundle.append_log(vault, [f"**Stopped** 見張りを止めた{suffix}"], now)
        return {"recorded": True, "unresolved": unresolved}
    if args.command == "conflicts":
        return conflict.listing(vault)
    if args.command == "resolve":
        try:
            lock.acquire(data_dir, now)
        except lock.Busy:
            return {"busy": True}
        try:
            bundle.append_log(vault, [conflict.resolve(vault, args.source, args.action)], now)
        finally:
            lock.release(data_dir, lock.token_of(now))
        return {"resolved": args.action, "source": args.source}
    for line in watch.watch_events(vault, data_dir):  # watch
        print(line, flush=True)
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "notify":
        _notify()
        return 0
    try:
        result = _run(args, _data_dir())
    except KeyboardInterrupt:
        return 0
    except (ConfigError, ValueError) as error:  # JSON の構文エラーも ValueError
        print(f"エラー: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"実行時のエラー: {error}", file=sys.stderr)
        if isinstance(error, _ApplyFailed):
            print(_LOG_HINT, file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
