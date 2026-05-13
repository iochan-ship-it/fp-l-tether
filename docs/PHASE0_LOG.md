# Phase 0 動作検証ログ

実機で Phase 0 検証を行うたびに、このファイルに結果を追記する。失敗時は完全なエラーログを残しておくこと（Claude Code で次回トラブル解析するため）。

---

## 🎯 2026-05-12 17:41 — Phase 0-C v3 **[CLEAR]** — 5/5 連射成功

- カメラ FW: fp L 1.00
- macOS: 26.3.1
- Python: 3.13 (venv) + libusb 1.0 (brew)
- 接続: 直結 USB-C

### 実行コマンド
```
sudo venv/bin/python scripts/phase0_capture_v2.py --shots 5 --gap 2 --poll-iter 100
```

### 結果（要約）

```
Shot 1: slot 0x00 → image_id=0x00 → 26,755,708 B → SDIM0001_01.JPG (5.89s, 4.3 MB/s)
Shot 2: slot 0x01 → image_id=0x01 → 26,794,268 B → SDIM0002_02.JPG (4.97s, 5.1 MB/s)
Shot 3: slot 0x02 → image_id=0x02 → 26,736,892 B → SDIM0003_03.JPG (5.08s, 5.0 MB/s)
Shot 4: slot 0x03 → image_id=0x03 → 26,742,812 B → SDIM0004_04.JPG (5.06s, 5.0 MB/s)
Shot 5: slot 0x04 → image_id=0x04 → 26,844,668 B → SDIM0005_05.JPG (4.87s, 5.3 MB/s)

✓ all shot sizes are unique — no obvious clone-return bug
SUCCESS — all 5 shot(s) captured & saved
```

### 解決のキー — 5 つの決定的インサイト

これは数日間、libgphoto2 / Sigma SDK / sigma-ptpy / libusb 直叩きと戦ってたどり着いた結論。

1. **`GetCaptureStatus(p1=N)` はスロット N の状態を返す（グローバル状態ではない）。**
   shot 1 は db_head=0 だから p1=0 でたまたま動くが、shot 2 以降は db_head が advance してるので p1=0 では永遠に 0x0000。**正解: `pre_status.image_db_head` の値を p1 に渡す**。libgphoto2 の `camera_sigma_fp_capture` も p1=0 固定で同じバグを持ってる。

2. **`SetDataGroup3 (0x9018)` を init で送って PC capture mode を有効化。**
   wire: `03 00 80 02 00 00 ... 00 85` (21B + 1B sum checksum)。
   FieldPresent1=0x03, DestinationToSave=0x80 (PC+card)。
   libgphoto2 master の `cameras/sigma-fp.txt` reverse-engineered トレースから抽出。

3. **Snap mode = 2 (NON_AF_CAPTURE)。**
   libgphoto2 の `ptp_sigma_fp_snap(params, 1, 1)` は mode=1 だが誤り。
   Sigma SDK ヘッダの NON_AF_CAPTURE=0x02 が正解で fp 実機トレースもこれを使用。
   Wire bytes: `02 02 01 05` (length=2, mode=2, amount=1, chk=5)。

4. **GetCaptureStatus の wire 応答は 8 バイト（libgphoto2 は 7 バイト解釈で off-by-one）:**
   `[0x06 length, imageid, dbhead, dbtail, status_lo, status_hi, dest, chk]`
   chk = sum(data[0..6]) & 0xFF。

5. **`brew install gphoto2` の CLI は fp L で即死。**
   原因: `camera_init` 内 `ptp_sigma_fp_9035` が関数内部で `free(*data)` し、caller も `free(xdata)` する double-free。
   macOS のクラッシュレポートで `___BUG_IN_CLIENT_OF_LIBMALLOC_POINTER_BEING_FREED_WAS_NOT_ALLOCATED` を確認済。
   Python ネイティブ実装の方が圧倒的に安定。

### 観察

- ダウンロード時のバイト数が報告 `filesize` より約 25-30 バイト多い。USB ZLP padding か camera trailer。JPEG は末尾余分バイトを無視するので無害。
- camera ID は `0, 1, 2, 3, 4...` と単純に増える（ring buffer 巻き返しは未到達）。
- 4-5 MB/s で安定転送（理論値 USB 2.0 ≒ 35 MB/s に遠いが PTP 経由はこんなもの）。

### 副次的に動いた / 必要だった

