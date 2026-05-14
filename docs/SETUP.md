# macOS セットアップ手順

Sigma fp L テザー撮影アプリを動かすための準備。**初回 1 回のみ**。

---

## 1. Python 3.11 の用意

最近の macOS は Python 3.11 が同梱されていることが多いですが、Homebrew で入れるのが確実です。

```bash
brew install python@3.11
python3.11 --version   # 3.11.x が出ればOK
```

`brew install` で問題が出る場合は、公式インストーラ https://www.python.org/downloads/ から `.pkg` をダウンロード。

---

## 2. 仮想環境を作って依存をインストール

```bash
cd /path/to/fp-l-tether

# 仮想環境を作成
python3.11 -m venv venv

# アクティベート
source venv/bin/activate

# パッケージ管理ツールを最新化
pip install --upgrade pip setuptools wheel

# 本体と依存をインストール（pyproject.toml を読みます）
pip install -e .
```

成功すると以下のような出力が出ます:

```
Successfully installed fp-l-tether-0.0.1 pyobjc-core-... pyobjc-framework-ImageCaptureCore-... typer-... ...
```

### 確認

```bash
python -c "import ImageCaptureCore; print('OK:', ImageCaptureCore.__bundle__)"
```

`OK: <NSBundle ...ImageCaptureCore.framework>` のような出力が出れば OK。

---

## 3. macOS のプライバシー設定

`ImageCaptureCore` を使ってカメラに通信するには、ターミナル（または Claude Code）にカメラへのアクセス権を与える必要があります。

1. システム設定 → プライバシーとセキュリティ → カメラ
2. **ターミナル**（または **Claude Code**）を ON にする
3. 必要に応じて再起動

「フルディスクアクセス」や「ファイルとフォルダ」の許可は不要です。

---

## 4. カメラ準備（毎回必須）

**重要**: USB モードは USB 接続中には変更できないため、必ず接続前に設定すること。

1. Sigma fp L の電源 OFF
2. USB-C ケーブルを**外す**
3. CINE/STILL スイッチを **STILL** にする
4. 電源 ON
5. **MENU → システム → USB mode → "Camera Control"** を選択
6. もう一度電源 OFF
7. USB-C ケーブルで Mac に接続
8. 電源 ON

接続後、Mac の「イメージキャプチャ.app」を起動するとカメラが見えます。**ただし、これは閉じてください**（同時に2アプリがPTPセッションを掴めないため）。

---

## 4.5 fp L 本体メニュー（テザー撮影向け推奨設定）

長時間のテザー撮影で安定動作させるため、本体メニューで以下を設定してください。

### Auto Power Off → **OFF**（必須推奨）

**MENU → システム → Auto Power Off → OFF**

fp L はテザー撮影中であっても、約5分以上 USB バスが idle になると内部
省電力モードに入る挙動があります。この状態では Sigma 独自オペコード
（`GetCamCaptStatus` 等）が **0-byte data phase** を返すようになり、
本アプリの daemon は一時的に停止します。

本アプリは以下の二段構えで対応していますが、根本予防として本体側の
Auto Power Off を OFF にしておくことを強く推奨:

- **予防**: 60秒毎の `sigma_get_camera_info` ping (keep-alive heartbeat) で
  カメラを起こし続ける（`config.toml [camera] keep_alive_enabled = true`）
- **回復**: 0-byte read を検知したら `sigma_get_camera_info` +
  `sigma_set_datagroup_3_pc_capture` で再起動を試みる
  （panel の status は黄色 `◍ recovering` で表示）

これは fp L 本体の仕様レベルで必要な対策です（テザー用途では keep-alive
を出し続けるか、本体側の Auto Power Off を OFF にしておくのが安全）。

---

## 5. 干渉する他アプリを終了

他のテザー / カメラ取り込みアプリが Sigma fp L の PTP セッションを掴むと
本アプリは接続できません。同時に動かさないでください:

- **イメージキャプチャ.app** (macOS 標準)
- **Photos.app**（自動起動設定になっている場合）
- **Lightroom Classic** の Tether ウィンドウ（Lightroom 本体は OK）
- その他のサードパーティ製テザー / PTP クライアント

Photos.app が勝手に起動する場合の対策:
1. イメージキャプチャ.app を一度起動
2. 左下の「このカメラを接続したときに開くアプリケーション」を **「アプリケーションなし」** に変更

---

## 6. Phase 0 動作検証を実行

```bash
source venv/bin/activate  # まだなら

# Step A: 列挙のみ
python scripts/phase0_smoke_test.py

# Step B: PTP セッション
python scripts/phase0_session_test.py

# Step C: フル撮影フロー (5枚)
python scripts/phase0_snap_test.py
```

各ステップの結果は `docs/PHASE0_LOG.md` に追記してください。

---

## 7. （任意）クリップボードに hex を貼り付けやすくする

Phase 0 のデバッグ中、`hex_dump()` の出力を CLAUDE.md / GitHub Issue に貼ることが多いので、ターミナルの選択をパイプで送れるエイリアスを作っておくと便利:

```bash
echo 'alias clip="pbcopy"' >> ~/.zshrc
# 使い方: cat /tmp/dump.txt | clip
```

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `ModuleNotFoundError: No module named 'ImageCaptureCore'` | `pip install pyobjc-framework-ImageCaptureCore` を再実行 |
| `MacOSOnlyError` | macOS で実行しているか確認 |
| カメラが列挙されない | USB モードと電源、ケーブル、他アプリ終了をチェック |
| PTP セッションが開けない | 他のテザー / イメージキャプチャ.app を完全終了 |
| シャッターが切れない（SnapCommand失敗） | カメラの「PC接続時の動作」設定を確認、FW v3.0 以降に更新 |

詳しくは `docs/TROUBLESHOOTING.md` を参照。
