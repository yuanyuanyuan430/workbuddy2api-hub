"""Portable-package updater (issue #560).

These tests never touch a real gateway or GitHub: the zip is built locally,
downloads use file:// URLs, and stop/start/health are patched. They pin the
parts that can destroy an install: path traversal, version mismatch, backup,
replace, and rollback.
"""
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_update_portable as U


def proxy_source(version):
    return 'server_version = "wb-proxy/%s"\n' % version


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def make_zip(path, version, files, profiles=None):
    manifest = {
        "schema": 1,
        "version": version,
        "root": "wb-proxy",
        "files": list(files),
        "protected": ["accounts/", "usage/"],
    }
    if profiles is not None:
        manifest["profiles"] = profiles
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("release-manifest.json",
                    json.dumps(manifest, ensure_ascii=False))
        for rel, data in files.items():
            zf.writestr("wb-proxy/" + rel, data)
    return path


def new_files(version):
    return {
        "wb_proxy.py": proxy_source(version),
        "dashboard.html": "<html>%s</html>" % version,
        "wb_update_apply.py": "# helper %s\n" % version,
    }


class ChecksumTests(unittest.TestCase):
    def test_parse_checksums(self):
        text = "%s  workbuddy2api-hub-v2.0.0.zip\n%s *other.ipk\n" % ("a" * 64, "b" * 64)
        parsed = U.parse_checksums(text)
        self.assertEqual(parsed["workbuddy2api-hub-v2.0.0.zip"], "a" * 64)
        self.assertEqual(parsed["other.ipk"], "b" * 64)

    def test_asset_urls(self):
        zip_url, sha_url, name = U.asset_urls("o/r", "v2.0.0", "2.0.0")
        self.assertEqual(name, "workbuddy2api-hub-v2.0.0.zip")
        self.assertEqual(zip_url, "https://github.com/o/r/releases/download/v2.0.0/" + name)
        self.assertEqual(sha_url, "https://github.com/o/r/releases/download/v2.0.0/SHA256SUMS")


class UpdateSourceTests(unittest.TestCase):
    """便携版一键更新与更新检查必须指向同一个仓库。

    两处默认值分散在 wb_updates / wb_update_portable，任一处漏改都会让面板
    「检查到的是本仓库、装进来的是上游包」这种最难排查的组合出现。
    """

    def test_the_portable_updater_pulls_from_the_maintained_repository(self):
        self.assertEqual(U.REPO_DEFAULT, "yuanyuanyuan430/workbuddy2api-hub")

    def test_the_portable_updater_agrees_with_the_checker(self):
        import wb_updates
        self.assertEqual(U.REPO_DEFAULT, wb_updates.DEFAULT_REPOSITORY)

    def test_asset_urls_pull_from_that_repository(self):
        zip_url, sha_url, name = U.asset_urls(U.REPO_DEFAULT, "v9.9.9", "9.9.9")
        base = "https://github.com/%s/releases/download/v9.9.9" % U.REPO_DEFAULT
        self.assertEqual(zip_url, base + "/" + name)
        self.assertEqual(sha_url, base + "/SHA256SUMS")