- カメラの USB モード = "Camera Control"
- `sudo killall ptpcamerad` (即 respawn するが取得タイミングをずらせる)
- `sudo venv/bin/python` (libusb 経由のため特権必要)

### Phase 0 全体結論

**完全に Phase 0-C クリア。Phase 1（CLI ラッパー + menubar + Lightroom Auto Import 連携）に進む条件が揃った。**

---

## テンプレート（コピーして使う）

```
## YYYY-MM-DD HH:MM — Phase 0-? [PASS/FAIL]

- カメラ FW: vX.X
- macOS: XX.X
- Python: 3.11.X
- pyobjc: X.X
- 接続: 直結 / ハブ経由 / どっち?
- 他のアプリ起動状況: なし / xxx

### 実行コマンド
`python scripts/phase0_X_test.py`

### 結果
（コンソール出力をそのまま貼る）

### 観察
- 何が見えた
- 何が想定外だったか

### 次のアクション
- 次のステップ
- 修正が必要なファイル

---
```

---

## 検証結果の蓄積

<!-- 以下にPhase 0実行結果を追記していく -->

### 2026-05-11 — Phase 0-A [PASS]

- カメラ FW: (未確認、要確認)
- macOS: 26.3.1 (arm64, Apple Silicon)
- Python: 3.14.5
- pyobjc: 12.1
- 接続: USB-C 直結
- 他のアプリ起動状況: なし (Image Capture.app は終了済み)

#### 実行コマンド
`python scripts/phase0_smoke_test.py`

#### 結果
- Detected: SIGMA fp L
- Vendor ID: 0x1003
- Product ID: 0xC442 (fp L)
- Serial Number: 0000000091508851
- Transport: ICTransportTypeUSB
- PTP capable: YES ✓
- Take picture API: NO (これは Apple の高レベル API のフラグなので問題なし。
  PTP pass-through (requestSendPTPCommand) は別の capability で動く)

#### 観察
- 初回実行では No cameras detected。原因: macOS 14 Sonoma 以降は
  `ICDeviceBrowser.browsedDeviceTypeMask` に Device type bits だけでなく
  Device location bits (`ICDeviceLocationTypeMaskLocal = 0x100`) を OR
  しないと local USB カメラがフィルタされる。修正済 (ic_bridge.py)。
- 修正後は一発で検出。

#### 次のアクション
- Phase 0-B (phase0_session_test.py) に進む。

---

### 2026-05-11 21:00 — Phase 0-B [PASS (with caveat)]

- 同じ環境
- 他のアプリ起動状況: なし (Image Capture.app 終了済み)

#### 実行コマンド
`python scripts/phase0_session_test.py`

#### 結果

```
Step 1: Opening PTP session via ImageCaptureCore...
  ✓ Session opened.
  Note: didRemoveDevice WARNING fired immediately after open
        — likely the browser stopping after we open the session,
          not an actual disconnect. Session continued working.

Step 2: ConfigApi (0x9035)...
  Response code: 0x2001 (OK)
  in_data       : 01 00 00 00 01 00 01 00

Step 3: GetCamOpPermission (0x9039)...
  Response code: 0x2001
  in_data       : 01 00 00 00 01 00 01 00

Step 4: GetCamCaptStatus (0x9015)...
  Response code: 0x2001 (OK)
  in_data (8 bytes): 01 00 00 00 01 00 01 00
  Trying offset=0: SgmCaptStatus(image_id=0x01, db_head=0x00, db_tail=0x00, capt_status=0x0100, dest=0x00)
  Trying offset=1: SgmCaptStatus(image_id=0x00, db_head=0x00, db_tail=0x00, capt_status=0x0001, dest=0x01)
```

#### 観察
- 3 つの異なる opcode が**全く同じ 8 バイト**を in_data に返している
  → これは怪しい。可能性は 3 つ:
    a) Apple ImageCaptureCore の `inData` は実は data phase ではなく、
       PTP response container の parameters を返している
       → `01 00 00 00 01 00 01 00` は parameter1=0x01, parameter2=0x00010001 と解釈できる
    b) Standby 状態のデフォルトレスポンスが偶然同じ
    c) PyObjC バインディングのバグで毎回同じバッファを返している

- どれが正しいかは **Phase 0-C で SnapCommand を撃って ImageID が変化するか**
  を見ればすぐ判明する

- 私の `CaptStatusCode` enum (ptp_codes.py) は値を推測で書いたので、
  実機トレースに合わせて要修正の可能性大

