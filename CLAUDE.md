# CLAUDE.md — fp-l-tether プロジェクト継続コンテキスト

このファイルは Claude Code セッションが切れても次のセッションで即座にフルコンテキストを取り戻すためのもの。**最初に読むべき**ファイル。

---

## プロジェクト概要

- **名前**: fp-l-tether
- **目的**: Sigma fp L (USB-C) → Mac → Lightroom Classic の自作テザー撮影アプリ
- **状態**: Phase 0 (動作検証) 待機中
- **ユーザー**: IO（@piartcenter）。Python経験は household-budget / class-schedule 相当
- **本プロジェクトの最重要文書**: `DESIGN_PLAN.md` (このフォルダ内)

## 採用スタック（決定済み・変更注意）

- **Python 3.11**
- **PyObjC + Apple ImageCaptureCore** ← カメラ通信
- **生PTPオペコード** ← Sigma SDK ヘッダーから抽出済 (`fp_l_tether/camera/ptp_codes.py`)
- **CLI**: Typer / **メニューバー**: rumps
- **設定**: TOML / **ログ**: 構造化 JSON + text
- **依存**: `pyobjc-framework-ImageCaptureCore`, `typer`, `rumps`, `pydantic`, `tomli`

理由は `DESIGN_PLAN.md` 第0章を参照。要約: Apple純正経路で最も安定、libusb/sudo 不要、既存 Python スタックと統一、長期保守可能。

## なぜ sigma-ptpy / libgphoto2 を採用していないか

