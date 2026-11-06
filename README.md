# PhotoWorkflow

カメラからの取り込みから、位置情報の付与、フレーム付与・EXIF焼き込み、そしてクラウドバックアップやブログ（note）への公開までを一貫して効率化するための自動化ワークフローツール群です。

## 主な機能

1. **自動転送・仕分け (Python / PowerShell)**
   - カメラやメディアから画像ファイルを自動転送。
   - RAW（NEF等）とJPEGを自動で振り分け、日付別フォルダへ綺麗にアーカイブ。
2. **GPS位置情報付与 (Nikon Photo Geotagger)**
   - C#製デスクトップアプリにより、写真アーカイブへスムーズにGPSタグを付与。
3. **フレーム付与 & EXIF焼き込み (`frame_processor.py`)**
   - ExifToolを用いたメタデータの一括取得（バッチ処理による高速化）。
   - 機材名（例: `Z50II` など）の表記ゆれやフォーマットの自動補正。
   - 枠線、ブランドロゴ、撮影データ（シャッタースピード、F値、ISO、焦点距離など）の洗練されたレイアウト描画。
4. **クラウド・ブログ連携**
   - 処理済みの高品質JPEG画像を Amazon Photos へバックアップ。
   - `note` プラットフォームでの公開に最適化された画像管理。

---

## 処理フロー概要

```mermaid
graph TD
    A[カメラ / メディア] -->|自動転送・仕分け| B[ローカルワークスペース]
    B -->|RAW / NEF| C[日付別アーカイブフォルダ]
    B -->|JPEG| D[GPSタグ付け<br>（Nikon Photo Geotagger / Python）]
    D --> E[フレーム付与 & EXIF焼き込み<br>（frame_processor.py / Python）]
    E -->|高解像度JPEG| F[Amazon Photos]
    E -->|ブログ用画像| G[note 投稿]
```

---

## 使い方

```
python main.py
```

起動するとメニューが表示され、実行したいステップを番号で選びます。選んだステップだけを実行し、終わるとメニューに戻ります。

```
==================================================
      写真整理ワークフロー  メニュー
==================================================
  取り込み元: C:\SHARE\Photo\from_camera
    JPEG 120 件 / RAW 120 件 / GPX 2 件
--------------------------------------------------
  [1] STEP 1: GPS位置情報の書き込み
  [2] STEP 1.5: 手動GPS情報付与
  [3] STEP 2: 写真整理・コピー
  [4] STEP 2.5: EXIF抽出・書き出し処理
  [5] STEP 3: フレーム付与処理
  [A] 全ステップを順に実行（各ステップの前に確認あり）
  [Q] 終了
--------------------------------------------------
番号を入力してください:
```

- 取り込み元フォルダがなくても、STEP 2.5 と STEP 3（`to_note` が対象）は実行できます。
- `config.json` の `steps` で無効にしたステップは「※config で無効」と表示され、実行できません。
- ステップ実行中に Ctrl+C を押すと、そのステップを中断してメニューに戻ります。

### コマンドラインオプション

| オプション | 内容 |
|---|---|
| `--step STEP` | メニューを出さずに指定したステップだけを実行（複数指定可。例: `--step gps_tag --step copy`） |
| `--all` | メニューを出さずに全ステップを順に実行（各ステップの前に確認あり） |
| `--yes`, `-y` | メニューを出さずに全ステップを確認なしで実行（タスクスケジューラなど無人実行向け） |
| `--skip STEP` | 全ステップ実行時に指定したステップを飛ばす |
| `--dry-run` | ファイル操作をせず、実行予定のステップだけ表示 |
| `--list-steps` | ステップ名の一覧を表示 |
| `--config PATH` | 使用する `config.json` を指定 |

ステップ名: `gps_tag` / `manual_gps` / `copy` / `exif_export` / `frame`

## フレームのレイアウト設定（YAML）

STEP 3 のフレームは `config.json` の `external_tools.flame_config` で指定した YAML（`standard.yaml` / `black.yaml` など）で見た目を決めます。
従来の書き方はそのまま使えます。さらに次の設定を追加できます（すべて省略可。全項目の例は `custom_example.yaml`）。

長さは、数値なら「写真の長辺に対する比率」、`"80px"` のような文字列ならピクセル数です。

| セクション | キー | 内容 |
|---|---|---|
| `canvas` | `margins.top` / `bottom` / `left` / `right` | 上下左右の余白を個別に指定（`ratios` より優先） |
| | `aspect_ratio` | キャンバス全体の比率（例: `"1:1"`, `"4:5"`）。足りない分の余白を自動で追加 |
| | `aspect_ratio_portrait` | 縦長写真だけに使う比率 |
| | `aspect_fill` | 高さを足すときの配分: `center` / `bottom` / `top` |
| `layout` | `lines` | 行のリスト（何行でも可）。各行に `text` / `font` / `size` / `color` / `align` / `spacing` を指定 |
| | `align` | 全行の揃え: `left` / `center` / `right` |
| | `align_to` | 揃えの基準: `image`（写真の端）/ `canvas`（キャンバスの端） |
| | `padding_x` | 左右揃えのときの端からの距離 |
| | `text_area` | 文字を置く余白: `bottom` / `top` |
| | `vertical_align` | `padding`（従来どおり `top_padding_ratio`）/ `center`（余白の中央） |
| | `line_spacing` / `y_offset` | 行の間隔・全体の上下位置（比率でも指定でき、画像サイズに合わせて変わる） |
| `logo` | `enabled` / `path` | ロゴの表示有無と画像ファイル |
| | `height` / `height_font_ratio` | ロゴの高さ（直接指定、または1行目の文字サイズ×倍率） |
| | `position` | `left` / `center` / `right` / `text_left` / `text_right`（文字の隣） |
| | `vertical` | `bottom` / `center` / `top` / `text`（文字ブロックの中央） |
| | `offset_x` / `offset_y` / `gap` / `opacity` | 位置の微調整、文字との間隔、不透明度 |
| | `left` / `top` | キャンバス左上からの座標で直接指定（`position` / `vertical` より優先。片方だけでも可） |

`layout.lines` を書いた場合は `layout.top` / `layout.bottom` と `fonts.main_size` / `sub_size` は不要です。
テンプレートに存在しない項目（例: `{unknown}`）は空文字になり、処理は止まりません。
