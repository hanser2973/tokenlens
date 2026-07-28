# TokenLens

AI / Agent Token 消耗统计看板：自动扫描本机 AI 编程工具的日志，聚合 token 用量、估算费用，并跟踪订阅套餐配额。纯本地运行，数据不上传。

## 功能

- **自动采集**：增量扫描各工具的本地日志（只读，不改动原数据），聚合成 日 × Agent × 模型 × 项目 用量
- **项目维度**：按工作目录区分项目，看清哪个项目最烧钱；支持按项目筛选全站数据
- **环比对比**：统计卡显示与紧邻的等长上一周期的涨跌幅
- **费用估算**：内置常见模型参考价；可从 [models.dev](https://models.dev) 一键导入目录价，新模型出现时后台自动匹配补价，也可手动定价
- **配额跟踪**：Kimi For Coding 官方接口实时查询（含跨设备校准的绝对额度估算）、Codex 本地限额记录、手动配额（固定额度或百分比锚定）；每个窗口可单独「停止 / 开始估计」——多端环境下本机观测口径未必干净时，可以只看官方百分比，或从当前时刻重新锚定
- **时间线明细**：任意一天按小时、任意小时按分钟查看用量（近 10 天直接查分钟桶，更早的日期回退为重扫日志）
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

重复启动不会开出第二个实例：新进程探测到已有 TokenLens 在跑时，会直接打开它的界面并退出——两个进程共用同一份采集状态会导致漏扫或重复计数。

## 采集原理

各 AI 工具会把会话记录（含模型名与 token 用量）写入本地日志，TokenLens 每 10 分钟增量扫描一次：

| 工具 | 日志位置 | 依据事件 | 项目来源 |
| --- | --- | --- | --- |
| Kimi Code | `~/.kimi-code/sessions/*/*/agents/*/wire.jsonl` | `usage.record` | 会话目录名 `wd_<项目>_<hash>` |
| Claude Code | `~/.claude/projects/*/*.jsonl` | assistant 消息 `usage`（按 message id 去重） | 每行的 `cwd` |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | `token_count` | `session_meta` / `turn_context` 的 `cwd` |
| Cline / Roo Code / Kilo Code | 各 VS Code 系编辑器 `globalStorage/<扩展>/tasks/*/ui_messages.json` | `api_req_started` | 不记录，归入「未知项目」 |
| opencode | `~/.local/share/opencode/storage/message/*/*.json` | assistant 消息 `tokens` | `path.cwd` |

日志有两种存放方式，扫描策略不同：追加写的 jsonl 按字节偏移增量读；整份重写的 JSON
（Cline 系、opencode）按 mtime 判断是否重读，靠事件时间戳去重，重读同一份文件不会重复计数。

> Cline 系与 opencode 的解析器按各自公开的存储格式实现，并用固定夹具做了测试，但未在真实安装上验证过；
> 如果字段有出入，改动只涉及 `collector.py` 里对应的 `parse_*_snapshot` 一个函数。

Cursor / Trae 等把会话存在 IndexedDB、不在本地留 token 明细的工具无法自动统计，可在界面中手动录入（录入时可填项目名）。

项目名只取工作目录的最后一段（`C:\code\tokenlens` → `tokenlens`）：足够区分项目，
又不会把完整路径这种隐私带进多端同步文件。

升级前采集的历史数据没有项目信息，会归入「未知项目」；在「项目」页点**重建采集数据**
可清空采集状态重扫全部日志，把项目维度补齐（原数据文件会先备份成 `.bak-<时间戳>`，手动录入的记录不受影响）。

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