#### 次のアクション
- Phase 0-C (phase0_snap_test.py) で実際にシャッターを撃ち、in_data の
  バイト列がイベント駆動で変化することを確認する。
- 変化すれば → パーサーを実トレースに合わせて調整
- 変化しなければ → ic_bridge.py の `send_ptp` の in_data 読み出しロジックを
  根本から見直す (delegate の inData ではなく response container の解析に切り替え)

---

### 2026-05-11 21:08 — Phase 0-C [FAIL]

#### 実行コマンド
`python scripts/phase0_snap_test.py`

#### 結果
- 5 ショット全失敗
- Shot 1-4: `ICBridgeError: Timed out waiting for PTP response (opcode=0x901B)`
- Shot 5: `RuntimeError: SnapCommand failed: 0x0000` (タイムアウトではなく、
  応答コード 0x0000 が返ってきた = カメラが degraded state に陥った)
- 物理シャッター音は確認できず（要確認）

#### 観察
- `SnapCommand outData = 02 01 03` (3バイト, length prefix なし) で送っていた
- libgphoto2 #882 の Windows SDK トレースを再解析:
  ```
  [Data] 0x00,0x00,0x00,0x00,0x04,0x00,0x00,0x00,0x02,0x02,0x01
  [CheckSum] 0x05
  ```
  → 先頭の `04 00 00 00` は **4バイト長プレフィックス** (uint32 LE = 4)
  → Sigma 独自プロトコルは PTP データ位相の前に長さフィールドを要求
- 5 ショット目で `response_code=0x0000` が返ってきたのは
  カメラがハーフ状態（USB スタックに不整合）になった示唆

#### 適用した修正
- `ptp_codes.py`:
  - `SgmSnapState.to_wire_outdata()` を追加（4バイト長プレフィックス付き）
  - `SNAP_SINGLE_STILL_PAYLOAD` を `03 00 00 00 02 01 03` に更新
  - `wrap_sigma_outdata()` ヘルパー追加
- `scripts/phase0_snap_test.py`:
  - `NUM_SHOTS = 1` に変更（一発成功してから増やす）
- `scripts/phase0_physical_test.py` を新規作成
  - SnapCommand を使わず、物理シャッター押下後の状態変化を観察するための診断ツール
  - GetCamCaptStatus 差分、GetPictFileInfo2 のレスポンスを `/tmp/fp_l_phase0_physical/` に保存

#### 次のアクション
1. カメラを電源 OFF → 5秒待機 → ON で degraded state リセット
2. `python scripts/phase0_physical_test.py` を実行
   → SnapCommand を介さずにダウンロード経路だけ単独検証
   → ImageID が変化するか、GetPictFileInfo2 がデータを返すかを確認
3. 上記の結果次第:
   - 変化検出&データ取得 OK → SnapCommand 単独の問題 → snap_test 再試行
   - 変化検出 NG → in_data の読み出し方を根本見直し

---

### 2026-05-11 21:17 — Phase 0-C diagnostic (physical_test) [FAIL — but reveals key info]

#### 実行コマンド
`python scripts/phase0_physical_test.py`

#### 結果
- セッションは正常に開いた
- Warmup: `0x9035`, `0x9015` 共に `response=0x2001`, `in_data=00 00 00 00` (4バイト)
- ベースライン: `in_data (4 bytes): 00 00 00 00`
- 物理シャッターを押した状態で 110 ポーリング (~22秒)
- **全ポーリングで in_data が常に `00 00 00 00` のまま、変化なし**

#### 重大な観察
- Phase 0-B では同じ GET_CAM_CAPT_STATUS が `01 00 00 00 01 00 01 00` (8バイト) を返していた
- 今回は `00 00 00 00` (4バイト)
- 同じ opcode でレスポンスサイズが違う → in_data は SgmCaptStatus 構造体 (7バイト固定) ではない
- 仮説: Apple `inData` パラメータが **PTP response container の parameters** を返している可能性
  (response params は op によって 0〜5 個と可変、4 or 8 バイトになる)
- もしこの仮説が正しいなら、私たちは間違ったフィールドを見ていた

#### 次のアクション
- 標準 PTP の `GetDeviceInfo` (0x1001) を試す診断 (`phase0_diag_v2.py`) を作成済
- GetDeviceInfo は仕様上「メーカー文字列」「モデル文字列」「サポートopcode一覧」などで
  数百バイトを返す。これが:
    - 100バイト以上 → inData は data phase 正しく取れている → Sigma 側の問題
    - 4〜20バイト程度 → inData は response params → 設計を根本見直し

