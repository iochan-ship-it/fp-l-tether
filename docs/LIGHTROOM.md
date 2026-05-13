# Lightroom Classic 連携セットアップ

このアプリは 2 つの Lightroom 連携モードをサポートします。`config.toml` の `lightroom.mode` で切替。

---

## モード比較

| | Mode A: Auto Import 監視フォルダ | Mode B: セッションフォルダ直書き |
|---|---|---|
| 推奨度 | ★★★★★（デフォルト） | ★★★（撮影後にまとめて取り込みたい人向け） |
| 撮影直後の自動表示 | ✅ Loupe ビューに即反映 | ❌ 手動 Sync が必要 |
| Lightroom 起動が前提 | ✅ | ❌ |
| アプリ側の保存先 | `~/Pictures/Tether/_watch/` | `~/Pictures/Tether/Session_xxx/` |
| Lightroom 側の最終置き場 | LR が `Session_xxx/` へ移動 | アプリが直接書く |

---

## Mode A セットアップ（Auto Import 監視フォルダ方式）

### 1. アプリ側 config.toml

```toml
[lightroom]
mode = "watch"
watch_folder = "~/Pictures/Tether/_watch"
session_root = "~/Pictures/Tether"
use_atomic_write = true
```

### 2. Lightroom Classic を起動

### 3. Auto Import 設定を開く

メニューバーから:

**File → Auto Import → Auto Import Settings...**

### 4. 各項目を以下のように設定

| 項目 | 値 |
|---|---|
| **Watched Folder** | `~/Pictures/Tether/_watch`（左の Choose... ボタン）|
| **Destination Subfolder** | `Auto_Imported` または `{session}` テンプレ（任意） |
| **File Naming → Template** | `Filename`（アプリ側で命名済みのため） |
| **Information → Develop Settings** | スタジオ用のプリセット（任意） |
| **Information → Metadata** | 著作権テンプレ（任意） |
| **Information → Keywords** | `tethered, studio` などお好みで |
| **Initial Previews** | `Embedded & Sidecar` か `Standard`（速さ重視なら前者） |
| **☑ Don't Import Suspected Duplicates** | チェック ON 推奨 |
| **☑ Enable Auto Import** | チェック ON |

### 5. OK ボタンを押す

### 6. 動作確認

```bash
# 適当なテストDNGを置いてみる
cp ~/Pictures/SomeOldShot.dng ~/Pictures/Tether/_watch/test.dng
```

Lightroom 側で 1〜2 秒以内に取り込まれ、`_watch/` から消えれば成功。

### 7. 撮影時の Lightroom 操作

撮影開始前に:
1. ライブラリパネルの左 **Catalog** → **Previous Import** をクリック
2. キーボードの **E** で Loupe ビューに切替

撮影するたびに最新の DNG が大きく表示されます。

---

## Mode B セットアップ（セッションフォルダ直書き方式）

撮影後にまとめて Lightroom で取り込みたい場合。Lightroom を起動しなくても撮影できます。

### 1. アプリ側 config.toml

```toml
[lightroom]
mode = "session"
session_root = "~/Pictures/Tether"
use_atomic_write = true
```

### 2. 撮影後に Lightroom で取り込み

#### A. Import From Folder 方式

1. Lightroom → File → Import Photos and Video...
2. 左パネルで `~/Pictures/Tether/Session_YYYYMMDD_xxx/` を選択
3. 上部で **Add** を選択（移動・コピーしない）
4. Import ボタン

#### B. Folder Sync 方式（既存カタログにフォルダがある場合）

1. Library モジュール → 左パネルの Folders で `~/Pictures/Tether/` を右クリック
2. **Synchronize Folder...** を選択
3. **Import new photos** にチェック
4. Synchronize ボタン

---

## ファイル命名・整理

`config.toml` のテンプレで命名:

```toml
[output]
filename_template = "{session}_{item}_{shot:04d}.dng"
session_template = "{date}_{name}"
default_item = "untitled"
```

CLI で `--session ceramics --item jar` を渡すと:

```
~/Pictures/Tether/Session_20260512_ceramics/
  ├── 20260512_ceramics_jar_0001.dng
  ├── 20260512_ceramics_jar_0002.dng
  └── ...
```

Lightroom の Develop メタデータパネルの **File Name** がそのままアイテム識別子になるので、後で展示会・作品ごとにスマートコレクションで仕分けできます。

---

## ヒント

### スマートコレクション例

「2026年5月の陶器セッション」を自動コレクションに:

1. Library → 左パネル Collections の `+` → **Create Smart Collection**
2. Match: All
3. Rule 1: `Folder` contains `ceramics`
4. Rule 2: `Capture Date` is in the past `30` days

### キーワードの自動付与

`Auto Import Settings` の Keywords 欄に以下のような書き方が使えます:
- `tethered, studio, sigmafpl`
- セッションごとに変えたい場合は撮影セッション開始前に変更

### 設定が変わってしまった時の復旧

Auto Import が動かなくなった場合:
1. File → Auto Import → **Auto Import Settings** を開いて `☑ Enable Auto Import` が外れていないか確認
2. Watched Folder のパスが実在するか確認（パスがズレるとシレッと無効化される）
3. Lightroom を再起動

---

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| 撮影しても LR に何も入らない | Auto Import が無効 | Settings で Enable をチェック |
| ファイルが中途半端な状態で取り込まれる | atomic write が無効 | `use_atomic_write = true` を確認 |
| LR が「ファイルが見つからない」エラー | アプリと LR で異なるパスを参照 | `~` を絶対パスに展開して両方で同じパスを使う |
| Watched Folder が空でないと言われる | LR は監視フォルダにファイルが残るのを嫌う | アプリが書き込む直前に LR が拾うので通常起きないはず。残骸があれば手動削除 |
| LR が遅延して撮影テンポと合わない | プレビュー生成が重い | `Initial Previews = Embedded & Sidecar` に変更 |
