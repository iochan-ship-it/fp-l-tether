# Sigma fp L テザー撮影自作プロジェクト — Cowork引き継ぎドキュメント

## このプロジェクトの目的

Sigma fp L（61MP、Sigma独自のPTP実装）で macOS への USB テザー撮影ワークフローを自作する。完成イメージ：

```
Sigma fp L (USB-C, Camera Controlモード)
    ↓ sigma-ptpy (Python/PTP)
自作Pythonスクリプト（シャッター検知 → DNG取得）
    ↓ ファイル保存
Lightroom Classic 監視フォルダ (~/Pictures/Tether/Session_YYYYMMDD/)
    ↓ Auto Import
Lightroom Classic カタログに自動取り込み → 即確認・現像
```

**ゴール**：USB抜き差しゼロで撮影し続けながら、撮影直後にMacのLightroomで確認できる状態を作る。

---

## ユーザー（IO）の制約と背景

- **用途**：屋内スタジオでの陶器・絵画の静止画撮影のみ（動画・屋外不要）
- **カメラ**：Sigma fp L（61MP、DNG約100MB/枚）
- **Mac環境**：macOS（最新版）+ Lightroom Classic（継続使用前提）
- **予算**：Capture One（月$14.9〜）より安いこと → **完全無料が理想**
- **ライブビュー**：不要（撮影後に確認できればOK）
- **フォーカス制御**：不要（マニュアルフォーカス前提）

### 既存のClaude Code開発スタック
- household-budget（Plaid API、日本語UI、6桁PIN認証）
- class schedule Webアプリ（Node.js + Next.js + SQLite + Supabase）
- CLAUDE.md によるセッション継続管理を確立済み

→ **同じ開発スタイル（Python + Claude Code + CLAUDE.md）でこのプロジェクトも進める**

---

## なぜCapture One/SDカードリーダーではダメだったか（経緯）

### Capture One却下の理由
- サブスク（月$14.9）を払いたくない
- 既知の不具合：接続のたびにカメラ設定（露出・WB）がリセットされる
- 買い切り版$299は予算オーバー

### SDカードリーダー運用却下の理由
- Sigma fp Lは**アルカプレートを付けるとSDカードスロットの蓋が物理的に開けられない**
- 三脚固定撮影でアルカプレートを外すたびにキャリブレーションが崩れる
- → SDカード抜き差し運用は実質不可能

### Mass Storageモード却下の理由
- Mac側でマウントされている間、**カメラ側で撮影できない**仕様（USB Mass Storage Class排他制御）
- これが「毎回USBを抜き差ししている真の原因」だった
- USB端子の物理摩耗が長期リスク

### 結論：Camera Controlモード + sigma-ptpy自作

---

## 技術調査の結果（重要事実）

### Sigma fp LのUSBモード（公式マニュアルより）
3つの選択肢：
- **Mass Storage**：SDカードを外付けドライブとしてMacに見せる（マウント中撮影不可）
- **Video Class (UVC)**：Webカメラとして動作
- **Camera Control**：テザー撮影ソフトでカメラを操作 ← **これを使う**

**重要な制約**：
> 「USB mode cannot be changed while the camera is connected to your computer. Be sure to set the [USB mode] you require before connecting it to your computer.」

USB接続中にモード変更不可。接続前に設定を完了させる必要がある。

### Camera Controlモードの実態（IOが現場で確認済み）
- ✅ USB挿しっぱなしで撮影継続可能
- ✅ SDカードに画像保存される
- ❌ Macには何も自動転送されない（PTPプロトコルでカメラを「見えるようにする」だけ）
- → **DNGをMacに取り込むには、PTP対応ソフトが別途必要**

### Sigma fp L対応ソフトの現状（2026年5月時点）
- **Capture One Pro**：唯一の商用対応ソフト、ただし月$14.9〜＋設定リセットバグ
- **Dragonframe 5.x**：対応するが$325、静物用途には過剰
- **gphoto2/darktable**：libgphoto2 2.5.31でfp L IDは登録済みだが、撮影が不安定（撮影画像のクローン返却バグ等が未解決）
- **Lightroom Classicネイティブテザー**：Sigma非対応
- **Smart Shooter / Helicon Remote / Sofortbild / Cascable**：全てSigma非対応

### sigma-ptpy の現状
- GitHub: https://github.com/makanikai/sigma-ptpy
- MIT License、Star数約7
- **fp前提のプロジェクトで、fp Lでの動作報告がリポジトリに少ない**
- → **動作検証が最優先タスク**

---

## 想定アーキテクチャ

```
sigma-tether/
├── tether.py              # メインCLI（ポーリングループ）
├── camera.py              # sigma-ptpy ラッパー
├── transfer.py            # DNG受信＆Lightroom監視フォルダへ保存
├── config.toml            # 保存先・命名規則・カメラ設定
├── CLAUDE.md              # Claude Codeセッション継続用
├── requirements.txt       # sigma-ptpy, libusb等
├── test_camera.py         # 動作検証スクリプト（最初に実行する）
└── README.md
```

### MVP の機能仕様
1. `python tether.py` でCLI起動
2. カメラ接続を検知 → 「監視開始」表示
3. シャッターを切る → 数秒以内に自動検知
4. カメラからDNG取得 → `~/Pictures/Tether/Session_YYYYMMDD_HHMMSS/` に保存
5. Lightroom Classic の Auto Import がそのフォルダを監視 → カタログに自動取り込み
6. Ctrl+C で監視終了

### 将来の拡張（MVP後）
- macOSメニューバーアプリ化（rumps等）
- 撮影セッション管理UI
- ファイル名カスタマイズ（陶器名、作品番号等のメタデータ付与）
- Lightroom側プリセット自動適用との連携

---