---

### 2026-05-11 21:21 — Phase 0 deep diagnostic (diag_v2) [CRITICAL FINDING]

#### 実行コマンド
`python scripts/phase0_diag_v2.py`

#### 結果

5 つの opcode 全てで、レスポンスは**完全に同じ 12 バイト**:
```
0C 00 00 00 03 00 01 20 XX 00 00 00  (XX = transaction_id)
```
PTPレスポンスコンテナのヘッダだけで、parameters なし。

in_data は op に関わらず 4〜8 バイト:
- 1 回目セッション (trans_id=3): 全て `00 00 00 00` (4バイト)
- 2 回目セッション (trans_id=9): 全て `01 00 00 00 01 00 01 00` (8バイト)

**GetDeviceInfo (0x1001) すら 4 バイトしか返らない** ← 致命的

#### 結論

Apple `ImageCaptureCore.requestSendPTPCommand:outData:` API は:
- データ位相 (data phase) を露出してくれない
- `inData` パラメータは PTP レスポンスコンテナの parameters と推測される
- Sigma 専用プロトコル (status, file info, file content 全てが data phase 依存) と
  根本的に相性が悪い

`ICCameraDeviceCanTakePicture` capability を持つカメラ (Apple の認知済みベンダー) 用に
最適化されており、Sigma fp L のような未認知ベンダーで raw PTP を叩く用途には
**Apple API は事実上使えない**。

#### 方針転換

`DESIGN_PLAN.md` で「最終手段」扱いだった libusb 直接アクセスに切り替える。
- 採用: `pyusb` + `libusb-package` (libusb バイナリ同梱) + `sigma-ptpy` ベース
- 不採用: ImageCaptureCore (続行不可)

#### 次のアクション
1. `pip install pyusb libusb-package sigma-ptpy` で依存追加
2. `scripts/phase0_libusb_smoke.py` を作成して USB レベルでカメラに到達できるか確認
3. 既存の Camera ブリッジを `fp_l_tether/camera/usb_bridge.py` として再実装

---

### 2026-05-11 21:50頃 — libusb smoke test [PASS via sudo] ★ Phase 0 最大の壁突破

#### 経緯
- `pip install pyusb libusb-package` 完了。libusb-package は cp314 用 pre-built wheel が
  PyPI に無くソースビルド → バンドル libusb が空のまま install されて使えず
- Homebrew をインストール (Xcode CLT 同時、5〜15分)
- `brew install libusb` で `/opt/homebrew/lib/libusb-1.0.dylib` 設置
- smoke test 再実行: system / homebrew どちらの backend も読み込めるように
- ただし `Kernel driver IS active — detach failed: Access denied`
- `pkill -f PTPCamera` では離れず → sudo で実行 → 成功

#### 実行コマンド
`sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" scripts/phase0_libusb_smoke.py`

#### 結果
```
Manufacturer  : SIGMA
Product       : SIGMA fp L
Serial        : 0000000091508851
Interface 0   : class=0x06 (Still Image / PTP)
  EP 0x01 BULK/OUT max_pkt=512
  EP 0x82 BULK/IN  max_pkt=512
  EP 0x83 INT/IN   max_pkt=64

✓ detached
✓ Successfully claimed interface 0
✓ Released
✓ libusb smoke test PASSED via backend: system libusb
```

#### 観察
- USB Bulk エンドポイント完全に確保。これで raw PTP コンテナを送受できる
- sudo が必要なのは macOS のカーネルドライバ保護機構による
- 運用上は launchd デーモン化 or codesign + entitlements が将来課題。
  Phase 0/1 は sudo で先に進める

#### 次のアクション
1. `fp_l_tether/camera/usb_bridge.py` を実装
   - PTP コンテナ (Command/Data/Response) の wire format
   - Sigma 独自プロトコル (length prefix + checksum) のラッパ
2. USB 経由で再度 Phase 0-B を実行し、GetCamCaptStatus が
   本物の 7-byte SgmCaptStatus を返すかを確認
3. SnapCommand → DNG ダウンロードを試す

---

### 2026-05-11 22:03 — Phase 0-B (USB) [FULL PASS] ★

