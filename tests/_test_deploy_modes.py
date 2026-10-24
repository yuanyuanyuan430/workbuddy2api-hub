"""quick-deploy.sh 的部署模式（#512）。

脚本本身需要 Docker 才能跑，这里做源码级断言：模式解析、端口绑定、公网提示
与文档一致性。任何一条失配都意味着「一键部署到公网」会静默回到监听所有网卡。

    python tests/_test_deploy_modes.py
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class DeployModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = read("quick-deploy.sh")
        cls.readme = read("README.md")

    def test_default_stays_lan(self):
        self.assertIn('DEPLOY_MODE="${WB_DEPLOY_MODE:-lan}"', self.script)

    def test_only_three_modes_are_accepted(self):
        self.assertIn("local|lan|public) ;;", self.script)
        self.assertIn("WB_DEPLOY_MODE 只能是 local / lan / public", self.script)

    def test_loopback_modes_bind_the_host_port_to_localhost(self):
        self.assertIn('PORT_BINDING="127.0.0.1:8788:8788"', self.script)
        self.assertIn('PORT_BINDING="8788:8788"', self.script)
        # 容器内仍要监听 0.0.0.0，否则 Docker 的端口转发连不进来。
        self.assertIn("- HOST=0.0.0.0", self.script)

    def test_generated_compose_uses_the_binding(self):
        self.assertIn('      - "${PORT_BINDING}"', self.script)
        self.assertNotIn('      - "8788:8788"', self.script)

    def test_upgrade_does_not_rewrite_an_existing_compose(self):
        self.assertIn("已有部署不会自动改写端口映射", self.script)

    def test_deploy_defaults_pull_the_image_this_repo_builds(self):
        # 部署产物（compose / 一键脚本 / 面板页头链接）必须指向本仓库：
        # 指向上游会让「装好之后点一键更新」把上游构建覆盖回来。
        image = "ghcr.io/yuanyuanyuan430/workbuddy2api-hub:latest"
        self.assertIn("    image: " + image, read("docker-compose.yml"))
        self.assertIn("    image: " + image, self.script)
        self.assertIn("\n    " + image, self.readme)
        self.assertIn(
            'href="https://github.com/yuanyuanyuan430/workbuddy2api-hub"',
            read("dashboard.html"))

    def test_public_mode_explains_the_reverse_proxy_requirement(self):
        self.assertIn("请用 Nginx / Caddy / Cloudflare Tunnel 反代", self.script)
        self.assertIn("WB_DEPLOY_MODE=public bash", self.readme)


if __name__ == "__main__":
    unittest.main()
