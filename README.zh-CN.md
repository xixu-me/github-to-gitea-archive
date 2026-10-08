# GitHub to Gitea Archive

将任意 GitHub 用户或组织所拥有的仓库持续归档到同一台服务器，并通过 Gitea 浏览。来源始终是 GitHub，服务不会向 GitHub 仓库写回内容。

支持代码、分支、标签、PR 引用、可访问的 wiki，以及 issues、评论、时间线、reactions、PR 详情与 reviews、releases、labels 和 milestones。附件、release 二进制和 LFS 对象仅保留链接、指针与清单，不下载文件内容。无法完整表示成 Gitea 原生 PR 的记录保留在原始归档中，并明确说明差异。

账号、Gitea 命名空间、管理用户、域名、数据库和仓库目录、端口、磁盘预算均可配置。支持只读 GitHub App、PAT，以及无需凭据的公开账号轮询。私有仓库必须具备相应读取权限。

运行环境为 Linux/systemd/Nginx、Python 3.11+、Git、OpenSSL，以及同机的 Gitea 28 + SQLite WAL；Python 运行时仅使用标准库。一套实例归档一个账号，更换账号须使用独立的数据目录和 Gitea 命名空间。

```sh
git clone https://github.com/xixu-me/github-to-gitea-archive.git
cd github-to-gitea-archive
python3 -m unittest discover -s tests -p 'test_*.py'
sudo python3 scripts/install.py
sudoedit /etc/gitea/github-archive.env
```

按 [部署文档](docs/deployment.md) 完成 Gitea、Nginx、只读 App/PAT 配置并启用定时器。安装器不会启动服务，不覆盖已有环境文件，不修改 Gitea 配置，也不会安装或升级 Gitea。

保留了独立 webhook 收件库、隐私变更访问保护、自动重试、磁盘空间保护、原生 Git hooks 刷新和本机恢复快照。快照不提供整台服务器丢失后的容灾；原始 JSON 保留最新观察版本，不保证保存每次历史编辑。详细能力、边界与验收方式见 [英文 README](README.md)、[配置](docs/configuration.md)、[架构](docs/architecture.md) 和 [运维恢复](docs/operations.md)。

MIT 开源。请勿将环境文件、密钥、数据库、归档记录或验收报告提交到公开仓库。
