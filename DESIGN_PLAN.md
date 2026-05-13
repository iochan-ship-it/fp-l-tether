# Sigma fp L テザー撮影アプリ — 設計プラン

**作成日**: 2026-05-11
**対象**: Sigma fp L → Mac (USB-C) → Lightroom Classic
**ユーザー**: IO（素人だがClaude Codeで反復開発、Python経験はhousehold-budget/class-schedule相当）
**目的**: 安定して長く使えるテザー撮影ワークフローを自作する

---

## 0. 結論サマリー（Why this stack）

**採用スタック**:
- 言語: **Python 3.11**（既存スタックと統一）
- カメラ通信: **PyObjC + Apple ImageCaptureCore + 生PTPオペコード**
- UI: **CLI (Typer)** + **macOSメニューバー (rumps)** 両対応
- Lightroom連携: **Auto Import 監視フォルダ方式 / セッションフォルダ直書き方式** を `config.toml` で切替
- 設定: TOML
- ログ: 構造化JSON + テキスト（`~/Library/Logs/fp-l-tether/`）

**なぜこのスタック（4つの理由）**:

1. **Apple純正パスで安定性が最高** — Sigma公式SDKと同じく `ICCameraDevice.requestSendPTPCommand:` を使う。これはApple純正の `ImageCaptureCore` フレームワークで、macOSの「イメージキャプチャ.app」と同じ低レベル経路。libusb のような野良ドライバを介さないため、USBスタックの安定性はOSと一蓮托生。
2. **sudoもlibusbも不要** — ユーザー権限のみで動く。素人運用での権限トラブルが起きない。
3. **Pythonのまま** — 既存スタック（household-budget、class-schedule）と完全に揃う。Claude Code との反復もそのまま使える。Swift/Xcode 開発に切り替える必要なし。
4. **長期保守性** — SnapCommand=0x901B などのSigma独自オペコードは fp / fp L / fp II (future) で互換が高い。Apple のフレームワーク側も Apple がメンテし続ける。Sigma が SDK 更新を止めても、オペコードを直接送れるので影響を受けない。

---

## 1. リサーチ結果サマリー

### 1.1 sigma-ptpy（GitHub: makanikai/sigma-ptpy）
- **License**: MIT / **Stars**: 7 / **Forks**: 4
- 既知バグ:
  - PTPイベントポーリングで `[Errno 60] Operation timed out`（`ignore_events=True` で回避）
  - 公式ドキュメント自体に誤りがあり、レンズ焦点距離が常に間違う
- **fp L の動作報告なし**（fp 専用前提）
- 内部実装は `ptpy` 経由で libusb を叩く形
- → 採用しないが、**SnapCommandのデータ構造リファレンス**として読む

### 1.2 SIGMA Camera Control SDK for Mac（DMGから抽出）
- 構成: **26+ の macOS Framework（API1つにつき1Framework）+ Headers + SampleAPP**
- 通信経路: `ICCameraDevice.requestSendPTPCommand:outData:sendCommandDelegate:didSendCommandSelector:contextInfo:`
  → **Apple ImageCaptureCore を直接利用**
- 抽出した全PTPオペコード（重要なもの）:

| オペコード | コード | 用途 |
|---|---|---|
| `OpenSession` | 0x1002 | PTPセッション確立 |
| `GetCamCaptStatus` | **0x9015** | キャプチャ状態ポーリング（シャッター検知） |
| `SnapCommand` | **0x901B** | Macからシャッターを切る |
| `GetPictFileInfo2` | **0x902d** | 撮影したファイルのメタ情報 |
| `GetBigPartialPictFile` | **0x9022** | DNG本体をチャンク取得 |
| `ClearImageDBSingle` | **0x901C** | カメラ内ImageDBから削除 |
| `GetCamDataGroup1-5` | 0x9012-0x9027 | ISO/SS/絞り/WB/Color等の現設定取得 |
| `GetCamCanSetInfo5` | 0x9030 | 設定可能範囲取得 |
| `GetCamOpPermission` | 0x9039 | "PC control mode" 確認 |
| `ConfigApi` | 0x9035 | APIバージョン交渉 |
| `CloseApplication` | 0x902f | 終了処理 |

