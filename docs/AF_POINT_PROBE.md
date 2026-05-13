# AF ピント位置 (Single AF point coordinate) 探索手順

## 目的

`Focus Area = Single` 時の AF 点座標が PTP の `GetCamDataGroupFocus (0x9031)`
あるいは他の DataGroup のどこに格納されているかを実機 diff で特定する。
確定すれば `SetCamDataGroupFocus (0x9032)` でアプリ側から AF 点を移動できる。

## 既知の手がかり

`memory/project_focus_group_mapping.md` より:
- tag `0x000D` (UNDEF×4): AF baseline で `54 01 00 02`、body-MF で `55 01 00 02`
  → 「AF 点座標 X (0x154=340) / Y (0x0200=512)」の候補。要検証。
- tag `0x0051` (SHORT): ライブ測定値、毎回変動 → 設定判定に使えない (除外)
- DataGroup1 (0x9012) byte 0x07: Focus Mode + Focus Area + Face/Eye の packed
  enum、AF 点座標は別の場所のはず。

## 重要な制約 (memory/feedback_diff_session_workflow.md)

**fp L は USB 接続中は本体操作が完全ロック** (2026-05-12 確認):
- 十字キー / D-pad / タッチ操作も含めて全て効かない
- AF 点フレームは USB 接続中 1 mm も動かせない
- ゆえに **N position 取るには N 回 USB 抜き差しが必要** (1 セッション内で AF 点を順に動かして比較は不可能)

## 事前準備 (カメラ本体側)

USB を抜いた状態でメニューから以下を固定:

| 項目 | 値 |
|---|---|
| Focus Mode | AF-S |
| Focus Area | **Single** |
| Face/Eye Detection AF | Off |
| Pre-AF | Off |
| AF + MF | Off |
| レンズ物理 AF/MF スイッチ | AF |

これらが Single AF 点座標以外の変動要因なので固定。

## 取得手順 (各 position につき 1 回)

position label を `center / top_left / top_right / bottom_left / bottom_right` とする。

```bash
# 1. USB を抜く
# 2. 本体で AF 点フレームを target position に移動 (十字キー / タッチ)
# 3. USB を挿し直す
# 4. ptpcamerad を kill
sudo killall ptpcamerad
# 5. snapshot を取る (label を変えて 5 回繰り返す)
sudo venv/bin/fp-l-tether inspect all \
    --save ~/Desktop/fp_l_inspect/af_point/af_<LABEL>.json
```

position 5 つ全て取り終わるまで繰り返し。

**先に 2 position で確認するのが効率的**: まず center と top_left の 2 つだけ取って差分が出るか見る。

```bash
# まず center → top_left の 2 個だけ
sudo killall ptpcamerad
sudo venv/bin/fp-l-tether inspect all \
    --save ~/Desktop/fp_l_inspect/af_point/af_center.json
# USB 抜く → AF 点を top_left に → USB 挿す
sudo killall ptpcamerad
sudo venv/bin/fp-l-tether inspect all \
    --save ~/Desktop/fp_l_inspect/af_point/af_top_left.json
```

## 解析

```bash
venv/bin/fp-l-tether inspect all \
    --compare ~/Desktop/fp_l_inspect/af_point/af_center.json \
    --compare ~/Desktop/fp_l_inspect/af_point/af_top_left.json
```

差分があれば → 全 5 position を取って X / Y 座標 byte を特定する次のステップへ。
差分ゼロなら → AF 点座標はどの DataGroup にも格納されていない可能性 (CamConfig 0x9010
や CamStatus2 0x902c など別 opcode を試す必要)。

### 期待される signal

- AF 点を上下に動かす → Y 座標 byte が変化
- AF 点を左右に動かす → X 座標 byte が変化
- 5 position の差分で X / Y それぞれが連続的に変化する byte を絞り込む

候補:
- `focus` tag `0x000D` (UNDEF×4): 4 byte あるので X (2B LE) + Y (2B LE) 仮説
- 他の focus tag (0x000A / 0x000B / 0x000C など)
- DG1-6 の raw byte: 固定構造体なので IFD には出ないが `--compare` の raw byte diff で見える

### ノイズ要因

- `focus` tag `0x0051` SHORT: 毎回変動するライブ値。無視。
- レンズ AF 駆動による焦点距離の副次変化が DG3 等に出る可能性 → ignore。

## 確定後の実装

確定したら:

1. `memory/project_focus_group_mapping.md` を更新
2. `fp_l_tether/camera/usb_bridge.py` に `sigma_set_focus_point(x, y)` を追加
   (SetCamDataGroupFocus 0x9032 の wire format も別途逆解析必要)
3. floating panel に「AF 点プリセット」ボタン (center / 1/3 grid 9 点 等)

## 進捗ノート

- [ ] 2 position の snapshot 取得 (center, top_left) — 差分の有無を確認
- [ ] 差分があれば 5 position に拡張
- [ ] `--compare` で AF 点座標 tag 特定
- [ ] SetCamDataGroupFocus (0x9032) の wire format 解析 (memory 未記載)
- [ ] read-back で書き込み値が反映されるか確認
- [ ] panel UI 実装

## 失敗ログ

**2026-05-12 試行 1**: `probe_af_point.py` で USB 接続中に 5 position 連続取得しようとしたが、USB 接続中は本体操作が完全ロックされるため AF 点は動かず、全 snapshot が同一になった。手順 B (USB 抜き差し) のみが有効と確定。