class ExtractTests(unittest.TestCase):
    def test_safe_extract_keeps_manifest_and_root_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("release-manifest.json", "{}")
                zf.writestr("wb-proxy/a.txt", "ok")
                zf.writestr("other/b.txt", "drop")
            out = os.path.join(d, "out")
            U.safe_extract(path, out, "wb-proxy")
            self.assertTrue(os.path.isfile(os.path.join(out, "wb-proxy", "a.txt")))
            self.assertFalse(os.path.exists(os.path.join(out, "other")))

    def test_safe_extract_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("../evil.txt", "x")
            with self.assertRaises(U.UpdateError):
                U.safe_extract(path, os.path.join(d, "out"), "wb-proxy")

    def test_safe_extract_preserves_unix_modes(self):
        # 启动脚本在包里是 755，解包后必须保持可执行：否则一键更新在
        # macOS/Linux 的重启一步 Popen() 报 EACCES，整次更新被迫回滚。
        # 没有 unix 属性的条目维持默认权限，不应被凭空赋予执行位。
        # The launchers ship 755 and the restart Popen()s them, so a 644 copy
        # rolls the whole update back (Errno 13). Entries without unix attrs
        # must not gain an executable bit out of nowhere.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "modes.zip")
            with zipfile.ZipFile(path, "w") as zf:
                info = zipfile.ZipInfo("wb-proxy/start-wb-proxy.sh")
                info.external_attr = 0o755 << 16
                zf.writestr(info, "#!/bin/sh\n")
                info = zipfile.ZipInfo("wb-proxy/wb_proxy.py")
                info.external_attr = 0o644 << 16
                zf.writestr(info, "x = 1\n")
                info = zipfile.ZipInfo("wb-proxy/plain.txt")
                info.external_attr = 0
                zf.writestr(info, "plain\n")
            out = os.path.join(d, "out")
            U.safe_extract(path, out, "wb-proxy")
            if os.name == "nt":
                # Windows stores no exec bit (chmod only maps the read-only
                # bit); extraction succeeding without an error is the contract.
                self.assertTrue(os.path.isfile(
                    os.path.join(out, "wb-proxy", "start-wb-proxy.sh")))
                return
            self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(
                out, "wb-proxy", "start-wb-proxy.sh")).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(
                out, "wb-proxy", "wb_proxy.py")).st_mode), 0o644)
            self.assertFalse(stat.S_IMODE(os.lstat(os.path.join(
                out, "wb-proxy", "plain.txt")).st_mode) & 0o111)


