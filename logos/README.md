# メーカー別ロゴのフォルダ

フレームに入れるロゴ画像をここに置きます。YAML の `logo.dir: "logos"` を指定すると、
写真の EXIF のメーカー名（Make）を見て、使うロゴを自動で切り替えます。

## ファイル名

| メーカー（EXIF の Make） | ファイル名 |
|---|---|
| NIKON CORPORATION | `nikon.png` |
| Google（Pixel） | `google.png` |
| 上記以外 | メーカー名の最初の単語を小文字にしたもの（例: `FUJIFILM` → `fujifilm.png`、`Canon` → `canon.png`、`SONY` → `sony.png`） |

- 背景が透明な PNG がおすすめです（`.webp` / `.jpg` も使えます）
- 別のファイル名にしたいときや、ロゴごとに大きさを変えたいときは YAML の `logo.makers` に書きます
- 写真のメーカー名は `exiftool -Make 写真.jpg` で確認できます

## 注意

ロゴは各メーカーの商標です。このフォルダの画像はリポジトリに含めません（`.gitignore` で除外しています）。
各自で用意して置いてください。