- SampleAPP のシャッターフローは IssuesログにWindows版で記録あり（fp L で動作確認済み）：
  1. `SnapCommand` 送信（data=`0x02,0x02,0x01` → still + 1shot）
  2. `GetCamCaptStatus` をポーリング → "Image processing [ImageID=…]" → "ImageReady"
  3. `GetPictFileInfo2` でファイル情報取得
  4. `GetBigPartialPictFile` で分割DL（大ファイル対応）
  5. `ClearImageDBSingle` でクリーンアップ
- ライセンス: 同梱ドキュメント以外サポートなし、コーダー向け（**Pythonからimportできない**ためフレームワーク自体は使わず、PTPオペコードのみ拝借）

### 1.3 libgphoto2 / gphoto2 のfp L対応（GitHub Issue #882）
- 2.5.31 でUSB ID登録済みだが「サポート」とは呼べない状態
- 動作報告:
  - 1〜2枚目は撮れることもある
  - 3枚目以降「PTP General Error」で失敗
  - **画像のクローン返却バグ**（Try5/Try6 が Try4 の複製になる、未解決）
  - `--summary` `--capture-preview` も挙動不安定
- → **採用不可**。但し、ASCOM driver(CloudyNights)の存在は「fp L のPTP制御は不可能ではない」ことの証明

### 1.4 Capture One Pro / Dragonframe / その他
- Capture One: 唯一の商用安定ソフトだが月額。設定リセットバグ。
- Dragonframe 5.x: $325、静物用途には過剰
- Lightroom Classic ネイティブ tether: Sigma 非対応
- Smart Shooter / Sofortbild / Cascable: Sigma 非対応

### 1.5 Lightroom Classic Auto Import の挙動
- **監視フォルダは空でなければならない**（取込→移動の動作）
- **サブフォルダは監視しない**
- 取込中は最新画像が自動で Loupe ビューに表示される → スタジオで超便利
- DNG への自動変換オプションはAuto Importには無い（撮影元がDNGなら問題なし）
- 「Don't Import Suspected Duplicates」設定で重複防止
- **ファイルは atomic にwatched folderへ rename すべき**（partial fileが取り込まれるのを防ぐ）

---

## 2. 全体アーキテクチャ

```
┌───────────────────────────────────────────────────────────────────┐
│                  Sigma fp L (USB-C, Camera Control mode)          │
└─────────────────────────────┬─────────────────────────────────────┘
                              │ USB 3.1 (PTP/MTP)
                              │
                ┌─────────────▼─────────────┐
                │ macOS ImageCaptureCore.fwk│  ← Apple純正、ユーザー権限
                └─────────────┬─────────────┘
                              │ ICCameraDevice.requestSendPTPCommand
                              │
        ┌─────────────────────▼──────────────────────┐
        │              fp-l-tether (Python)          │
        │ ┌────────────────┐  ┌──────────────────┐   │
        │ │ camera/        │  │ ui/              │   │
        │ │  ic_bridge.py  │  │  cli.py (Typer)  │   │
        │ │  ptp_codes.py  │  │  menubar.py(rumps)│  │
        │ │  session.py    │  │                  │   │
        │ │  shutter_loop  │  └──────────────────┘   │
        │ └────────────────┘                          │
        │ ┌────────────────┐  ┌──────────────────┐   │
        │ │ transfer/      │  │ config/          │   │
        │ │  downloader.py │  │  schema.py       │   │
        │ │  filename.py   │  │  config.toml     │   │
        │ │  atomic_io.py  │  └──────────────────┘   │
        │ └────────────────┘                          │
        │ ┌────────────────┐  ┌──────────────────┐   │
        │ │ lightroom/     │  │ telemetry/       │   │
        │ │  watch_mode.py │  │  logger.py       │   │
        │ │  session_mode.py│ │  metrics.py      │   │
        │ └────────────────┘  └──────────────────┘   │
        └─────────────────────┬──────────────────────┘
                              │
            ┌─────────────────┴──────────────────┐
            │ (mode A) ~/Pictures/Tether/_watch/  │  ← Lightroom Auto Import 監視
            │ (mode B) ~/Pictures/Tether/Session_*│  ← 手動Lightroom取込
            └─────────────────┬──────────────────┘
                              │
                ┌─────────────▼─────────────┐
                │   Lightroom Classic       │
                │   Auto Import / Catalog   │
                └───────────────────────────┘
```

