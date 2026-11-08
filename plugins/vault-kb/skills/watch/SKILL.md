---
name: watch
description: vault の `00_inbox` と案件のフォルダの変化を見張り、置かれた資料を案件・区分のフォルダへ自動で片づけ続ける。利用者が /vault-kb:watch と打ったセッションの中で、会話と並行して動く（セッションを閉じると止まる）。利用者が打ったときだけ使い、それ以外の場面では呼ばない。初期設定には init、資料の検索・質問には ask を使う。
disable-model-invocation: true
allowed-tools: Bash(python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" prepare) Bash(python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" apply *) Monitor Read(//**/vault-kb/.vault-kb-plan.json) Read(//**/references/reconcile.md) Edit(//**/vault-kb/.vault-kb-plan.json)
compatibility: Windows 専用。Python 3.12〜3.14。
---

# vault-kb の見張り

利用者のセッションの中で、会話と並行して動く。片づけについて利用者に質問しない。報告は、片づけのたびに 1 行にする（run.py・計画ファイル・Monitor の許可はプラグインのフックが与えるので、確認は出ない）。

## 手順

1. `${CLAUDE_PLUGIN_ROOT}/references/reconcile.md` を読む。
2. 取り残しを 1 回片づける: reconcile.md の手順を実行する。
3. 見張りを張る: `Monitor` を次の指定で呼ぶ。
   - command: `python "${CLAUDE_PLUGIN_ROOT}/run.py" --data "${CLAUDE_PLUGIN_DATA}" watch`
   - description: `vault-kb の変化`
   - timeout_ms: 1800000
4. `Monitor` から `changed` の行が来たら、reconcile.md の手順を実行する。`stopped` の行が来たら、見張りは `/vault-kb:unwatch` で止められた。何もせずに終わる（手順 3 をやり直さない）。
5. `stopped` を受けずに期限切れの通知が来たら、reconcile.md の手順を 1 回実行してから、手順 3 をやり直す（開いたままで残ったファイルなど、変化がなくても残っているものを拾う）。
6. 利用者に質問しない。迷っても決めて進める。結果の報告は 1 行にする。