#### 実行コマンド
`sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" scripts/phase0_usb_session.py`

#### 結果（成功）
- **GetDeviceInfo (0x1001): 281 bytes** — ASCII 部分に "SIGMA", "SIGMA fp L", "91508851" が見える
  PTP DeviceInfo 構造体が完全に取れている
- **ConfigApi (0x9035): 81 bytes** — `4C 00 00 00 ... 26`
  - length prefix = 76, payload 76 bytes, checksum 0x26 ✓
  - 内部に "V90", "SIGMA fp L", "91508851" が見える
- **GetCamOpPermission (0x9039): 25 bytes** — length=20, mode_word=0x00010001
- **GetCamCaptStatus (0x9015): 8 bytes** `06 00 00 00 00 00 00 06`
  - parsed: `SgmCaptStatus(image_id=0x06, db_head=0x00, db_tail=0x00, capt_status=0x0000, dest=0x00)`
  - has_new_image=False, is_capturing=False ← 待機中、正しい
- **GetCamDataGroup1 (0x9012): 21 bytes** — FieldPresent + 各種設定の raw bytes

#### 観察
- Apple ImageCaptureCore で取れなかった**本物のデータ位相が全て露出**
- Sigma の wire format は **opcode 毎に異なる**:
  - **可変長 op** (ConfigApi, GetCamOpPermission): `<uint32 length> <payload> <uint8 checksum>`
    - checksum = sum(length_prefix + payload) & 0xFF
  - **固定長 op** (GetCamCaptStatus): `<N bytes struct> <uint8 checksum>` (no length prefix)
    - checksum = sum(struct_bytes) & 0xFF
- libgphoto2 #882 トレースの解釈と整合
- SgmCaptStatus に `from_wire()` メソッドを追加して 8-byte wire form を直接パース可能に

#### 次のアクション
Phase 0-C (SnapCommand) に進む

---

### 2026-05-11 22:04 — Phase 0-C (USB) attempt 1 [FAIL]

#### 実行コマンド
`sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" scripts/phase0_usb_snap.py`

#### 結果
- ConfigApi, GetCamOpPermission, baseline GetCamCaptStatus は OK
- **SnapCommand (0x901B) を送信 → response=0x201D で拒否**
- 30 秒間 140 回ポーリングしても ImageID=0x06 のまま変化なし
- カメラの物理シャッターも反応なし

#### Payload送信内容
- struct: `02 01 03` (CaptureMode=0x02 NON_AF_CAPTURE, CaptureAmount=0x01, 3rd byte=0x03)
  - 3rd byte は SgmSnapState の "CheckSum" フィールド (SDK ヘッダ命名) として `0x02+0x01=0x03` を計算して入れた
- wire (length prefix + struct + external checksum): `04 00 00 00 02 01 03 06`

#### 観察
- 0x201D は PTP 標準にないコード → Sigma の vendor 独自エラー
  - 推測: "InvalidParameter" or "InvalidDataFormat"
- libgphoto2 #882 Windows トレースを再解析:
  ```
  [Data] 0x00,0x00,0x00,0x00,0x04,0x00,0x00,0x00,0x02,0x02,0x01
  [CheckSum] 0x05
  ```
  - CaptureMode=0x02, CaptureAmount=0x02, **3rd byte=0x01**
  - 3rd byte が 0x01 で、2+2=4 や 2 XOR 2=0 のどれにもならない
  - つまり **SDK の "CheckSum" 命名がミスリードで、実際は別の意味の byte**
- Windows トレースは SnapCommand 前に SetCamDataGroup1-4 など多数の初期化を実行している
  - 私たちは ConfigApi だけだった
- 仮説:
  - 仮説 A: 3rd byte は固定値 0x01
  - 仮説 B: 3rd byte = CaptureAmount - 1
  - 仮説 C: 不要な readback (GetCamDataGroupN) を全部やってから Snap

#### 次のアクション
`scripts/phase0_snap_variants.py` で複数の payload 形式を順に試して動くものを特定する

---

### 2026-05-11 22:18 — Phase 0-C variants probe [partial]
すべて 0x201D で拒否。3rd byte の値も CaptureMode も関係なかった。

### 2026-05-11 22:30 — Phase 0-C v2 PTP params probe [BREAKTHROUGH on format] ★

#### 実行コマンド
`sudo "/Users/PI/Documents/Claude/Projects/FP L Tether APP/venv/bin/python" scripts/phase0_snap_v2.py`