### 2.1 ディレクトリ構成

```
~/Projects/fp-l-tether/
├── pyproject.toml             # Poetry または uv 管理
├── README.md
├── CLAUDE.md                  # Claude Codeセッション継続用
├── config.toml                # ユーザー設定（保存先・命名規則・モード）
├── config.example.toml
├── fp_l_tether/
│   ├── __init__.py
│   ├── __main__.py            # `python -m fp_l_tether` エントリ
│   ├── camera/
│   │   ├── __init__.py
│   │   ├── ic_bridge.py       # PyObjC + ImageCaptureCore ラッパー
│   │   ├── ptp_codes.py       # SDKヘッダーから抽出したオペコード/構造体
│   │   ├── ptp_packets.py     # PTPパケット組み立て (チェックサム含む)
│   │   ├── session.py         # OpenSession/ConfigApi/CloseApplication
│   │   ├── status_poller.py   # GetCamCaptStatusポーリングループ
│   │   ├── snap.py            # SnapCommand送信
│   │   └── settings_reader.py # GetCamDataGroup1-5
│   ├── transfer/
│   │   ├── __init__.py
│   │   ├── file_info.py       # GetPictFileInfo2 パース
│   │   ├── downloader.py      # GetBigPartialPictFile チャンク受信
│   │   ├── atomic_io.py       # tmp → final atomic rename
│   │   └── filename.py        # 命名ルール (Session/Item/Shot)
│   ├── lightroom/
│   │   ├── __init__.py
│   │   ├── watch_mode.py      # Mode A: Auto Import watched folder へ rename
│   │   └── session_mode.py    # Mode B: セッションフォルダ直書き
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── cli.py             # Typer ベース CLI
│   │   ├── menubar.py         # rumps メニューバーアプリ
│   │   └── messages.py        # 日本語/英語切替メッセージ
│   ├── config/
│   │   ├── __init__.py
│   │   ├── schema.py          # Pydantic モデル
│   │   └── loader.py
│   └── telemetry/
│       ├── __init__.py
│       └── logger.py          # 構造化ログ
├── tests/
│   ├── unit/                  # ptp_packets, filename, atomic_io
│   ├── integration/           # 実機テスト用 (CIではskip)
│   └── fixtures/
├── scripts/
│   ├── phase0_smoke_test.py   # 接続検証
│   ├── phase0_snap_test.py    # シャッター検証
│   └── phase0_download_test.py# DNG取得検証
└── docs/
    ├── SETUP.md               # macOSセットアップ手順
    ├── LIGHTROOM.md           # Lightroom Auto Import 設定手順
    └── TROUBLESHOOTING.md
```

---

## 3. Phase 0 — 動作検証（最優先・必須）

Phase 0 は「**PyObjC + ImageCaptureCore で fp L を本当に制御できるか**」を最短経路で確認する3つの段階的スクリプト。

### 3.1 Step 0-A: カメラ列挙テスト（5分）

**`scripts/phase0_smoke_test.py`**
- `ICDeviceBrowser` で接続されているカメラを列挙
- fp L が見えるか？ Vendor ID `0x1003`、Product ID `0xc442` を確認
- `capabilities` に `ICCameraDeviceCanAcceptPTPCommands` が含まれるか確認

成功条件: コンソールに `Sigma fp L (serial: ...)` が出る

失敗時の分岐:
- カメラが見えない → USBモード設定漏れ（Camera Control）、USBケーブル、Macのプライバシー設定（フルディスクアクセス等）
- 見えるが PTP capable じゃない → カメラFW更新（最新版v3.0確認）

### 3.2 Step 0-B: PTPセッション + GetCamCaptStatus 1往復（10分）

**`scripts/phase0_snap_test.py` の前半**
- `ICCameraDevice.requestOpenSession`
- `ConfigApi` (0x9035) 送信 → APIバージョン交渉
- `GetCamCaptStatus` (0x9015) を1回送信
- レスポンスをhex dump

成功条件: `[CaptStatus] Standby` 相当のレスポンスを受信

失敗時の分岐:
- セッションが開けない → 別アプリ（Capture One、イメージキャプチャ）が掴んでいる
- レスポンスがタイムアウト → カメラの「USBモード=Camera Control」設定/電源/ケーブル疑い