- **sigma-ptpy** (makanikai/sigma-ptpy): Stars 7、fp 専用、fp L 動作報告なし、libusb 経由で macOS 権限問題リスク
- **libgphoto2 2.5.31**: fp L で「3枚目から PTP General Error」「画像クローン返却バグ」が未解決 (Issue #882 参照)
- **Capture One**: 月額 + 設定リセットバグ
- **SIGMA SDK 直接利用**: Swift/Obj-C 必須で Claude Code 反復に重い

詳細は `DESIGN_PLAN.md` 第1章。

## 開発ワークフロー

```bash
# セットアップ (初回のみ)
cd "/Users/PI/Documents/Claude/Projects/FP L Tether APP"
python3 -m venv venv
source venv/bin/activate
pip install -e .
# または: pip install -r requirements.txt

# Phase 0 検証 (カメラ接続後)
python scripts/phase0_smoke_test.py     # Step A: 列挙のみ
python scripts/phase0_session_test.py   # Step B: PTPセッション + GetCamCaptStatus
python scripts/phase0_snap_test.py      # Step C: フルフロー
```

## ディレクトリ構成

```
.
├── CLAUDE.md                    # このファイル（最初に読む）
├── DESIGN_PLAN.md               # 設計の正典
├── sigma-tether-handoff.md      # 当初リサーチ結果
├── README.md                    # 利用者向け概要
├── pyproject.toml               # 依存・パッケージ定義
├── config.example.toml          # 設定テンプレ
├── config.toml                  # 実設定（.gitignore対象）
├── .gitignore
├── fp_l_tether/                 # 本体パッケージ
│   ├── camera/                  # PyObjC + PTP
│   │   ├── ptp_codes.py         # SDKから抽出した定数・構造体
│   │   ├── ic_bridge.py         # ImageCaptureCore ラッパー
│   │   └── ptp_packets.py       # パケット組み立て・チェックサム
│   ├── transfer/                # ファイル取得・命名・atomic write
│   ├── lightroom/               # 監視フォルダ vs セッションフォルダ
│   ├── ui/                      # CLI + menubar
│   ├── config/                  # Pydantic設定モデル
│   └── telemetry/               # 構造化ログ
├── scripts/                     # Phase 0 検証スクリプト
│   ├── phase0_smoke_test.py
│   ├── phase0_session_test.py
│   └── phase0_snap_test.py
├── docs/
│   ├── SETUP.md                 # macOS 準備手順
│   ├── LIGHTROOM.md             # Lightroom Auto Import 手順
│   ├── PHASE0_LOG.md            # 動作検証の結果記録
│   ├── PTP_TRACE.md             # PTPトレース記録 (撮影成功時のbyte列)
│   ├── TROUBLESHOOTING.md
│   └── COMPATIBILITY.md         # FW別動作報告
└── tests/
    ├── unit/                    # CIで通すpure-Pythonテスト
    └── integration/             # 実機テスト (CIではskip)
```

## 重要な PTP オペコード一覧（実装で使う順）

| 用途 | コード | Sigma独自? | MVP必須 |
|---|---|---|---|
| OpenSession | 0x1002 | 標準 | ✅ |
| CloseSession | 0x1003 | 標準 | ✅ |
| ConfigApi | 0x9035 | Sigma | ✅ (起動時) |
| GetCamOpPermission | 0x9039 | Sigma | ✅ (起動時) |
| GetCamCanSetInfo5 | 0x9030 | Sigma | △ |
| GetCamDataGroup1-5 | 0x9012-0x9027 | Sigma | ✅ |
| **SnapCommand** | **0x901B** | Sigma | ✅ |
| **GetCamCaptStatus** | **0x9015** | Sigma | ✅ (ポーリング) |
| **GetPictFileInfo2** | **0x902d** | Sigma | ✅ |
| **GetBigPartialPictFile** | **0x9022** | Sigma | ✅ |
| **ClearImageDBSingle** | **0x901C** | Sigma | ✅ |
| CloseApplication | 0x902f | Sigma | ✅ (終了時) |

データ構造の正確な定義は `fp_l_tether/camera/ptp_codes.py` を参照。SDKヘッダーから抽出済み。

## Phase 0 検証の分岐ロジック

```
Step A (smoke)  → カメラ列挙 → fp Lが見える?
  ├─ NO  → USBモード/ケーブル/プライバシー設定確認
  └─ YES → Step B

Step B (session) → OpenSession + ConfigApi + GetCamCaptStatus 1往復
  ├─ FAIL → 別アプリ(Capture One/Image Capture)が掴んでないか確認
  │         → 撃ち落とせない場合: ConfigApiバージョン交渉のシーケンス変更
  │         → それでもダメ: sigma-ptpy ルートへフォールバック
  └─ OK   → Step C

Step C (snap) → SnapCommand → ポーリング → DL → クリア → 5枚連続テスト
  ├─ クローン返却bug発生 → ClearImageDBSingle必須化、ImageID差分検証
  ├─ DL失敗 → GetBigPartialPictFileのチャンクサイズ調整
  └─ 全成功 → Phase 1 着手
```

検証結果は `docs/PHASE0_LOG.md` に追記する。

## Lightroom 連携の2モード

- **Mode A (watch)**: アプリは `~/Pictures/Tether/_watch/` に atomic rename。LR Auto Import が拾う。
- **Mode B (session)**: アプリは `~/Pictures/Tether/Session_YYYYMMDD_xxx/` に直接保存。LRは手動Sync。

`config.toml` の `lightroom.mode = "watch"` または `"session"` で切替。両方実装する方針。

## コーディング規約

- 型ヒント必須 (mypy strict 推奨)
- async/await はカメラI/Oが安定したら導入。Phase 0/1.0 は threading + Queue で十分
- ログは `from fp_l_tether.telemetry.logger import logger` 経由で統一
- 文字列はユーザー向けは日本語、ログ・コード内コメントは英語と日本語併用OK
- 関数は1機能 = 1ファイル原則
- Sigma 固有の値はすべて `ptp_codes.py` に集約 (マジックナンバーをコードに散らさない)

## 既知の落とし穴

1. **fp L 設定リセット症状**: Capture One接続でも報告あり。起動時に GetCamDataGroup1-5 を保存しておき、設定が変わったら警告を出す
2. **PTP 3枚目から失敗 (gphoto2 由来)**: Sigma 公式 SDK 経路で再現する可能性。`ClearImageDBSingle` を毎回呼ぶことで回避する仮説
3. **Lightroom が partial file を取り込む**: atomic rename (tmp → final) 必須。拡張子は最後に `.tmp` → `.dng` rename
4. **ImageCaptureCore の delegate**: PyObjC で delegate を扱う場合 NSObject サブクラスを作る必要あり。runloop は `CFRunLoopRunInMode` で短時間回す

## 過去の決定ログ

- 2026-05-11: PyObjC + ImageCaptureCore を採用 (DESIGN_PLAN.md v1.0)。理由は前述
- 2026-05-11: Lightroom 連携は両モード対応で進める (IO 選択)
- 2026-05-11: MVP スコープは「シャッター制御 + 自動検知 + DNG転送」を必須、その他もできる限り含む (IO 選択)

## 次のアクション

1. ⬜ `pip install -e .` で依存をインストール
2. ⬜ Sigma fp L を Camera Control モードに設定して USB 接続
3. ⬜ `python scripts/phase0_smoke_test.py` を実行
4. ⬜ 結果を `docs/PHASE0_LOG.md` に追記
5. ⬜ Step B / Step C に進む

## 参考リンク（変わらないもの）

- Apple ImageCaptureCore: https://developer.apple.com/documentation/imagecapturecore
- PyObjC ImageCaptureCore: https://pyobjc.readthedocs.io/en/latest/apinotes/ImageCaptureCore.html
- Sigma SDK 配布告知: https://www.sigma-global.com/en/news/2020/07/02/10916/
- sigma-ptpy (フォールバック用): https://github.com/makanikai/sigma-ptpy
- libgphoto2 fp L Issue: https://github.com/gphoto/libgphoto2/issues/882

---

**Claude Code を開いたら、まずこのファイルと `DESIGN_PLAN.md` を読むこと。**