class StageTests(unittest.TestCase):
    def test_validate_stage(self):
        with tempfile.TemporaryDirectory() as d:
            stage = os.path.join(d, "stage")
            files = new_files("2.0.0")
            manifest = {"version": "2.0.0", "root": "wb-proxy",
                        "files": list(files)}
            os.makedirs(os.path.join(stage, "wb-proxy"))
            for rel, data in files.items():
                with open(os.path.join(stage, "wb-proxy", rel), "w",
                          encoding="utf-8") as fh:
                    fh.write(data)
            with open(os.path.join(stage, "release-manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(manifest, fh)
            got = U.validate_stage(stage, "2.0.0", "wb-proxy")
            self.assertEqual(got["version"], "2.0.0")
            with self.assertRaises(U.UpdateError):
                U.validate_stage(stage, "3.0.0", "wb-proxy")


class ApplyTests(unittest.TestCase):
    def _layout(self, d):
        package = os.path.join(d, "package")
        root = os.path.join(package, "wb-proxy")
        os.makedirs(os.path.join(root, "accounts"))
        with open(os.path.join(root, "wb_proxy.py"), "w", encoding="utf-8") as fh:
            fh.write(proxy_source("1.0.0"))
        with open(os.path.join(root, "dashboard.html"), "w", encoding="utf-8") as fh:
            fh.write("old")
        with open(os.path.join(root, "accounts", "keep.json"), "w", encoding="utf-8") as fh:
            fh.write("keep")
        with open(os.path.join(package, "release-manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": "1.0.0", "root": "wb-proxy",
                       "files": ["wb_proxy.py", "dashboard.html"]}, fh)
        return package, root

    def test_backup_apply_restore(self):
        with tempfile.TemporaryDirectory() as d:
            package, root = self._layout(d)
            stage = os.path.join(d, "stage")
            files = new_files("2.0.0")
            os.makedirs(os.path.join(stage, "wb-proxy"))
            for rel, data in files.items():
                with open(os.path.join(stage, "wb-proxy", rel), "w",
                          encoding="utf-8") as fh:
                    fh.write(data)
            manifest = {"version": "2.0.0", "root": "wb-proxy",
                        "files": list(files)}
            with open(os.path.join(stage, "release-manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(manifest, fh)
            backup, saved, absent = U.backup_current(
                root, package, manifest["files"], "2.0.0")
            U.apply_stage(stage, root, package, manifest)
            self.assertIn("2.0.0", read(os.path.join(root, "wb_proxy.py")))
            self.assertEqual(read(os.path.join(root, "accounts", "keep.json")),
                             "keep")
            U.restore_backup(backup, root, package, saved, absent)
            self.assertIn("1.0.0", read(os.path.join(root, "wb_proxy.py")))
            self.assertEqual(read(os.path.join(root, "dashboard.html")), "old")

    def test_run_update_success_and_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            package, root = self._layout(d)
            files = new_files("2.0.0")
            zip_path = os.path.join(d, "new.zip")
            make_zip(zip_path, "2.0.0", files)
            with open(zip_path, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            sha_path = os.path.join(d, "SHA256SUMS")
            with open(sha_path, "w", encoding="utf-8") as fh:
                fh.write("%s  new.zip\n" % sha)
            status_file = os.path.join(root, "updates", "status.json")

            def fake_urls(repo, tag, version):
                return ("file:///" + zip_path.replace(os.sep, "/"),
                        "file:///" + sha_path.replace(os.sep, "/"),
                        "new.zip")

            with mock.patch.object(U, "asset_urls", fake_urls), \
                    mock.patch.object(U, "status_path", lambda: status_file), \
                    mock.patch.object(U, "is_visible_process", lambda pid: False), \
                    mock.patch.object(U, "stop_gateway", lambda pid: None), \
                    mock.patch.object(U, "wait_port_free", lambda port: True), \
                    mock.patch.object(U, "start_service", lambda *a: None), \
                    mock.patch.object(U, "wait_health", lambda port, **kw: True):
                code = U.run_update(root, package, "v2.0.0", "2.0.0",
                                    8788, False, 1234, "o/r")
            self.assertEqual(code, 0)
            with open(status_file, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["state"], "succeeded")
            self.assertIn("2.0.0", read(os.path.join(root, "wb_proxy.py")))

            # A second run whose new build never becomes healthy must roll back.
            package, root = self._layout(os.path.join(d, "second"))
            status_file = os.path.join(root, "updates", "status.json")
            with mock.patch.object(U, "asset_urls", fake_urls), \
                    mock.patch.object(U, "status_path", lambda: status_file), \
                    mock.patch.object(U, "is_visible_process", lambda pid: False), \
                    mock.patch.object(U, "stop_gateway", lambda pid: None), \
                    mock.patch.object(U, "wait_port_free", lambda port: True), \
                    mock.patch.object(U, "start_service", lambda *a: None), \
                    mock.patch.object(U, "wait_health", lambda port, **kw: False):
                code = U.run_update(root, package, "v2.0.0", "2.0.0",
                                    8788, False, 1234, "o/r")
            self.assertEqual(code, 1)
            with open(status_file, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["state"], "rolled_back")
            self.assertIn("1.0.0", read(os.path.join(root, "wb_proxy.py")))


class ProfileTests(unittest.TestCase):
    """#573：镜像 / rootfs 部署只拥有应用层。

    便携包按整份清单更新；镜像部署没有 start-wb-proxy*、ops/ 和随包价格快照，
    把它们写进去只会多出镜像本来没有的文件，其中 pricing/pricing.json 还会盖掉
    wb_pricing.py 的内联快照且两份永不同步。
    """

    ALL = {
        "wb_proxy.py": proxy_source("2.0.0"),
        "wb_update_apply.py": "# helper 2.0.0\n",
        "dashboard.html": "<html>2.0.0</html>",
        "start-wb-proxy.bat": "@echo off\r\n",
        "pricing/pricing.json": '{"generated_at": "2.0.0"}',
    }
    APP = ["wb_proxy.py", "wb_update_apply.py", "dashboard.html"]

    def _manifest(self):
        return {"version": "2.0.0", "root": "wb-proxy",
                "files": list(self.ALL), "profiles": {"app": self.APP}}

    def _stage(self, directory):
        stage = os.path.join(directory, "stage")
        os.makedirs(os.path.join(stage, "wb-proxy", "pricing"))
        for rel, data in self.ALL.items():
            with open(os.path.join(stage, "wb-proxy", rel), "w",
                      encoding="utf-8") as fh:
                fh.write(data)
        manifest = self._manifest()
        with open(os.path.join(stage, "release-manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh)
        return stage, manifest

    def test_profile_selects_the_app_layer_in_manifest_order(self):
        self.assertEqual(U.profile_files(self._manifest(), "app"), self.APP)
        # No profile keeps the historical full list, so an executor that
        # predates this feature behaves exactly as before.
        self.assertEqual(U.profile_files(self._manifest()), list(self.ALL))

    def test_unknown_or_incomplete_profile_is_refused(self):
        with self.assertRaises(U.UpdateError):
            U.profile_files(self._manifest(), "image")
        with self.assertRaises(U.UpdateError):
            U.profile_files({"version": "2.0.0", "root": "wb-proxy",
                             "files": ["wb_proxy.py"]}, "app")
        with self.assertRaises(U.UpdateError):
            U.profile_files({"version": "2.0.0", "root": "wb-proxy",
                             "files": list(self.ALL),
                             "profiles": {"app": ["dashboard.html"]}}, "app")

    def test_apply_with_profile_does_not_write_portable_only_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "app")
            os.makedirs(root)
            stage, manifest = self._stage(d)
            U.apply_stage(stage, root, d, manifest,
                          U.profile_files(manifest, "app"))
            self.assertIn("2.0.0", read(os.path.join(root, "wb_proxy.py")))
            self.assertFalse(os.path.exists(
                os.path.join(root, "start-wb-proxy.bat")))
            self.assertFalse(os.path.exists(
                os.path.join(root, "pricing", "pricing.json")))

    def test_a_profile_backup_is_still_restorable(self):
        # The backup manages fewer files than the manifest lists; the count
        # check has to use backup.json's own `files`, not the manifest.
        with tempfile.TemporaryDirectory() as d:
            package = os.path.join(d, "package")
            root = os.path.join(package, "app")
            os.makedirs(root)
            with open(os.path.join(root, "wb_proxy.py"), "w",
                      encoding="utf-8") as fh:
                fh.write(proxy_source("1.0.0"))
            with open(os.path.join(package, "release-manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"version": "1.0.0", "root": "wb-proxy",
                           "files": list(self.ALL)}, fh)
            backup, saved, absent = U.backup_current(
                root, package, self.APP, "2.0.0")
            self.assertEqual(len(saved) + len(absent), len(self.APP))
            ok, reason = U.backup_complete(backup, "2.0.0")
            self.assertTrue(ok, reason)

    def test_run_update_with_app_profile_skips_portable_only_files(self):
        with tempfile.TemporaryDirectory() as d:
            package = os.path.join(d, "package")
            root = os.path.join(package, "app")
            os.makedirs(root)
            with open(os.path.join(root, "wb_proxy.py"), "w",
                      encoding="utf-8") as fh:
                fh.write(proxy_source("1.0.0"))
            with open(os.path.join(package, "release-manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"version": "1.0.0", "root": "wb-proxy",
                           "files": list(self.ALL)}, fh)
            zip_path = os.path.join(d, "new.zip")
            make_zip(zip_path, "2.0.0", self.ALL,
                     profiles={"app": self.APP})
            with open(zip_path, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            sha_path = os.path.join(d, "SHA256SUMS")
            with open(sha_path, "w", encoding="utf-8") as fh:
                fh.write("%s  new.zip\n" % sha)
            status_file = os.path.join(root, "updates", "status.json")

            def fake_urls(repo, tag, version):
                return ("file:///" + zip_path.replace(os.sep, "/"),
                        "file:///" + sha_path.replace(os.sep, "/"),
                        "new.zip")

            with mock.patch.object(U, "asset_urls", fake_urls), \
                    mock.patch.object(U, "status_path", lambda: status_file), \
                    mock.patch.object(U, "is_visible_process", lambda pid: False), \
                    mock.patch.object(U, "stop_gateway", lambda pid: None), \
                    mock.patch.object(U, "wait_port_free", lambda port: True), \
                    mock.patch.object(U, "start_service", lambda *a: None), \
                    mock.patch.object(U, "wait_health", lambda port, **kw: True):
                code = U.run_update(root, package, "v2.0.0", "2.0.0",
                                    8788, False, 1234, "o/r", profile="app")
            self.assertEqual(code, 0)
            self.assertIn("2.0.0", read(os.path.join(root, "wb_proxy.py")))
            self.assertFalse(os.path.exists(
                os.path.join(root, "start-wb-proxy.bat")))
            self.assertFalse(os.path.exists(
                os.path.join(root, "pricing", "pricing.json")))


class RequestTests(unittest.TestCase):
    def test_bridge_reports_portable_install_enabled(self):
        import wb_update_request as R
        with mock.patch.object(R, "_systemd_enabled", return_value=False), \
                mock.patch.object(R.wb_update_portable, "available",
                                  return_value=True), \
                mock.patch.object(R.wb_update_portable, "status",
                                  return_value={"state": "idle", "version": "",
                                                "message": ""}):
            self.assertTrue(R.install_enabled())
            self.assertEqual(R.status(), {
                "install_enabled": True,
                "installation": {"state": "idle", "version": "",
                                 "message": ""},
            })

    def test_unavailable_in_a_source_checkout(self):
        self.assertFalse(U.available())
        with self.assertRaises(U.UpdateError):
            U.request_install("v2.0.0", "1.0.0")

    def test_request_spawns_helper_with_port_and_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "wb-proxy")
            os.makedirs(root)
            open(os.path.join(root, "wb_update_apply.py"), "w").close()
            status_file = os.path.join(root, "updates", "status.json")
            with mock.patch.object(U, "available", lambda: True), \
                    mock.patch.object(U, "app_root", lambda: root), \
                    mock.patch.object(U, "package_root", lambda: d), \
                    mock.patch.object(U, "status_path", lambda: status_file), \
                    mock.patch.object(U.subprocess, "Popen") as popen:
                result = U.request_install("v2.0.0", "1.0.0",
                                           port=8899, lan=True)
                self.assertEqual(result["state"], "queued")
                cmd = popen.call_args[0][0]
                self.assertIn("--tag", cmd)
                self.assertIn("v2.0.0", cmd)
                self.assertIn("--port", cmd)
                self.assertIn("8899", cmd)
                self.assertIn("--lan", cmd)
                self.assertIn("1", cmd)
                self.assertEqual(U.status()["state"], "queued")
                with self.assertRaises(U.UpdateError):
                    U.request_install("v1.0.0", "1.0.0")


class ExternalBridgeTests(unittest.TestCase):
    def test_external_enabled_needs_env_and_directory(self):
        import wb_update_request as R
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(R, "REQUEST_DIR", d), \
                    mock.patch.dict(os.environ, {
                        "WB_UPDATE_INSTALL_ENABLED": "1",
                        "WB_UPDATE_EXECUTOR": "external"}, clear=False):
                self.assertTrue(R.external_enabled())
                self.assertTrue(R.install_enabled())
            with mock.patch.object(R, "REQUEST_DIR", d), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(R.external_enabled())

    def test_executor_marker_registers_without_env(self):
        import wb_update_request as R
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "executor.json"), "w") as fh:
                fh.write("{}")
            with mock.patch.object(R, "REQUEST_DIR", d), \
                    mock.patch.dict(os.environ, {
                        "WB_UPDATE_INSTALL_ENABLED": "1"}, clear=True):
                self.assertTrue(R.external_enabled())

    def test_request_writes_pid_port_and_root(self):
        import wb_update_request as R
        with tempfile.TemporaryDirectory() as d:
            request_file = os.path.join(d, "request.json")
            status_file = os.path.join(d, "status.json")
            with mock.patch.object(R, "REQUEST_DIR", d), \
                    mock.patch.object(R, "REQUEST_FILE", request_file), \
                    mock.patch.object(R, "STATUS_FILE", status_file), \
                    mock.patch.object(R, "_systemd_enabled",
                                      return_value=False), \
                    mock.patch.object(R, "external_enabled",
                                      return_value=True), \
                    mock.patch.object(R, "install_enabled",
                                      return_value=True):
                queued = R.request_install("v2.0.0", "1.0.0", port=8899)
                self.assertEqual(queued["state"], "queued")
                with open(request_file, encoding="utf-8") as fh:
                    payload = json.load(fh)
                self.assertEqual(payload["tag"], "v2.0.0")
                self.assertEqual(payload["version"], "2.0.0")
                self.assertEqual(payload["port"], 8899)
                self.assertEqual(payload["pid"], os.getpid())
                self.assertTrue(os.path.isdir(payload["root"]))
                # 非便携包部署（镜像 / 自建 rootfs）只拥有应用层（#573）。
                self.assertEqual(payload.get("profile"), "app")
                self.assertEqual(R.status()["installation"]["state"], "queued")

    def test_portable_request_carries_no_profile(self):
        # 便携包照旧按整份清单更新；多带一个档位会让它少写 start-wb-proxy*。
        import wb_update_request as R
        with mock.patch.object(R.wb_update_portable, "app_root",
                               return_value="/pkg/wb-proxy"), \
                mock.patch.object(R.wb_update_portable, "package_root",
                                  return_value="/pkg"):
            self.assertNotIn("profile", R._request_fields("2.0.0", 8788))

    def test_external_executor_reads_and_unlinks_the_request(self):
        import wb_update_external as E
        with tempfile.TemporaryDirectory() as d:
            request_file = os.path.join(d, "request.json")
            with open(request_file, "w", encoding="utf-8") as fh:
                json.dump({"tag": "v2.0.0", "version": "2.0.0",
                           "pid": 1234, "port": 8788, "root": d}, fh)
            os.chmod(request_file, 0o600)
            payload = E.read_request(request_file)
            self.assertEqual(payload["tag"], "v2.0.0")
            self.assertFalse(os.path.exists(request_file))

    def test_external_executor_calls_run_update_without_start(self):
        import wb_update_external as E
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "wb_proxy.py"), "w") as fh:
                fh.write("# app\n")
            request_file = os.path.join(d, "request.json")
            with open(request_file, "w", encoding="utf-8") as fh:
                json.dump({"tag": "v2.0.0", "version": "2.0.0",
                           "pid": 1234, "port": 8788, "root": d}, fh)
            os.chmod(request_file, 0o600)
            with mock.patch.object(E, "run_update", return_value=0) as run:
                code = E.main(["--request-dir", d])
            self.assertEqual(code, 0)
            kwargs = run.call_args.kwargs
            self.assertIsNone(kwargs["start_service_fn"])
            self.assertIsNone(kwargs["profile"])
            self.assertEqual(kwargs["status_file"],
                             os.path.join(d, "status.json"))
            self.assertEqual(run.call_args.args[:2], (d, d))

    def test_external_executor_passes_the_requested_profile(self):
        import wb_update_external as E
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "wb_proxy.py"), "w") as fh:
                fh.write("# app\n")
            request_file = os.path.join(d, "request.json")
            with open(request_file, "w", encoding="utf-8") as fh:
                json.dump({"tag": "v2.0.0", "version": "2.0.0", "pid": 1234,
                           "port": 8788, "root": d, "profile": "app"}, fh)
            os.chmod(request_file, 0o600)
            with mock.patch.object(E, "run_update", return_value=0) as run:
                self.assertEqual(E.main(["--request-dir", d]), 0)
            self.assertEqual(run.call_args.kwargs["profile"], "app")


    def test_external_executor_recovers_a_half_applied_update(self):
        import wb_update_external as E
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "app")
            os.makedirs(os.path.join(root, "updates", "backup-2.0.0"))
            with open(os.path.join(root, "wb_proxy.py"), "w") as fh:
                fh.write("half\n")
            with open(os.path.join(root, "updates", "backup-2.0.0",
                                   "wb_proxy.py"), "w") as fh:
                fh.write("old\n")
            with open(os.path.join(root, "updates", "backup-2.0.0",
                                   "backup.json"), "w") as fh:
                json.dump({"version": "2.0.0", "saved": ["wb_proxy.py"],
                           "absent": []}, fh)
            status_file = os.path.join(d, "status.json")
            with open(status_file, "w") as fh:
                json.dump({"state": "applying", "version": "2.0.0"}, fh)
            code = E.main(["--request-dir", d, "--root", root])
            self.assertEqual(code, 0)
            self.assertEqual(read(os.path.join(root, "wb_proxy.py")), "old\n")
            with open(status_file, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["state"], "rolled_back")


if __name__ == "__main__":
    unittest.main()

