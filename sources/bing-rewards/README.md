# Bing Rewards 每日任务自动化

通过 GitHub Actions 每天自动执行 Bing Rewards 搜索和每日活动，支持多账号和推送通知。

## 配置步骤

### 1. 获取账号凭证

**主方案（推荐，无需安装）：从浏览器 DevTools 复制 Cookie**

1. 打开 [bing.com](https://www.bing.com)（确保已登录微软账号）
2. 按 **F12** → **Network（网络）** → 点击任意 `bing.com` 请求
3. 在 **Request Headers** 中找到 `Cookie`，复制完整值
4. 多账号建议用无痕窗口分别登录、分别复制，避免串号

**备用方案：login_helper.py 本地脚本**

如果主方案的 cookie 过期太快或无法登录，用此方案保存完整浏览器会话（覆盖所有域名）。

`login_helper.py` 是单文件脚本，不依赖仓库其他文件，可单独下载使用。

只需安装 playwright 包，浏览器优先用系统自带的 Edge：

```bash
pip install playwright
python login_helper.py              # 主号
python login_helper.py --name 小号  # 第二个号
```

脚本会自动检测 Edge → Chrome，都没有才问你要不要下载 Chromium。

### 2. 设置 GitHub Secrets

打开 **Settings → Secrets and variables → Actions**，添加以下 Secret：

**必填：**

| Secret 名 | 说明 |
|-----------|------|
| `BING_ACCOUNTS` | 账号配置 JSON |

**选填（推送通知）：**

| Secret 名 | 说明 |
|-----------|------|
| `PUSH_METHOD` | `pushplus` / `telegram` / `wxpusher` / `serverchan` |
| `PUSHPLUS_TOKEN` | pushplus 的 token |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID |
| `WXPUSHER_SPT` | wxpusher 的 SPT |
| `SERVERCHAN_SPT` | Server 酱的 SendKey |

`BING_ACCOUNTS` 格式：

**主方案（cookies 字段）：**
```json
[
  {"name": "主号", "cookies": "MUID=xxx; _U=yyy; SRCHD=zzz; ...", "appkey": ""},
  {"name": "小号", "cookies": "MUID=aaa; _U=bbb; ...", "appkey": "你的appkey"}
]
```

**备用方案（session 字段，优先级更高）：**

`login_helper.py` 输出的是单个账号对象。使用时需要包在 `[ ]` 里。

单账号：
```json
[<粘贴 txt 文件全部内容>]
```

多账号（逗号分隔）：
```json
[<主号文件内容>, <小号文件内容>]
```

- `name`：账号备注
- `cookies`：DevTools 复制的 Cookie 字符串（主方案）
- `session`：login_helper.py 生成的完整会话（备用方案，有 session 时优先使用）
- `appkey`：热门词 API key，留空也能用，需要去 https://www.gmya.net/api 免费申请

### 3. 测试

**Actions** → **Bing Rewards Daily** → **Run workflow**。

## 运行时间

每天北京时间 **00:10** 自动运行。失败可早上手动补跑。

## 配置项

`bing_rewards.py` 开头有中文注释：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_SEARCHES` | 30 | 每日搜索次数 |
| `MIN_SEARCH_DELAY` | 10 | 搜索间隔下限（秒） |
| `MAX_SEARCH_DELAY` | 20 | 搜索间隔上限（秒） |
| `ACCOUNT_COOLDOWN` | 300 | 账号间冷却（秒） |

单账号约 10-15 分钟完成，跟真人操作节奏一致。

## 会话过期

收到推送通知说某号过期后：
- 主方案：重新 F12 复制 Cookie，更新 `BING_ACCOUNTS`
- 备用方案：重新运行 `login_helper.py`，更新 session
