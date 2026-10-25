# Local API Manager

把所有大模型 API 聚合到本地一个端口、一把 Key。OpenAI 与 Anthropic 双协议进出，
任意客户端 × 任意上游自由组合，配一个纯白本地控制台。

```
客户端 (Claude Code / Codex CLI / 任意 SDK)
        │  Authorization: Bearer sk-loc-…
        ▼
┌─────────────────────────────┐
│  Local API Manager :8800    │
│  /v1  /v1/messages  /console│
└─────────────────────────────┘
   │        │         │        │
   ▼        ▼         ▼        ▼
 DeepSeek  OpenAI   Codex订阅  任何第三方
 Anthropic Gemini …  ZCode     (自填 URL+Key)
```

## 特性

- **双协议网关**：OpenAI `/v1/chat/completions` 与 Anthropic `/v1/messages` 同端口；
  任一协议可打任一上游，流式 / 非流式、工具调用、思考块、图片自动互转
- **统一 Key**：所有客户端共用 `sk-loc-*` 本地 Key，可轮换、停用、按 Key 计量
- **预配置目录**：DeepSeek、OpenAI、Anthropic、Gemini、xAI、Kimi、智谱、千问、豆包、
  混元、MiniMax、SiliconFlow、阶跃、OpenRouter、OpenCode Zen，选下拉、贴 Key 即接入，
  模型池自动发现
- **Codex 订阅接入**：一键读取 `~/.codex/auth.json`（ChatGPT 订阅），refresh token 自动续期，
  模型池来自官方接口，周配额用量自动捕获显示；后端仅支持流式时自动流式聚合
- **自定义提供商**：任何 OpenAI / Anthropic 兼容服务，弹窗填 名称 / ID / URL / 协议 / Key 即可
- **智能路由**：模型名直达；同名模型多提供商按优先级故障顺序；`provider/model` 定向直达
- **用量与费用**：按日 / 提供商 / 客户端 / 模型聚合，请求级日志；订阅型计 0 费用，
  API 型按刊例价估算（币种随提供商）
- **余额查询**：DeepSeek、SiliconFlow、OpenRouter 一键查余额
- **本地控制台**：纯白、无登录、无依赖，仅监听 127.0.0.1，写操作校验同源

## 快速开始

```bash
pip install -r requirements.txt
python main.py
# 控制台  http://127.0.0.1:8800/console
```

客户端配置：

| 客户端类型 | Base URL | 备注 |
| --- | --- | --- |
| OpenAI 兼容 | `http://127.0.0.1:8800/v1` | Key 填 `sk-loc-…` |
| Anthropic 兼容 | `http://127.0.0.1:8800` | Key 填 `sk-loc-…` |

模型名即上游模型名（`deepseek-chat`、`claude-sonnet-4-5`、`gpt-6.1-sol`…），
或用 `provider/model` 定向（如 `deepseek/deepseek-chat`）。

## 后台常驻（Windows）

安装一次：登录自启 + 崩溃 3 秒内自动拉起，无窗口运行。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-daemon.ps1     # 安装
# 改代码后：双击 restart.bat 生效
powershell -ExecutionPolicy Bypass -File scripts\uninstall-daemon.ps1   # 卸载
```

日志：`data/daemon.log`（看护）、`data/server.log`（服务）。

## 数据与安全

- 所有数据（提供商 Key、日志、用量）在本机 `data/` 下的 SQLite，不上传
- 服务仅监听 `127.0.0.1`；`/api/*` 拒绝非本机 Host，写入校验同源 Origin
- 上游 Key 只存本地数据库，导出 / 备份请自行保护好 `data/` 目录

## 费用口径

按 `app/pricing.py` 的公开刊例价估算（可自行修改），仅用于看板，不代表账单；
上游返回真实 usage 时以真实值为准，流式未回报时按输出长度粗估。

## 开发

```bash
python -B tests/test_offline.py   # 38 项离线测试：双协议 × 三类上游 × 流式/非流式全矩阵
```

```
main.py            FastAPI 路由（/v1/*、/console、/api/*）
app/db.py          SQLite 存储
app/keys.py        统一本地 Key
app/presets.py     预配置目录
app/router.py      模型路由
app/pricing.py     刊例价估算
app/balance.py     余额 / Codex 配额与续期
app/proxy.py       请求管线
app/convert/       协议转换（OpenAI ⇄ Anthropic ⇄ Responses，请求/响应/流式）
static/            纯白控制台（单文件，无构建）
scripts/           Windows 看护进程与安装脚本
tests/             离线端到端测试（mock 上游）
```

## License

MIT