### 3.3 Step 0-C: SnapCommand → 画像取得 完全フロー（30分）

**`scripts/phase0_snap_test.py` の後半**
1. `SnapCommand` (0x901B, data=`0x02,0x02,0x01`) でシャッター
2. `GetCamCaptStatus` を200msごとに最大10秒ポーリング
3. ImageID 取得 → `GetPictFileInfo2` でファイル情報
4. `GetBigPartialPictFile` をループしてDNG全体を取得
5. `/tmp/sigma_phase0_test.dng` に保存
6. `ClearImageDBSingle` でクリーンアップ
7. ファイルサイズ・拡張子・先頭バイトを検証

成功条件: ~100MB のDNGが保存され、`file` コマンドで "TIFF image data" と認識される

失敗時の分岐パターン:
- **A: 全成功** → Phase 1 へ
- **B: SnapCommandは通るがDLで失敗** → libgphoto2 と同じ症状。データ構造の差を SDK SampleAPP の hex dump（Issueに記録あり）と比較してパッチ
- **C: SnapCommandすら通らない** → ConfigApi のバージョン交渉ミス。先に GetCamCanSetInfo5 と GetCamOpPermission を呼ぶシーケンスを試す
- **D: PyObjCバインディング自体に問題** → sigma-ptpy + libusb 路線にフォールバック（test_camera.py 利用）

### 3.4 Phase 0 完了条件

- 3-A / 3-B / 3-C すべて成功
- DNG 5枚連続取得テストでクローン返却が起きないこと（libgphoto2バグの再現有無）
- 各オペコードの実レスポンスを `docs/PTP_TRACE.md` に記録

---

## 4. Phase 1 — MVP実装

Phase 0 が成功した前提で、CLI + メニューバー両対応のMVPを実装。

### 4.1 主要機能

| 機能 | 必須? | コンポーネント |
|---|---|---|
| カメラ接続検知 | ✅ | `camera.ic_bridge` |
| シャッター検知（カメラのボタン押下） | ✅ | `camera.status_poller` |
| DNG自動転送 | ✅ | `transfer.downloader` |
| Lightroom Auto Import 連携 | ✅ | `lightroom.watch_mode` |
| セッションフォルダ直書き | ✅ | `lightroom.session_mode` |
| Macからシャッター制御 (CLI/keyboard/menubar) | ✅ | `camera.snap` |
| 撮影設定の参照表示（ISO/SS/絞り/WB） | ✅ | `camera.settings_reader` |
| セッション管理（開始/終了/カウント） | ✅ | `transfer.filename` |
| メニューバーUI（rumps） | ✅ | `ui.menubar` |
| エラーハンドリング（USB抜け、電源OFF、再接続） | ✅ | 全体 |
| ファイル名カスタマイズ（作品番号、メタデータ） | ✅ | `transfer.filename` |
| 撮影枚数・保存先・遅延の表示 | ✅ | `ui.menubar` |
| ログ（テキスト＋JSON） | ✅ | `telemetry.logger` |
| Lightroom側プリセット自動適用ガイド | 📝ドキュメント | `docs/LIGHTROOM.md` |

### 4.2 シャッター検知ループの設計（最重要）

```python
# 擬似コード
async def shutter_loop(camera: ICCameraDevice, session: Session):
    last_image_id = None
    interval_ms = 200  # ポーリング間隔
    while session.active:
        status = await camera.send_ptp(0x9015, GetCamCaptStatusReq())
        # status = [CaptStatus, ImageID, ...]
        if status.image_id != last_image_id and status.code == IMAGE_READY:
            await download_and_save(camera, status.image_id, session)
            await camera.send_ptp(0x901C, ClearImageDBSingleReq(status.image_id))
            last_image_id = status.image_id
        elif status.code in (CAPTURE_RUNNING, IMAGE_PROCESSING):
            interval_ms = 100  # 撮影中は短く
        else:
            interval_ms = 200  # アイドル時は長く
        await asyncio.sleep(interval_ms / 1000)
```

ポイント:
- ポーリング間隔を **動的に調整**（撮影中100ms、アイドル200ms）→ USB帯域節約
- ImageID 差分判定で重複DL防止
- DL中もポーリングは続けて連写に対応（async）

### 4.3 ファイル命名規則

`config.toml` でテンプレート定義:

