# nanobot-expend

`nanobot-expend` 是我这套 nanobot 的拓展能力仓库。它不放 nanobot 本体，只收纳我们自己做出来的 sidecar、skill、部署脚本和运维胶水，让 `nanobot-exp` 尽量保持贴近上游，方便以后同步官方更新。

## 仓库定位

- `nanobot-exp`：保留 nanobot 本体，以及少量必要的适配补丁。
- `nanobot-expend`：放我们自己的外挂能力，比如 OBP、RSS、LOF、Notify、Gemini FastAPI Rust、QQ sidecar、知识收件箱等。
- 敏感配置不进 Git：真实 cookie、token、API key、Basic Auth、运行数据库和统计文件都只留在线上机器。

## 目录结构

```text
config/      示例配置、能力矩阵、sidecar 注册表
scripts/     同步、部署、冒烟测试、CI 检查脚本
sbin/        服务器侧维护工具
systemd/     systemd unit 和 drop-in 配置
sources/     各个 sidecar / skill / 脚本源码
```

## 主要能力

- `sources/obp-rs`：OBP 智能模型网关，负责模型路由、来源统计、成本账本、降级/兜底。
- `sources/gemini-fastapi-rs`：Gemini Web FastAPI 的 Rust 实现，面向 OpenAI 兼容调用。
- `sources/wechat-rss-rs`：文章/RSS sidecar，包含微信文章、鸭哥 AI 要闻、付费文章清洗器、QQ 推送格式化。
- `sources/lof-sidecar-rs`：LOF 投资看板和估值刷新 sidecar。
- `sources/notify-sidecar-rs`：定时任务桥，把原来 nanobot cron 里适合外置的任务迁出来。
- `sources/trend-sidecar-rs`：Trend Radar Lite，热点收集、历史榜、每日简报。
- `sources/qq-sidecar-rs`：QQ 消息签名校验/安全辅助 sidecar。
- `sources/nanobot-reflexio-rs`：本地记忆/Reflexio 实验 sidecar。
- `sources/*-skill`：nanobot skill 和脚本能力。

## 质量门禁

GitHub Actions 会跑一套轻量检查：

```bash
python3 scripts/ci-secret-scan.py
python3 scripts/ci-python-compile.py
bash scripts/ci-rust-crates.sh
```

它们分别检查：

- 是否误提交疑似真实密钥、cookie、Authorization 头。
- Python 脚本是否存在语法错误。
- `sources/` 下每个 Rust crate 是否能用 `cargo test --locked` 跑过。

本地修改后，建议先跑同样的命令再推送。

## 安全规则

以下内容不要提交：

- `.env`、`*.env`
- `data/config.json`、`data/router.json`、`data/stats.json`
- `runtime/`、`backups/`、`target/`
- cookie、token、API key、Basic Auth 密码
- 线上日志、数据库、缓存和临时文件

如果需要示例配置，请使用 `*.example.json` 或占位符，例如 `YOUR_TOKEN_HERE`。

## 和 nanobot-exp 的关系

这套仓库的目标是让 nanobot 本体更干净：能不改本体就不改本体，优先通过 sidecar、skill、脚本和管理页扩展能力。以后同步上游 nanobot 时，核心逻辑尽量在 `nanobot-exp` 合并，个性化能力尽量在 `nanobot-expend` 维护。