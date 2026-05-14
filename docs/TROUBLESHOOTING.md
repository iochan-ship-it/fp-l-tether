# トラブルシューティング

困ったときの早見表。新しい症状を見つけたら追記してください。

---

## 接続まわり

### `No cameras detected` (Phase 0-A で失敗)

1. **USB モードが "Mass Storage" になっている** → 設定し直す（接続前に変更）
2. **Camera Control モードだが他アプリが掴んでいる** → イメージキャプチャ.app / 他のテザー / Lightroom Tether 終了
3. **USB ケーブルが充電専用** → データ通信対応 USB-C ケーブルを使う
4. **USB ハブ経由で電力不足** → Mac に直結、または給電付きハブ
5. **macOS のプライバシー許可未付与** → 設定 → プライバシーとセキュリティ → カメラ で Terminal / Claude Code を ON

### カメラは見えるが PTP 能力なし

```
PTP capable: NO
```

- カメラ FW が古い → 公式サイトから v3.0 以降に更新
- USB モードが Camera Control 以外（Mass Storage / UVC）
- カメラが正常に PC 接続モードに入っていない（一度電源 OFF→ON）

### `Session open failed` / `Timed out waiting for session open callback`

ImageCaptureCore がカメラとセッション交渉に失敗。

- 別アプリが先にセッションを掴んでいる
- カメラの操作中（メニュー操作中・録画中など）→ STILL モードでスタンバイ状態にする
- USB ケーブル一旦抜いて挿し直し

---

## PTP コマンドまわり

### `0x2005 OPERATION_NOT_SUPPORTED`

- ConfigApi の API バージョン交渉が未完了 → 起動時に必ず `CONFIG_API` (0x9035) を呼ぶ
- カメラが「PC コントロールモード」に入ってない → `GET_CAM_OP_PERMISSION` (0x9039) のレスポンスを確認

### `0x2002 GENERAL_ERROR`

最も曖昧なエラー。libgphoto2 では fp L の 3 枚目以降で頻発。

- 直前の `ClearImageDBSingle` がスキップされている → 必ず呼ぶ
- `GetCamCaptStatus` のポーリングが速すぎる → 200ms 間隔以上
- カメラ内 ImageDB がフル → カメラの電源 OFF/ON でリセット

### `Timed out waiting for PTP response`

- カメラが応答返さずに固まっている → 電源 OFF/ON
- USB 帯域不足（USB 2.0 ハブ経由） → USB 3.0+ で直結

---

## 撮影画像まわり

### DNG ファイルがおかしい（サイズが小さい・開けない）

- `GetBigPartialPictFile` のチャンク取得が途中で止まった
- `GetPictFileInfo2` から取得した `expected_size` が不正
- `in_data` 先頭にヘッダーが含まれている可能性 → 先頭バイトを `hex_dump` で確認し、 0x49 0x49 (II) または 0x4D 0x4D (MM) で始まらないなら別途オフセット調整

### 連写すると 2 枚目以降が 1 枚目のクローン (libgphoto2 #882 と同症状)

- `ClearImageDBSingle` を毎回必ず呼ぶ
- `GetCamCaptStatus` で前回と異なる `ImageID` を待つ
- それでも再現する場合: `SnapCommand` の `CaptureMode` を変えて試す (`0x02` → `0x06`)

### 撮影が遅い

fp L の DNG は ~100MB。USB 3.1 Gen 1 (5Gbps) の実効速度で 1 枚あたり 2〜5 秒程度が想定どおり。

10 秒以上かかる場合:
- USB 2.0 ハブを経由している
- ケーブルが USB 2.0 グレード
- `chunk_size` を増やす (`config.toml` の `camera.chunk_size`)

---

## Lightroom まわり

### 撮影しても Lightroom に何も入らない

1. Lightroom Classic が起動しているか
2. `File → Auto Import → Auto Import Settings` で `☑ Enable Auto Import` が ON か
3. Watched Folder のパスがアプリ側 `config.toml` の `watch_folder` と一致しているか
4. `~` を絶対パスに展開して両方で同じパスを使う
5. Lightroom 再起動

### Lightroom が中途半端な画像を取り込む

`config.toml` で `lightroom.use_atomic_write = true` になっているか確認。

### Photos.app が勝手に起動する

イメージキャプチャ.app を起動 → 左下のアプリ選択を「アプリケーションなし」に変更。

---

## 設定リセット問題（fp L 既知）

USB 再接続のたびに「シャッタースピード・WB・露出が初期値に戻る」現象が
fp L 本体側の挙動として知られています。本アプリは persistent settings
cache を使って (再)接続時に直前の値を書き戻しますが、念のため:

回避:
- `config.toml` の `debug.detect_setting_reset = true` で警告を有効化
- 撮影開始前に手動で再確認する習慣を

---

## ログの場所

- `~/Library/Logs/fp-l-tether/tether.log` — 人間可読
- `~/Library/Logs/fp-l-tether/events.jsonl` — 機械可読
- `/tmp/fp_l_phase0_dng/` — Phase 0-C のテスト DNG

ログを Claude Code に貼って相談すれば原因を一緒に追えます。