## 最重要：フェーズ0（動作検証）から開始

**本格開発に入る前に、sigma-ptpyがfp Lで動くかを必ず検証する。** 動かなければ方針転換が必要。

### 環境準備（macOSターミナル）

```bash
mkdir -p ~/Projects/sigma-tether
cd ~/Projects/sigma-tether

python3 -m venv venv
source venv/bin/activate

brew install libusb
pip install sigma-ptpy
```

### カメラ準備

1. Sigma fp L の電源OFF、USBケーブル**未接続**の状態にする
2. CINE/STILLスイッチを **STILL** に
3. カメラ電源ON
4. MENU → システム → **USB mode を Camera Control** に設定
5. カメラ電源OFF
6. USBケーブルで Mac に接続
7. カメラ電源ON

### 検証スクリプト（test_camera.py）

5段階のテストを実行する：
1. **Import**：sigma-ptpyライブラリの読み込み
2. **Connection**：カメラへのPTPセッション確立
3. **Camera Info**：機種名・シリアル・FW・現在の露出設定取得
4. **Snap**：実際にシャッターを切る（Enter押下で実行）
5. **Image Download**：撮影したDNGをMacに転送し `/tmp/sigma_test.dng` に保存

スクリプトの全文は別ファイル `test_camera.py` を参照（IOが用意済み）。

### 想定される結果と分岐

**パターンA：全ステップ成功** ✅
→ そのまま本格開発フェーズへ。MVPアーキテクチャに沿ってtether.py本体を実装する。

**パターンB：接続はOKだが撮影/取得でエラー** ⚠️
→ fp と fp L で API/OpCode に差異がある可能性。sigma-ptpyのソースコードを読んで該当箇所を特定し、fp L 用にパッチを当てる。

**パターンC：そもそも接続不可** ❌
→ 方針転換が必要：
- Option 1: SIGMA Camera Control SDK（C++）を直接使う自作（Objective-C++ブリッジ、開発コスト大）
- Option 2: Capture One年契約に妥協
- Option 3: Mass Storageモード + マグネット式USB-Cアダプタ（$20〜30）で現状フローのUSB端子保護のみ強化

---

## 既知のリスク・落とし穴

### sigma-ptpyのfp L対応未確認
リポジトリは**fp**前提。fp Lでの動作報告がほぼ存在しない。fpとfp Lでは61MPセンサー対応で内部プロトコルが異なる可能性。

### macOSのUSB権限問題
sigma-ptpyはlibusbを通じて低レベルUSB通信を行う。macOSのセキュリティで権限を要求される可能性あり：
- `sudo`での実行が必要かもしれない
- 「システム設定 → プライバシーとセキュリティ」での許可が必要かもしれない

### DNGファイルサイズ（約100MB/枚）
- USB 3.1 Gen 1（5Gbps）の実効転送速度では1枚あたり2〜5秒程度
- 連写には不向き、陶器撮影の数秒〜十数秒間隔の撮影なら問題なし

### Camera Controlモードでの設定リセット
fp LはCapture One接続でも設定リセットが発生する既知不具合。sigma-ptpyでも同じ可能性があるため、撮影開始時の設定確認フローを組み込む必要があるかもしれない。

### libusbのPython binding（PyUSB）
sigma-ptpyの依存関係。インストール時にlibusbのシステムライブラリが先に必要：
```bash
brew install libusb
```
これを忘れるとpip installで失敗する。

---

## Coworkに依頼したいこと（タスク一覧）

### Phase 0: 動作検証（必須・最優先）
1. ✅ `~/Projects/sigma-tether/` 作業ディレクトリを作成
2. ✅ Python仮想環境構築
3. ✅ libusb、sigma-ptpy インストール
4. ✅ `test_camera.py` を実行
5. ✅ 結果（成功・失敗・エラー全文）を記録

### Phase 1: MVP実装（Phase 0が成功した場合）
6. 上記アーキテクチャに沿って `tether.py` `camera.py` `transfer.py` を実装
7. `config.toml` で保存先・命名規則を設定可能に
8. `CLAUDE.md` を作成してセッション継続管理
9. 実際に陶器・絵画撮影で動作確認
10. Lightroom Classic の Auto Import 設定手順をドキュメント化

### Phase 2: 安定化・拡張（任意）
11. エラーハンドリング強化（USBケーブル抜けた、カメラ電源OFF等）
12. macOSメニューバーアプリ化
13. 撮影セッションUI

---

## 補足情報

### Sigma fp L公式SDK
- 名称：SIGMA Camera Control SDK for Mac
- C++ヘッダー、ライブラリ、サンプルmacOSプログラムが配布物
- 2020年公開、以降ほぼ更新なし
- 配布URLの一部が404になっている
- Sigma側は「同梱ドキュメント以外のサポートなし、コーダー向け」と明言
- **Pythonからの利用は不可、Swift/Objective-C++経由のみ**
- sigma-ptpyがダメだった場合の最終手段

### 参考リンク
- sigma-ptpy: https://github.com/makanikai/sigma-ptpy
- Sigma fp/fp L 公式マニュアル: https://www.sigma-global.com/en/support/
- Lightroom Classic Auto Import: `File → Auto Import → Auto Import Settings`

### IO の好み・スタイル
- 指示は「失敗しにくい超詳細版」に分解（クリック対象・確認ポイントを厳密に）
- 出力は日本語・英語併用（コードは英語）
- Claude Codeの`CLAUDE.md`セッション継続を活用
- household-budget、class schedule等と同じ開発パターン

---

## 次のアクション

**Coworkで `test_camera.py` を実行し、その結果を見て次の判断をする。**

成功すれば本格開発、失敗すればパターンB/Cの方針転換。検証結果のエラーメッセージは全文（tracebackを含む）を保存しておくこと。
