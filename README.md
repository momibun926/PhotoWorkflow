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
    B -->|JPEG| D[GPSタグ付け<br>（Nikon Photo Geotagger / C#）]
    D --> E[フレーム付与 & EXIF焼き込み<br>（frame_processor.py / Python）]
    E -->|高解像度JPEG| F[Amazon Photos]
    E -->|ブログ用画像| G[note 投稿]