"""tools/update_map_libs.py（地図ライブラリの更新）。ネットワークは使わず、npm と unpkg の応答を作って試す。"""

import base64
import hashlib
import io
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from tools import update_map_libs as u

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)

HTML = '''<head>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.3/dist/leaflet.css"
        integrity="sha256-OLDCSS" crossorigin="anonymous" />
  <script src="https://unpkg.com/leaflet@1.9.3/dist/leaflet.js"
          integrity="sha256-OLDJS" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/geo@3.0.0/dist/geo.js" integrity="sha256-OLDGEO" crossorigin="anonymous"></script>
</head>'''


def _days_ago(days):
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _tarball(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo("package/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _meta(versions, tarballs=None, peers=None, deprecated=()):
    """versions: {版: 公開からの日数}"""
    tarballs = tarballs or {}
    meta = {"versions": {}, "time": {}, "dist-tags": {"latest": max(versions, key=lambda v: u._base_version(v))}}
    for version, age in versions.items():
        dist = {"tarball": f"https://registry.example/{version}.tgz"}
        if version in tarballs:
            dist["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(tarballs[version]).digest()).decode()
        info = {"dist": dist}
        if peers and version in peers:
            info["peerDependencies"] = peers[version]
        if version in deprecated:
            info["deprecated"] = "使わないでください"
        meta["versions"][version] = info
        meta["time"][version] = _days_ago(age)
    return meta


@pytest.mark.parametrize("spec, version, expected", [
    ("^1.6.0", (1, 9, 4), True),
    ("^1.6.0", (2, 0, 0), False),
    ("^1.6.0 || ^2.0.0-beta.4", (2, 1, 0), True),
    ("~1.9.0", (1, 9, 9), True),
    ("~1.9.0", (1, 10, 0), False),
    (">=1.7 <2", (1, 8, 0), True),
    (">=1.7 <2", (2, 0, 0), False),
    ("1.x", (1, 3, 0), True),
    ("1.x", (2, 0, 0), False),
    ("^0.7.2", (0, 7, 9), True),
    ("^0.7.2", (0, 8, 0), False),
    ("*", (5, 0, 0), True),
    ("1.9.4", (1, 9, 4), True),
])
def test_satisfies(spec, version, expected):
    assert u.satisfies(version, spec) is expected


def test_parse_version_rejects_prerelease():
    assert u.parse_version("1.9.4") == (1, 9, 4)
    assert u.parse_version("2.0.0-beta.4") is None


def test_choose_version_same_major_by_default():
    meta = _meta({"1.9.3": 400, "1.9.4": 300, "2.0.0": 100})
    assert u.choose_version(meta, "1.9.3", False, 7, NOW) == ("1.9.4", [])
    assert u.choose_version(meta, "1.9.3", True, 7, NOW) == ("2.0.0", [])


def test_choose_version_skips_young_prerelease_deprecated():
    meta = _meta({"4.0.0": 120, "4.1.0": 30, "4.2.0": 20, "4.3.0-beta.1": 10, "42.0.0": 3}, deprecated=("4.2.0",))
    assert u.choose_version(meta, "4.0.0", True, 7, NOW) == ("4.1.0", ["42.0.0"])
    assert u.choose_version(meta, "4.0.0", True, 0, NOW) == ("42.0.0", [])


def test_choose_version_never_downgrades():
    meta = _meta({"1.0.0": 400, "1.9.4": 300})
    assert u.choose_version(meta, "1.9.4", True, 7, NOW) == (None, [])


def test_find_assets():
    assets = u.find_assets(HTML)
    assert [(a.package, a.version, a.path) for a in assets] == [
        ("leaflet", "1.9.3", "dist/leaflet.css"),
        ("leaflet", "1.9.3", "dist/leaflet.js"),
        ("geo", "3.0.0", "dist/geo.js"),
    ]


def _fake_registry():
    files = {"dist/leaflet.js": b"// leaflet 1.9.4", "dist/leaflet.css": b"/* leaflet 1.9.4 */"}
    tarballs = {"1.9.4": _tarball(files)}
    metas = {
        "leaflet": _meta({"1.9.3": 400, "1.9.4": 300}, tarballs=tarballs),
        "geo": _meta({"3.0.0": 400}),
    }
    responses = {
        "https://registry.example/1.9.4.tgz": tarballs["1.9.4"],
        "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js": files["dist/leaflet.js"],
        "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css": files["dist/leaflet.css"],
    }
    return files, metas, responses


def test_apply_updates_rewrites_version_and_integrity():
    files, metas, responses = _fake_registry()
    assets = u.find_assets(HTML)
    plans = u.plan_updates(assets, metas, allow_major=False, min_age_days=7, now=NOW)
    assert [(p.package, p.new) for p in plans] == [("leaflet", "1.9.4"), ("geo", None)]

    updated = u.apply_updates(HTML, assets, plans, metas, get=responses.__getitem__)
    assert "leaflet@1.9.3" not in updated
    assert f'leaflet@1.9.4/dist/leaflet.js"\n          integrity="{u.sri_hash(files["dist/leaflet.js"])}"' in updated
    assert f'integrity="{u.sri_hash(files["dist/leaflet.css"])}"' in updated
    assert 'geo@3.0.0/dist/geo.js" integrity="sha256-OLDGEO"' in updated  # 更新しないものはそのまま


def test_apply_updates_rejects_cdn_mismatch():
    """unpkg が npm の配布ファイルと違う中身を返したら、書き換えない。"""
    _, metas, responses = _fake_registry()
    responses["https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"] = b"// tampered"
    assets = u.find_assets(HTML)
    plans = u.plan_updates(assets, metas, False, 7, NOW)
    with pytest.raises(ValueError, match="一致しません"):
        u.apply_updates(HTML, assets, plans, metas, get=responses.__getitem__)


def test_apply_updates_rejects_bad_tarball_checksum():
    _, metas, responses = _fake_registry()
    responses["https://registry.example/1.9.4.tgz"] = _tarball({"dist/leaflet.js": b"other"})
    assets = u.find_assets(HTML)
    plans = u.plan_updates(assets, metas, False, 7, NOW)
    with pytest.raises(ValueError, match="チェックサム"):
        u.apply_updates(HTML, assets, plans, metas, get=responses.__getitem__)


def test_peer_warnings():
    metas = {
        "leaflet": _meta({"1.9.4": 300, "2.0.0": 100}),
        "geo": _meta({"4.0.0": 100}, peers={"4.0.0": {"leaflet": "^1.6.0"}}),
    }
    ok = [u.Plan("leaflet", "1.9.4", None, []), u.Plan("geo", "3.0.0", "4.0.0", [])]
    assert u.peer_warnings(ok, metas) == []
    ng = [u.Plan("leaflet", "1.9.4", "2.0.0", []), u.Plan("geo", "3.0.0", "4.0.0", [])]
    assert "geo@4.0.0" in u.peer_warnings(ng, metas)[0]


def test_main_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    _, metas, _ = _fake_registry()
    target = tmp_path / "m.py"
    target.write_bytes(HTML.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(u, "fetch_metadata", lambda pkg, get=None: metas[pkg])
    monkeypatch.setattr(u, "choose_version", lambda meta, cur, major, age, now=None: (
        ("1.9.4", []) if cur == "1.9.3" else (None, [])))
    assert u.main(["--file", str(target), "--dry-run"]) == 0
    assert "1.9.3 → 1.9.4" in capsys.readouterr().out
    assert b"leaflet@1.9.3" in target.read_bytes()


def test_main_keeps_line_endings(tmp_path, monkeypatch):
    _, metas, responses = _fake_registry()
    target = tmp_path / "m.py"
    target.write_bytes(HTML.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(u, "fetch_metadata", lambda pkg, get=None: metas[pkg])
    monkeypatch.setattr(u, "http_get", responses.__getitem__)
    real_choose = u.choose_version
    monkeypatch.setattr(u, "choose_version", lambda meta, cur, major, age, now=None: real_choose(meta, cur, major, age, NOW))
    monkeypatch.setattr(u, "download_verified", lambda meta, pkg, ver, path, get=None, tarballs=None:
                        responses[f"https://unpkg.com/{pkg}@{ver}/{path}"])
    assert u.main(["--file", str(target)]) == 0
    data = target.read_bytes()
    assert b"leaflet@1.9.4" in data
    assert data.count(b"\r\n") == HTML.count("\n") and b"\n" not in data.replace(b"\r\n", b"")


def test_bundled_html_has_pinned_assets():
    """manual_gps_tagger.py の地図ライブラリは、すべて版番号と integrity 付きで読み込んでいる。"""
    source = u.DEFAULT_TARGET.read_text(encoding="utf-8")
    assets = u.find_assets(source)
    assert {a.package for a in assets} == {"leaflet", "leaflet-control-geocoder"}
    assert source.count("https://unpkg.com/") == len(assets)
