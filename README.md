# TokenLens

AI / Agent Token 消耗统计看板：自动扫描本机 AI 编程工具的日志，聚合 token 用量、估算费用，并跟踪订阅套餐配额。纯本地运行，数据不上传。

## 功能

- **自动采集**：增量扫描 Kimi Code / Claude Code / Codex CLI 的本地 jsonl 日志（只读，不改动原数据），聚合成 日 × Agent × 模型 用量
- **费用估算**：内置常见模型参考价；可从 [models.dev](https://models.dev) 一键导入目录价，新模型出现时后台自动匹配补价，也可手动定价
- **配额跟踪**：Kimi For Coding 官方接口实时查询（含跨设备校准的绝对额度估算）、Codex 本地限额记录、手动配额（固定额度或百分比锚定）
- **时间线明细**：任意一天按小时、任意小时按分钟查看用量
- **运行形态**：优先 pywebview 原生窗口，未安装则自动回退为浏览器打开

## 快速开始

**Windows 免安装**：从 Releases 下载 `TokenLens.exe` 双击运行。

**源码运行**（Python 3.8+，仅依赖标准库）：

```bash
python server.py          # 或双击 启动.bat
python server.py --demo   # 先生成 45 天演示数据再启动
```

可选安装 `pywebview` 以原生窗口运行：

```bash
pip install -r requirements.txt
```

数据保存在程序同目录的 `data/`（首次运行自动创建）。

## 采集原理

各 AI 工具会把会话记录（含模型名与 token 用量）写入本地 jsonl 日志，TokenLens 每 10 分钟增量扫描一次：

| 工具 | 日志位置 | 依据事件 |
| --- | --- | --- |
| Kimi Code | `~/.kimi-code/sessions/*/*/agents/*/wire.jsonl` | `usage.record` |
| Claude Code | `~/.claude/projects/*/*.jsonl` | assistant 消息 `usage`（按 message id 去重） |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | `token_count` |

Cursor 等不在本地记录 token 明细的工具无法自动统计，可在界面中手动录入。

## 隐私与安全

- 服务只监听 `127.0.0.1`，并校验 `Host` / `Origin`，可挡 DNS rebinding 与跨站请求
- `data/` 目录包含你的真实使用数据（含本机文件路径），**不要提交到公开仓库**——本仓库 `.gitignore` 已默认忽略
- 唯二的外网请求：models.dev 价格目录、Kimi 官方配额接口（使用本机 Kimi CLI 的 OAuth 凭证）

## 开发

```bash
python -m unittest discover -s tests   # 运行测试
```

打包 exe 使用 PyInstaller（静态资源打进 exe，数据文件落在 exe 同目录）。

## License

[MIT](LICENSE)
