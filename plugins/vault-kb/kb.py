"""vault_kb の起動口。run.py が専用環境の Python で実行する。

`python -m vault_kb` にしないのは、カレントディレクトリ（利用者のプロジェクト）が
sys.path の先頭に入り、そこにある json.py などが先に読み込まれるのを避けるため。
"""

import sys

from vault_kb.cli import main

sys.exit(main())
