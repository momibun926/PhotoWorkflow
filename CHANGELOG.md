# 変更履歴

このプログラムの主な変更を記録します。

- 書き方は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) にならっています。
- バージョン番号の付け方は [セマンティック バージョニング](https://semver.org/lang/ja/) にならっています（README の「バージョンと変更履歴」を参照）。

## [未リリース]

<!-- 次のバージョンに入れる変更を、ここに「追加」「変更」「修正」「削除」に分けて書き足していく -->

## [1.1.0] - 2026-11-25

フレーム付与処理を、Nikon 以外のカメラ（まずは Google Pixel）にも対応させた版。今までの YAML はそのまま使える。

### 追加

- **フレーム付与処理**: 文字の行で `{focal_length_text}` を使えるようにした。Nikon DX のように35mmより小さいセンサーの写真は「35mm (52.5mm)」と換算値を付け、FX は「35mm」のまま（換算値 ÷ 実焦点距離で自動判定。`focal_length.show_equivalent` / `equivalent_format` で変更可）。`standard.yaml` / `custom_example.yaml` / `black.yaml` の焦点距離をこれに変えた。Google Pixel も同じく「4.53mm (24mm)」と表示する（`equivalent_35mm` は空にした）。
- **フレーム付与処理**: `focal_length.equivalent_35mm` に書いたメーカーの写真は、`{focal_length}` を35mm換算で表示するようにした。換算値はどの写真でも `{focal_length_35mm}` で使える。
- **フレーム付与処理**: メーカー共通の「仕上げの設定」`{style}` を追加した。Nikon はピクチャーコントロール、それが無い機種（Pixel など）は「撮影シーン (撮影処理)」（例: `Standard (HDR+)`）を表示する。あわせて `{scene}`（SceneCaptureType）と `{processing}`（Software から取り出した撮影処理名）も使えるようにした。`standard.yaml` は「Picture Control」を「Style」に変えた。
- **フレーム付与処理**: 写真のメーカー（EXIF の Make）に合わせてロゴを自動で切り替えられるようにした（`logo.dir` / `logo.makers` / `logo.fallback`）。ロゴは `logos` フォルダに置く。まずは Nikon と Google Pixel を `standard.yaml` / `custom_example.yaml` に設定した。
- **フレーム付与処理**: `scaling.basis: display` で、note などに貼ったときに縦写真の文字・ロゴ・余白が横写真と同じ大きさに見えるよう自動で調整できるようにした（表示枠は `scaling.display_box`、既定 `"4:5"`）。`standard.yaml` と `custom_example.yaml` で有効にした。

### 変更

- **フレーム付与処理**: 同梱の `standard.yaml` の表示を変えた。焦点距離は `{focal_length_text}`（DX・スマホは換算値付き）、3行目の「Picture Control」は「Style」、ロゴは `logos` フォルダからメーカーごとに選ぶ。今までどおり `nikon_logo.png` を使う場合は、`logo.dir` を消して `logo.path` を書く。

### 修正

- **フレーム付与処理**: 標準 EXIF のホワイトバランスが 0（Auto）のとき「-」と表示されていたのを、「Auto」と表示するようにした（Pixel など、メーカー独自の WB 情報が無い機種で起きていた）。Nikon のようにメーカー独自の値がある場合は、そちらを優先する。

## [1.0.0] - 2026-11-18

初回公開版。[MIT ライセンス](LICENSE)で公開（同梱フォントは [SIL Open Font License 1.1](fonts/OFL.txt)）。

### 機能

- **GPS位置情報の書き込み**: GPX ログをもとに、写真（JPEG・RAW）へ位置情報を書き込む。
  - 既に位置情報がある写真は書き換えない。
  - カメラ時計のずれを補正できる（`gps.camera_clock_offset`）。
  - 付与できなかった写真は、理由（GPX の記録範囲外・撮影日時なし など）とあわせて一覧表示する。
- **手動GPS情報付与**: 地図をクリックして位置を選び、写真へ書き込む GUI ツール（PyQt6 + Leaflet）。住所検索にも対応。
- **写真整理・コピー**:
  - JPEG を to_note と追加のコピー先（Amazon Photos 用フォルダなど）へコピーする。
  - RAW を撮影日ごとのフォルダ（年/月/年月日）へコピーする。撮影日が分からないものは unknown へ。
  - コピー後にサイズを確認し、すべてのコピー先へのコピーに成功したファイルだけを取り込み元からゴミ箱へ移す。一時的に移せない場合は少し待ってやり直す。
- **EXIF抽出・書き出し処理**: 写真の EXIF を、全タグの一覧（TSV）と写真ごとの `.exif` ファイルに書き出す。書き出すタグは `config.json` で変えられる。
- **フレーム付与処理**: 写真に余白・撮影データ・ロゴを付ける。
  - レイアウトは YAML（`standard.yaml` / `black.yaml` / `custom_example.yaml`）で決める。
  - 色空間（ICC プロファイル）を引き継ぐ。
  - 複数のプロセスで並列に処理する（`frame.workers`）。
  - 作成済みのフレーム画像は飛ばす（`frame.overwrite_existing` ですべて作り直す）。
- **メニューとコマンドライン**: メニューから1ステップずつ、または全ステップを順に実行できる。`--yes` / `--all` / `--step` / `--skip` / `--dry-run` / `--list-steps` / `--config` / `--version` に対応。
- **処理中の表示**: 全ステップで、写真1枚ごとの進み具合（件数・残り時間・今のファイル名）を同じ形で表示する。1行を上書きする表示と、1枚ごとに1行の表示を切り替えられる（`display.per_file`）。
- **ログ**: 詳細を `photo_organizer.log` に記録する（5MB ごとに切り替え、5世代まで保存）。
- **地図ライブラリの更新スクリプト**: `tools/update_map_libs.py`。版番号とハッシュ値（integrity）をまとめて更新する。
- **テスト**: `tests/` に pytest のテスト一式。

[未リリース]: https://github.com/momibun926/PhotoWorkflow/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/momibun926/PhotoWorkflow/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/momibun926/PhotoWorkflow/releases/tag/v1.0.0