```toml
[naming]
template = "{session}_{item}_{shot:04d}.dng"
session = "{date}_{name}"  # 例: 20260512_ceramics
item = "untitled"          # CLIで変更可能
auto_increment_shot = true
```

実行時:
```
~/Pictures/Tether/Session_20260512_ceramics/
  ├── 20260512_ceramics_jar_0001.dng
  ├── 20260512_ceramics_jar_0002.dng
  └── ...
```

CLI:
```bash
fp-l-tether start --session ceramics
fp-l-tether item jar    # アイテム名切替
fp-l-tether item lamp   # 次の作品に移ったらコレ
```

### 4.4 Lightroom連携 — 2モード

#### Mode A: Auto Import 監視フォルダ（推奨デフォルト）
- アプリは `~/Pictures/Tether/_watch/` に DNG を atomic rename
- Lightroom Classic の Auto Import が `_watch/` を監視
- LR は取り込み後、ファイルを `~/Pictures/Tether/Session_*/` へ自動移動（LR側で「Move to」設定）
- 撮影直後に Lightroom Loupe ビューが自動更新 ✨

セットアップ手順（docs/LIGHTROOM.md）:
1. LR → File → Auto Import → Auto Import Settings
2. Watched Folder: `~/Pictures/Tether/_watch/`
3. Destination: `~/Pictures/Tether/{session-folder}/`
4. File Naming Template: 「Filename」（アプリ側で命名済みのため）
5. Develop Settings: 任意のプリセット
6. Metadata: スタジオ用テンプレート
7. ☑ Don't Import Suspected Duplicates

#### Mode B: セッションフォルダ直書き
- `~/Pictures/Tether/Session_YYYYMMDD_xxx/` に直接保存
- 撮影後に手動でLR Folder Sync or Import From Folder
- Lightroom Auto Importを使わない人向け（既存のフォルダ整理ルールがある場合）

`config.toml`:
```toml
[lightroom]
mode = "watch"  # "watch" or "session"
watch_folder = "~/Pictures/Tether/_watch"
session_root = "~/Pictures/Tether"
```

### 4.5 メニューバーアプリ（rumps）

メニュー項目（IO目線で「触ったら何が起きるか即分かる」状態）:

```
📷 fp-l-tether
├─ ● 接続中: fp L (SN: xxxxxx)        ← ステータス（クリック不可）
├─ ● セッション: ceramics_20260512    ← 現在セッション
├─ ● 撮影: 12枚 / 最終: 14:32:11
├─ ───
├─ 📸 シャッターを切る                 ← SnapCommand 送信
├─ ▶  撮影開始                          ← shutter loop 開始
├─ ⏸  撮影停止
├─ ───
├─ 📂 セッションフォルダを開く
├─ 📂 Lightroom監視フォルダを開く
├─ ───
├─ ⚙️  設定を確認...                   ← 現設定をsheet表示
│   ├─ ISO: 100
│   ├─ Shutter: 1/125
│   ├─ Aperture: f/8
│   └─ WB: Daylight
├─ ───
├─ 🔧 設定ファイルを開く (config.toml)
├─ 📜 ログを開く
└─ ✖️  終了
```

### 4.6 キーボードショートカット

CLI実行中（または menubar アクティブ時）に押せるもの:
- `Space`: SnapCommand 送信
- `s`: セッション切替
- `i`: アイテム名変更
- `q`: 終了
- `r`: 撮影設定 再取得

実装は `pynput` or rumps の HotKey 機能。

### 4.7 エラーハンドリング設計

| イベント | 検知 | 対応 |
|---|---|---|
| USBケーブル抜け | ICDeviceBrowser delegate `didRemoveDevice` | menubar 赤表示 / ログ / 再接続待ち |
| カメラ電源OFF | requestSendPTPCommand エラー | 5秒間リトライ → 諦めて再接続待ち |
| LR監視フォルダが存在しない | 起動時チェック | 自動作成 + 案内 |
| ディスク残量不足 | shutil.disk_usage | 警告 + 撮影継続（カメラSD側には残る） |
| DNG取得タイムアウト | GetBigPartialPictFile 3回失敗 | スキップ + 次の撮影に進む |
| 撮影設定リセット症状（fp L既知） | GetCamDataGroup1-5 を起動時に保存→比較 | 警告通知 |

### 4.8 ログ仕様

