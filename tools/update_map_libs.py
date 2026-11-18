"""手動GPS付与ツールの地図ライブラリ（Leaflet など）を新しい版に更新する。

manual_gps_tagger.py の地図画面は、ライブラリを unpkg（CDN）から版番号を固定して読み込み、
integrity（ファイルの sha256）で中身が差し替えられていないことを確かめている。
このスクリプトは、その版番号と integrity をまとめて書き換える。

    python tools/update_map_libs.py              # 大きな版（1.x など）は変えずに、その中の最新版へ
    python tools/update_map_libs.py --major      # 大きな版も含めて最新版へ
    python tools/update_map_libs.py --dry-run    # 書き換えずに、更新できる版だけ表示

安全のため、次の版は選ばない。
  - 公開から --min-age-days 日（既定 7日）たっていない版（公開直後に取り下げ・差し替えが起きることがあるため）
  - ベータ版など正式版でないもの、npm で非推奨（deprecated）とされた版

ファイルは unpkg から取得し、npm に登録された配布ファイル（チェックサムで検証したもの）の中身と
1バイトも違わないことを確かめてから integrity を計算する。

更新したら、手動GPS付与ツールを起動して地図と住所検索が動くことを確認すること。
特に --major で大きな版を上げた場合は、ライブラリの使い方が変わって地図が表示されないことがある。
そのときは git で元に戻せば、前の版に戻る。
"""

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = APP_DIR / "modules" / "manual_gps_tagger.py"
REGISTRY = "https://registry.npmjs.org/"
CDN = "https://unpkg.com/"
TIMEOUT = 30

# 例: https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
#         integrity="sha256-..."
ASSET_PATTERN = re.compile(
    r'https://unpkg\.com/(?P<pkg>(?:@[\w.-]+/)?[\w.-]+)@(?P<ver>[\w.+-]+)/(?P<path>[^"\s]+)"'
    r'(?P<between>\s+)integrity="(?P<sri>(?P<algo>sha256|sha384|sha512)-[A-Za-z0-9+/=]+)"'
)

Version = Tuple[int, int, int]


@dataclass
class Asset:
    """HTML の中の、CDN から読み込むファイル1つ。"""

    package: str
    version: str
    path: str
    algorithm: str
    start: int   # 置き換える範囲（URL の先頭から integrity の値の末尾まで）
    end: int


@dataclass
class Plan:
    """1つのパッケージの更新内容。"""

    package: str
    current: str
    new: Optional[str]          # None なら更新しない
    skipped_young: List[str]    # 新しすぎるため見送った版
    note: str = ""