#### 結果

| Variant | command params | OUT data form | response |
|---|---|---|---|
| v1 | () | length_prefix + struct + ext_chk (8B) | **0x201D** |
| v2 | () | struct + ext_chk (4B) | **0x2001 ✓** |
| v3 | (0,) | struct + ext_chk (4B) | **0x2001 ✓** |
| v4 | (0, 4) | struct + ext_chk (4B) | **0x2001 ✓** |
| v5 | (0, 4) | length_prefix + struct + ext_chk (8B) | **0x201D** |
| v6 | (4,) | struct + ext_chk (4B) | **0x2001 ✓** |
| v7 | (0, 4) | struct only no ext_chk (3B) | **0x2001 ✓** |
| v8 | (0, 4) | NO data phase | timeout |

**しかし全 OK 受理パターンで ImageID は変化せず** (baseline=0x06 のまま)

#### 重大な発見
- **Sigma の OUT data phase は length prefix を含まない** ことが確定
- 形式: `<struct bytes> <1 byte external checksum>` のみ
- libgphoto2 #882 Windows トレースの `00 00 00 00 04 00 00 00` は
  ICAPTPPassThroughPB の内部メタデータが SDK ログに混入していたものだった
- 私の `wrap_sigma_outdata` の長さプレフィックス付与は誤り → 修正済 (usb_bridge.py)
- PTP コマンド container のパラメータは **何でもよい** (なし / (0,) / (4,) / (0,4) すべて OK)
- v7 で external checksum もないバージョンが OK だったのも興味深い (カメラがチェックサム検証を緩めにしている可能性)

#### 未解決問題
- response=0x2001 が出ても **シャッターが切れない** (ImageID 変化なし)
- 仮説 A: DestinationToSave=0x00 (SD only) になっているため PC ImageDB に届かない
- 仮説 B: SetCamDataGroup1-3 で PC 設定を書き込まないとカメラが実撮影を行わない
- 仮説 C: カメラ側が「test capture」モードで応答だけして実撮影しない状態

ユーザーに「カメラの物理シャッター音は鳴ったか」を確認中。

#### 次のアクション
1. usb_bridge.py を修正済（length prefix を不付与に）
2. もしシャッター音が鳴った → `DestinationToSave` を `0x04` (PC) などに設定する SetCamDataGroup3 を実装
3. もし鳴っていない → SetCamDataGroup1 (現在の設定を書き戻し) で PC 制御モードを完全活性化させる

---

## 既知の参照ポイント

### Sigma fp L の USB ID

- Vendor ID: `0x1003`
- Product ID: `0xC442`
- 通常の名称: `SIGMA fp L`

### GetCamCaptStatus 応答の期待バイト列

libgphoto2 #882 の Windows ログから、待機中の応答:

```
[Data] 0x06,0x00,0x00,0x01,0x04,0x00,0x00,
[CheckSum] 0x0B
```

→ `06 00 00 0100 04 00` をパース:
- ImageID = 0x06
- ImageDBHead = 0x00
- ImageDBTail = 0x00
- CaptStatus = 0x0001 (SHOOTING_IN_PROGRESS) ※エンディアン要確認
- DestinationToSave = 0x04
- CheckSum = 0x00

sum(0x06 + 0x00 + 0x00 + 0x00 + 0x01 + 0x04 + 0x00) = 0x0B  ✓

### SnapCommand 応答の期待バイト列

Windows ログより:
```
[Command] SnapCommand
    [OpeCode] 0x901B
    [Data] 0x00,0x00,0x00,0x00,0x04,0x00,0x00,0x00,0x02,0x02,0x01
    [CheckSum] 0x05
```

→ ペイロード解釈:
- 先頭 4 バイト 0x00000000: ICAPTPPassThroughPB header (param1)
- 次 4 バイト 0x00000004 LE: データ長 = 4 (3バイトpayload + 1バイトchecksum)
- payload: CaptureMode=0x02, CaptureAmount=0x02, ???=0x01
- checksum: 0x05 = (0x02 + 0x02 + 0x01) & 0xFF ✓

つまりSnapCommandのSgmSnapStateは:
- byte 0: CaptureMode
- byte 1: CaptureAmount
- byte 2: ???  → 文書では「CheckSum」となっているが、その後にもう1バイトCheckSumがあるため、これは別フィールドの可能性

→ Phase 0-C 実機検証で詳細を確認すること。