`~/Library/Logs/fp-l-tether/`:
- `tether.log` — 人間可読
- `events.jsonl` — `{timestamp, event, image_id, file, duration_ms}` で1行JSON

ログ例:
```
2026-05-12T14:32:11+09:00 INFO  session.start  ceramics_20260512
2026-05-12T14:32:14+09:00 INFO  shutter.detect image_id=0x01
2026-05-12T14:32:18+09:00 INFO  download.done  file=ceramics_jar_0001.dng size=98MB dt=3.8s
2026-05-12T14:32:18+09:00 INFO  imagedb.clear  image_id=0x01
```

---

## 5. Phase 2 — 安定化・拡張（任意）

| 拡張 | 価値 | 工数感 |
|---|---|---|
| アイテム別自動セッションフォルダ分割 | 陶器1点撮ったら次の点へ自動切替 | 小 |
| 撮影設定の参考CSVエクスポート | EXIFと別に独自記録 | 小 |
| Lightroom スマートコレクション連携 | セッション名で自動振分 | 中 |
| メタデータテンプレート（作家名・展示会など） | XMPサイドカー自動生成 | 中 |
| カメラ設定の事前プリセット適用 | SetCamDataGroup1-5を使う、リスクあり | 中 |
| 撮影記録のスプレッドシート出力 | Google Sheets API連携 | 小 |
| iPhoneからメニュー操作（ngrok+簡易API） | 三脚位置からiPhoneでシャッター | 中 |
| 自動ホワイトバランス校正アシスト | Lightroom Develop連携 | 大 |

---

## 6. 既知リスクと緩和策

| リスク | 確率 | 影響 | 緩和策 |
|---|---|---|---|
| PyObjCでPTP pass-throughが詰まる | 中 | 高 | Phase 0-Cで早期検証。ダメならsigma-ptpyに即フォールバック |
| libgphoto2と同じクローン返却バグ | 中 | 高 | Phase 0-CでImageID差分検証。`ClearImageDBSingle`必須化 |
| fp L のFW更新で挙動変化 | 低 | 中 | FW更新時のregression手順を `docs/REGRESSION.md` に整備 |
| Capture Oneのような設定リセット症状 | 中 | 中 | 起動時に GetCamDataGroup1-5 を読んで「設定が変わったら警告」 |
| Lightroom Auto Importが半端ファイルを取込む | 低 | 中 | atomic rename徹底（tmp→watchフォルダ）。拡張子を `.tmp.dng` から `.dng` へ最後にrename |
| macOSアップデートで ImageCaptureCore 仕様変更 | 低 | 高 | バージョン互換テストを CI で回す |
| USB-C コネクタ物理摩耗 | 中 | 低 | 撮影中はUSB抜き差しゼロという当初目標で大幅軽減。さらにマグネット式アダプタ追加$20で完全保護 |
| sigma-ptpyの保守停止 | 高 | 低 | そもそも採用しないが、フォールバック先としてforkを手元に保管 |

---

## 7. テスト戦略

### 7.1 単体テスト（CIで毎回）
- `ptp_packets.py` — チェックサム計算、エンディアン、データ構造
- `filename.py` — テンプレート展開、衝突回避、不正文字エスケープ
- `atomic_io.py` — tmp→final rename、書き込み中の中断耐性
- `config/schema.py` — Pydantic バリデーション

### 7.2 統合テスト（手動・実機）
- Phase 0 スクリプト群を `tests/integration/` に置く
- 「USB抜き→再接続」シナリオ
- 「電源OFF→ON」シナリオ
- 「100枚連続撮影」スループット
- 「LRが起動してない/Auto Import止まってる」状態の確認

### 7.3 リグレッション
- fp L FW更新ごとに Phase 0-C を実行
- 結果を `docs/COMPATIBILITY.md` に追記

---

## 8. ロードマップ（時間目安）

| Phase | 内容 | 工数（Claude Code併走） |
|---|---|---|
| 0 | 動作検証（3スクリプト） | 0.5〜1日 |
| 1.0 | CLIで最小フロー（検知+DL+保存） | 1〜2日 |
| 1.1 | Lightroom Auto Import連携 | 0.5日 |
| 1.2 | メニューバーUI (rumps) | 1日 |
| 1.3 | エラーハンドリング・再接続 | 1日 |
| 1.4 | セッション管理・命名規則 | 0.5日 |
| 1.5 | ドキュメント（SETUP/LIGHTROOM/TROUBLESHOOTING） | 0.5日 |
| 2 | 拡張機能（任意） | 必要に応じ |