# ------------------------------------------------------------------
# 版番号
# ------------------------------------------------------------------
def parse_version(text: str) -> Optional[Version]:
    """"1.9.4" → (1, 9, 4)。ベータ版（"2.0.0-beta.1" など）や書式が違うものは None。"""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _base_version(text: str) -> Version:
    """範囲指定の中の版番号。"2.0.0-beta.4" は (2, 0, 0)、"1.6" は (1, 6, 0) として扱う。"""
    parts = re.split(r"[-+]", text.strip().lstrip("v="), maxsplit=1)[0].split(".")
    nums = [int(p) if p.isdigit() else 0 for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def satisfies(version: Version, spec: str) -> bool:
    """npm の版の範囲指定（"^1.6.0 || ^2.0.0" など）を version が満たすか。

    peerDependencies でよく使う ^ ~ >= > <= < = と x（"1.x"）、"||" に対応する。
    """
    for alternative in spec.split("||"):
        comparators = alternative.split()
        if not comparators or all(_match_comparator(version, c) for c in comparators):
            return True
    return False


def _match_comparator(version: Version, comparator: str) -> bool:
    if comparator in ("*", "x", "X", ""):
        return True
    for op in (">=", "<=", ">", "<", "=", "^", "~"):
        if comparator.startswith(op):
            target = comparator[len(op):]
            break
    else:
        op, target = "=", comparator

    if op in ("=", "") and re.search(r"(^|\.)[xX*]", target):
        # "1.x" → 1.0.0 以上 2.0.0 未満
        fixed = [p for p in target.split(".") if p not in ("x", "X", "*")]
        lower = _base_version(".".join(fixed))
        upper = (lower[0] + 1, 0, 0) if len(fixed) == 1 else (lower[0], lower[1] + 1, 0)
        return lower <= version < upper

    base = _base_version(target)
    if op == ">=":
        return version >= base
    if op == ">":
        return version > base
    if op == "<=":
        return version <= base
    if op == "<":
        return version < base
    if op == "^":
        if base[0] > 0:
            upper = (base[0] + 1, 0, 0)
        elif base[1] > 0:
            upper = (0, base[1] + 1, 0)
        else:
            upper = (0, 0, base[2] + 1)
        return base <= version < upper
    if op == "~":
        return base <= version < (base[0], base[1] + 1, 0)
    return version == base


# ------------------------------------------------------------------
# HTML の読み取り
# ------------------------------------------------------------------
def find_assets(source: str) -> List[Asset]:
    """CDN から版番号と integrity 付きで読み込んでいるファイルの一覧。"""
    assets = []
    for m in ASSET_PATTERN.finditer(source):
        assets.append(Asset(
            package=m.group("pkg"), version=m.group("ver"), path=m.group("path"),
            algorithm=m.group("algo"), start=m.start(), end=m.end(),
        ))
    return assets


def asset_text(asset: Asset, version: str, sri: str, between: str) -> str:
    return f'{CDN}{asset.package}@{version}/{asset.path}"{between}integrity="{sri}"'


# ------------------------------------------------------------------
# 版の選択
# ------------------------------------------------------------------
def _published(meta: dict, version: str) -> Optional[datetime]:
    text = (meta.get("time") or {}).get(version)
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def choose_version(meta: dict, current: str, allow_major: bool, min_age_days: int,
                   now: Optional[datetime] = None) -> Tuple[Optional[str], List[str]]:
    """更新先の版と、新しすぎて見送った版の一覧を返す。更新先が無ければ None。

    今より古い版には戻さない。allow_major が False なら、今と同じ大きな版（1.x など）の中だけで選ぶ。
    """
    now = now or datetime.now(timezone.utc)
    current_v = parse_version(current)
    if current_v is None:
        raise ValueError(f"今の版番号を読み取れません: {current}")

    candidates = []
    for text, info in (meta.get("versions") or {}).items():
        v = parse_version(text)
        if v is None or v <= current_v or info.get("deprecated"):
            continue
        if not allow_major and v[0] != current_v[0]:
            continue
        candidates.append((v, text))
    candidates.sort(reverse=True)

    skipped = []
    for _, text in candidates:
        published = _published(meta, text)
        if published is None or now - published < timedelta(days=min_age_days):
            skipped.append(text)
            continue
        return text, skipped
    return None, skipped


# ------------------------------------------------------------------
# 取得と検証
# ------------------------------------------------------------------
def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "PhotoWorkflow-update-map-libs"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def fetch_metadata(package: str, get: Callable[[str], bytes] = http_get) -> dict:
    return json.loads(get(REGISTRY + urllib.parse.quote(package, safe="@")))


def sri_hash(data: bytes, algorithm: str = "sha256") -> str:
    return f"{algorithm}-" + base64.b64encode(hashlib.new(algorithm, data).digest()).decode("ascii")


def verify_tarball(meta: dict, version: str, tarball: bytes) -> None:
    """npm に登録された配布ファイルのチェックサム（integrity / shasum）と一致するか確かめる。"""
    dist = meta["versions"][version]["dist"]
    integrity = dist.get("integrity")
    if integrity:
        algorithm, _, expected = integrity.partition("-")
        actual = base64.b64encode(hashlib.new(algorithm, tarball).digest()).decode("ascii")
        if actual != expected:
            raise ValueError(f"npm の配布ファイルのチェックサムが一致しません（{version}）")
    elif dist.get("shasum"):
        if hashlib.sha1(tarball).hexdigest() != dist["shasum"]:
            raise ValueError(f"npm の配布ファイルのチェックサムが一致しません（{version}）")
    else:
        raise ValueError(f"npm にチェックサムが登録されていません（{version}）")


def read_from_tarball(tarball: bytes, path: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            # 配布ファイルの中は "package/dist/leaflet.js" のような形（先頭のフォルダ名は package 以外のこともある）
            if member.isfile() and member.name.split("/", 1)[-1] == path:
                extracted = tar.extractfile(member)
                if extracted is not None:
                    return extracted.read()
    raise FileNotFoundError(f"配布ファイルの中に {path} がありません")


def download_verified(meta: dict, package: str, version: str, path: str,
                      get: Callable[[str], bytes] = http_get, tarballs: Optional[Dict[str, bytes]] = None) -> bytes:
    """unpkg から取得したファイルが、npm の配布ファイルの中身と同じであることを確かめて返す。"""
    tarballs = tarballs if tarballs is not None else {}
    if version not in tarballs:
        tarball = get(meta["versions"][version]["dist"]["tarball"])
        verify_tarball(meta, version, tarball)
        tarballs[version] = tarball
    expected = read_from_tarball(tarballs[version], path)
    served = get(f"{CDN}{package}@{version}/{path}")
    if served != expected:
        raise ValueError(f"unpkg のファイルが npm の配布ファイルと一致しません: {package}@{version}/{path}")
    return served


# ------------------------------------------------------------------
# 全体の流れ
# ------------------------------------------------------------------
def plan_updates(assets: List[Asset], metas: Dict[str, dict], allow_major: bool, min_age_days: int,
                 now: Optional[datetime] = None) -> List[Plan]:
    plans = []
    for package in dict.fromkeys(a.package for a in assets):
        current = next(a.version for a in assets if a.package == package)
        new, skipped = choose_version(metas[package], current, allow_major, min_age_days, now)
        plans.append(Plan(package, current, new, skipped))
    return plans


def peer_warnings(plans: List[Plan], metas: Dict[str, dict]) -> List[str]:
    """更新後の版どうしが、互いの対応している版（peerDependencies）を満たしているか。"""
    chosen = {p.package: (p.new or p.current) for p in plans}
    warnings = []
    for package, version in chosen.items():
        peers = (metas[package].get("versions", {}).get(version, {}) or {}).get("peerDependencies") or {}
        for peer, spec in peers.items():
            if peer in chosen:
                peer_v = parse_version(chosen[peer])
                if peer_v is not None and not satisfies(peer_v, spec):
                    warnings.append(f"{package}@{version} は {peer} {spec} に対応していますが、{peer}@{chosen[peer]} になります")
    return warnings


def apply_updates(source: str, assets: List[Asset], plans: List[Plan], metas: Dict[str, dict],
                  get: Callable[[str], bytes] = http_get) -> str:
    """更新するファイルを取得・検証し、版番号と integrity を書き換えた HTML を返す。"""
    new_versions = {p.package: p.new for p in plans if p.new}
    tarballs: Dict[str, Dict[str, bytes]] = {}
    replacements = []
    for asset in assets:
        version = new_versions.get(asset.package)
        if not version:
            continue
        data = download_verified(metas[asset.package], asset.package, version, asset.path,
                                 get=get, tarballs=tarballs.setdefault(asset.package, {}))
        between = ASSET_PATTERN.match(source, asset.start).group("between")
        replacements.append((asset, asset_text(asset, version, sri_hash(data, asset.algorithm), between)))
    # 後ろから置き換えると、前の位置がずれない
    for asset, text in sorted(replacements, key=lambda r: r[0].start, reverse=True):
        source = source[:asset.start] + text + source[asset.end:]
    return source


def _major_hint(plan: Plan, meta: dict, min_age_days: int) -> str:
    """大きな版が上の新しい版があるときの案内（--major なしで実行したとき用）。"""
    current_major = parse_version(plan.current)[0]
    major_new, major_skipped = choose_version(meta, plan.current, True, min_age_days)
    if major_new and parse_version(major_new)[0] > current_major:
        return f"大きな版が上の {major_new} もあります（上げる場合は --major を付けて実行）"
    young = [v for v in major_skipped if parse_version(v)[0] > current_major]
    if young:
        return f"大きな版が上の {', '.join(young)} もありますが、公開から {min_age_days} 日たっていません"
    return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="地図ライブラリ（Leaflet など）の版番号と integrity を更新する")
    parser.add_argument("--major", action="store_true", help="大きな版（1.x → 2.x など）も含めて最新版にする")
    parser.add_argument("--dry-run", action="store_true", help="書き換えずに、更新できる版だけ表示する")
    parser.add_argument("--min-age-days", type=int, default=7, metavar="N",
                        help="公開から N 日たっていない版は選ばない（既定: 7。0 なら公開直後の版も選ぶ）")
    parser.add_argument("--file", type=Path, default=DEFAULT_TARGET, help="書き換えるファイル（既定: manual_gps_tagger.py）")
    args = parser.parse_args(argv)

    # 改行コード（CRLF / LF）を変えないよう、バイトのまま読み書きする
    source = args.file.read_bytes().decode("utf-8")
    assets = find_assets(source)
    if not assets:
        print(f"[エラー] {args.file} に、版番号と integrity 付きの unpkg の URL が見つかりません")
        return 1

    try:
        metas = {pkg: fetch_metadata(pkg) for pkg in dict.fromkeys(a.package for a in assets)}
        plans = plan_updates(assets, metas, args.major, args.min_age_days)
    except Exception as e:
        print(f"[エラー] npm から版の情報を取得できませんでした: {e}")
        return 1

    print("地図ライブラリの版:" + ("（大きな版も含めて確認）" if args.major else "（大きな版は今のまま）"))
    for p in plans:
        status = f"{p.current} → {p.new}" if p.new else f"{p.current}（更新なし）"
        print(f"  - {p.package}: {status}")
        if p.skipped_young:
            print(f"      公開から {args.min_age_days} 日たっていないため見送り: {', '.join(p.skipped_young)}")
        current_major = parse_version(p.current)[0]
        if p.new and parse_version(p.new)[0] != current_major:
            print(f"      ※ 大きな版が変わります（{current_major}.x → {parse_version(p.new)[0]}.x）。使い方が変わっている可能性があります")
    if not args.major:
        for p in plans:
            hint = _major_hint(p, metas[p.package], args.min_age_days)
            if hint:
                print(f"      {hint}")

    for w in peer_warnings(plans, metas):
        print(f"[警告] {w}")

    if not any(p.new for p in plans):
        print("更新する版はありません。")
        return 0
    if args.dry_run:
        print("（--dry-run のため書き換えていません）")
        return 0

    try:
        updated = apply_updates(source, assets, plans, metas)
    except Exception as e:
        print(f"[エラー] ファイルを取得・検証できなかったため、書き換えていません: {e}")
        return 1

    args.file.write_bytes(updated.encode("utf-8"))
    print(f"{args.file} を書き換えました。手動GPS付与ツールを起動して、地図と住所検索が動くか確認してください。")
    print("動かない場合は git で元に戻してください（例: git checkout -- modules/manual_gps_tagger.py）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