合計MVP到達まで: **6〜7営業日**（実機テスト時間を含む）

---

## 9. すぐ次にやること（Phase 0 着手手順）

1. `~/Projects/fp-l-tether/` を作成
2. `pyproject.toml` を生成（Python 3.11、依存: `pyobjc-framework-ImageCaptureCore`, `typer`, `rumps`, `pydantic`, `tomli`）
3. `scripts/phase0_smoke_test.py` を実装（カメラ列挙）
4. SIGMA Camera Control SDK の DMG をマウントし、Headers/ 配下のヘッダーを読んで PTP データ構造を `fp_l_tether/camera/ptp_codes.py` に転写
5. Phase 0-A → 0-B → 0-C 順に実機検証
6. 各ステップの結果（成功/失敗+全ログ）を `docs/PHASE0_LOG.md` に記録
7. 0-C成功 → Phase 1着手 / 失敗 → 第6章の分岐表に従い対応

---

## 10. ライセンス・配布方針

- **個人利用前提**（GitHubには公開しない or プライベートリポジトリ）
- **Sigma SDK のヘッダー情報をリバースしてopcode辞書化したコード**は配布注意。Sigma SDKは「同梱ドキュメント以外のサポートなし」と明示しているが、ヘッダー由来の値の再配布範囲は曖昧。OSSとして公開する場合は、オペコード値は別ファイルにし「Sigma SDK installation required」と明記する戦略を取る。
- 自分用ツールとしての利用に限定すれば法的リスクは実質ゼロ。

---

## 付録A: 抽出済みPTPオペコード一覧（実装で使う順）

| グループ | コード | 名称 | MVP使用 |
|---|---|---|---|
| 標準 | 0x1001 | GetDeviceInfo | △起動時のみ |
| 標準 | 0x1002 | OpenSession | ✅ |
| 標準 | 0x1003 | CloseSession | ✅ |
| Sigma | 0x9035 | ConfigApi | ✅ |
| Sigma | 0x902f | CloseApplication | ✅ |
| Sigma | 0x9039 | GetCamOpPermission | ✅ |
| Sigma | 0x9030 | GetCamCanSetInfo5 | △起動時 |
| Sigma | 0x9012-0x9027 | GetCamDataGroup1-5 | ✅ |
| Sigma | 0x901B | **SnapCommand** | ✅ |
| Sigma | 0x9015 | **GetCamCaptStatus** | ✅ |
| Sigma | 0x902d | **GetPictFileInfo2** | ✅ |
| Sigma | 0x9022 | **GetBigPartialPictFile** | ✅ |
| Sigma | 0x901C | **ClearImageDBSingle** | ✅ |
| Sigma | 0x9031/0x9032 | GetCamDataGroupFocus | ❌（MFのため不要） |
| Sigma | 0x9033/0x9034 | GetCamDataGroupMovie | ❌（静止画専用のため不要） |
| Sigma | 0x902B | GetCamViewFrame (LiveView) | ❌（要求なし） |

---

## 付録B: 参考リンク

- sigma-ptpy: https://github.com/makanikai/sigma-ptpy
- libgphoto2 Issue #882 (fp L サポート): https://github.com/gphoto/libgphoto2/issues/882
- SIGMA Camera Control SDK 配布告知: https://www.sigma-global.com/en/news/2020/07/02/10916/
- Apple ImageCaptureCore `requestSendPTPCommand:`: https://developer.apple.com/documentation/imagecapturecore/iccameradevice/1507967-requestsendptpcommand
- PyObjC ImageCaptureCore Bindings: https://pyobjc.readthedocs.io/en/latest/apinotes/ImageCaptureCore.html
- Lightroom Classic Auto Import公式: https://helpx.adobe.com/lightroom-classic/help/import-photos-automatically.html
- Sigma fp L Manual (FW v3.0): https://www.sigma-global.com/en/support/download/fp_L_Manual_FW_Ver.3.0_EN.pdf

---

## 改訂履歴

- 2026-05-11 v1.0: 初版（Cowork調査結果統合）
