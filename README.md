# TokenLens

AI / Agent Token 消耗统计看板：自动扫描本机 AI 编程工具的日志，聚合 token 用量、估算费用，并跟踪订阅套餐配额。纯本地运行，数据不上传。

## 应用截图

以下截图由当前版本运行生成，使用隔离的演示数据。Token 用量、费用、模型速度和套餐配额均为展示示例，不代表真实账号数据或性能测量结果。

### 总览

集中查看 Token 消耗、估算费用、请求数、缓存命中率与用量趋势。

![TokenLens 总览：消耗统计、趋势图与模型和 Agent 分布](docs/screenshots/overview.jpg)

### 模型与速度

查看各模型的用量与费用，并分别展示生成速度、请求速度和 Codex 任务吞吐。

![TokenLens 模型页：模型用量、费用与速度统计](docs/screenshots/models.jpg)

### Plan 用量

查看 Kimi Code 套餐与月额度、Codex 窗口用量及火山方舟 Coding Plan 配额。

![TokenLens Plan 页：套餐、窗口用量与月额度](docs/screenshots/plans.jpg)

## 功能

- **自动采集**：增量扫描各工具的本地日志（只读，不改动原数据），聚合成 日 × Agent × 模型 × 项目 用量
- **项目维度**：按工作目录区分项目，看清哪个项目最烧钱；支持按项目筛选全站数据
- **环比对比**：统计卡显示与紧邻的等长上一周期的涨跌幅
- **费用估算**：内置常见模型参考价；可从 [models.dev](https://models.dev) 一键导入目录价，新模型出现时后台自动匹配补价，也可手动定价
- **配额跟踪**：Kimi For Coding 官方接口实时查询（含跨设备校准的绝对额度估算）、火山方舟 Coding Plan 官方 OpenAPI 查询（仅百分比，见下方「火山方舟 Coding Plan」）、Codex 本地限额记录、手动配额（固定额度或百分比锚定）；每个窗口可单独「停止 / 开始估计」——多端环境下本机观测口径未必干净时，可以只看官方百分比，或从当前时刻重新锚定
- **真实容量**：一个窗口从满额跑到用完，实际消耗掉多少 token —— 实测数出来的，不是按百分比外推的，见下方「真实容量」
- **账号识别**：读取各工具本地凭证里的账号唯一 id，多端汇总按账号对齐，换设备不再算错，见下方「多端与账号」
- **时间线明细**：任意一天按小时、任意小时按分钟查看用量（近 10 天直接查分钟桶，更早的日期回退为重扫日志）
- **模型速度**：模型页展示生成 / 请求速度、Codex 平均与最近任务吞吐（tok/s）、首 token 延迟；基于本机完成调用的日志计时，每 15 秒更新
- **外观**：可换背景壁纸与主题色（星紫 / 初音 / 樱），见下方「外观」
- **运行形态**：优先 pywebview 原生窗口，未安装则自动回退为浏览器打开

## 快速开始

**Windows 免安装**：从 [GitHub Releases](https://github.com/hanser2973/tokenlens/releases/latest) 下载 `TokenLens.exe` 双击运行。

**源码运行**（Python 3.8+，仅依赖标准库）：

```bash
python server.py          # 或双击 启动.bat
python server.py --demo   # 先生成 45 天演示数据再启动
```

两个可选依赖：`pywebview`（以原生窗口运行）与 `zstandard`（解压 DeepSeek Harness 的会话日志）。

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
| DeepSeek Harness | `~/.dsh/sessions/*/*/session.jsonl.zstd` | `assistant/message` 的 `data.usage` | 会话头（第一帧）的 `cwd` |
| Cline / Roo Code / Kilo Code | 各 VS Code 系编辑器 `globalStorage/<扩展>/tasks/*/ui_messages.json` | `api_req_started` | 不记录，归入「未知项目」 |
| opencode | `~/.local/share/opencode/storage/message/*/*.json` | assistant 消息 `tokens` | `path.cwd` |
| ZCode | `~/.zcode/cli/db/db.sqlite` | `model_usage` 表（一次模型调用一行，按请求 id 去重） | `session` 表的 `directory` |

日志有三种存放方式，扫描策略不同：追加写的 jsonl 按字节偏移增量读；整份重写的 JSON
（Cline 系、opencode）按 mtime 判断是否重读，靠事件时间戳去重，重读同一份文件不会重复计数；
dsh 的日志是**一批追加一个独立 zstd 帧**首尾拼接而成，帧边界天然对齐字节偏移，
所以同样按偏移增量读，只是读出来要先解帧（尾部半个帧不推进偏移，留到下一轮）。
ZCode 把账记在自带的 SQLite 里（`model_usage` 表，每行一次调用、带唯一请求 id），
以只读方式打开（不动应用的数据，WAL 下也能读），增量靠「已收过的请求 id」去重。

### DeepSeek Harness 需要一个解压库

zstd 解压要到 Python 3.14 才进标准库（`compression.zstd`）。在那之前需要
`pip install zstandard`（或 `pyzstd`）—— 三者按 `compression.zstd` → `zstandard` → `pyzstd`
的顺序探测，任一可用即可。没有解压库时**整源跳过**（不是读了当空数据），采集其他工具
照常，界面在扫描完成的提示里说明；之后装上库，先前的日志会被完整补采，不需要重建。

不想装库也有办法：在 dsh 的 `settings.yaml` 里把会话持久化配成 `compression: 'none'`，
它会改写成裸 `.jsonl`，TokenLens 也认（此后新建的会话生效，老的压缩日志仍需解压库）。

用量取的是 `assistant/message` 的 `data.usage`，而不是 `assistant/chunk` 里的 usage 块 ——
一次成功调用两处各记一遍，只收一处才不会翻倍。dsh 自己的口径是「同一 `(turn, step)`
用 message 覆盖先前的 chunk 采样」，与只认 message 等价；在真实会话上对过账，合计与
dsh 自己 `session_projcache.json` 里的 `tokenUsage` 完全一致。代价是某一步失败后重试时，
失败那次的 token 不计入（dsh 的合计同样不计）。

> Cline 系与 opencode 的解析器按各自公开的存储格式实现，并用固定夹具做了测试，但未在真实安装上验证过；
> 如果字段有出入，改动只涉及 `collector.py` 里对应的 `parse_*_snapshot` 一个函数。

Cursor / Trae 等把会话存在 IndexedDB、不在本地留 token 明细的工具无法自动统计，可在界面中手动录入（录入时可填项目名）。

项目名只取工作目录的最后一段（`C:\code\tokenlens` → `tokenlens`）：足够区分项目，
又不会把完整路径这种隐私带进多端同步文件。

升级前采集的历史数据没有项目信息，会归入「未知项目」；在「项目」页点**重建采集数据**
可清空采集状态重扫全部日志，把项目维度补齐（原数据文件会先备份成 `.bak-<时间戳>`，手动录入的记录不受影响）。

## Kimi Code 套餐与月额度

Plan 页使用本机 Kimi CLI 登录凭证查询官方 `/coding/v1/usages` 和 `/coding/v1/me`，
显示接口返回的套餐名（例如 Pro）、5 小时 / 周窗口（如接口提供）、**月总额度**与**月 Code 额度**，
以及各自的重置时间。兼容旧版 `limits` / `usage` 与新版 `usages` 响应；结果缓存 120 秒。

月总额度包含会员的其他 Kimi 产品消耗，月 Code 额度单独展示，两项不能相加。
月额度仅展示官方已用百分比，不按本机日志估算绝对 token 数；接口缺字段时明确显示未返回，
不会把未知额度显示成 0%。套餐信息查询失败时，已取得的配额仍会正常展示。
参考：[官方配额解析源码](https://github.com/MoonshotAI/kimi-code/blob/main/packages/oauth/src/managed-usage.ts)、
[Kimi Code 权益说明](https://www.kimi.com/help/kimi-code/benefits)。

## 模型速度（tok/s）

打开「模型」页即可查看，并遵循顶部的日期、Agent、模型、项目筛选：

- **生成速度**：输出 token 数 ÷ 流式生成耗时；Kimi 使用 `step.end` 中的 `llmStreamDurationMs`，不含首 token 等待与工具执行，输出含模型思考 token。
- **请求速度**：输出 token 数 ÷ 请求耗时；Kimi 使用首 token 延迟加流式耗时，旧日志回退到同一次 `llm.request` → `usage.record` 的时间差。
- **平均速度**：总输出 token ÷ 总对应耗时，避免把长短请求的 tok/s 简单平均。
- **首 token 延迟**：Kimi 日志中的 `llmFirstTokenLatencyMs`；没有计时字段时显示 `—`。
- **Codex 任务吞吐**：新版日志的 `token_usage_record.turn_token_usage.output_tokens` ÷ `task_complete.duration_ms`，显示平均与最近任务吞吐。整轮耗时包含思考、工具执行与等待，和单次模型生成 / 请求速度分列展示；首 token 延迟使用任务级 `time_to_first_token_ms`。累计输出已含思考 token，不重复加 `reasoning_output_tokens`，也不累加重复的 `token_count` 快照。

Codex 仅统计成功完成的任务；失败、取消和未完成的任务不结算。没有 `duration_ms` 时可用任务明确的开始 / 结束时间，
没有任务级 token 记录的旧日志不计算。跨扫描保留未完成任务状态，重复扫描按任务 id 去重；
同一轮切换多个模型或项目时分别标为「多模型任务」「多项目任务」，避免把整轮耗时重复归给每个模型。

ZCode 使用 `model_usage.started_at` / `completed_at`，opencode 使用消息的 `time.created` / `time.completed`，
这两类仅显示请求速度，耗时可能包含工具执行。Codex 的纯生成速度与单次请求速度显示 `—`；Claude 等缺少可靠计时的日志暂不计算。
取消、失败、零输出、无效耗时不作为样本；不足 50ms 的流式窗口不推算生成速度。

速度采集独立于 token 账本，首次打开自动回填已有日志，无需重建；只读已完成记录，
支持半行与跨扫描续接，重复刷新不会重复计数。样本仅存本机 `data/speed_samples.json`，
不参与多端同步，保留近 30 天最多 20,000 条。页面可手动刷新并每 15 秒自动更新。

## 火山方舟 Coding Plan

Plan 页自动展示火山方舟 Coding Plan 的用量（与控制台的「当前会话 / 近 1 周 / 近 1 月」一致），
数据来自火山引擎控制面 OpenAPI `GetCodingPlanUsage`，签名 V4 走
`open.volcengineapi.com` —— 和推理用的 `ARK_API_KEY` 是两套凭证，必须填账号的
**AccessKey ID / Secret Access Key**（控制台「访问控制 → 密钥管理」创建）。

配置入口：Plan 页右上「⚡ 方舟配额」。凭证只存本机 `data/settings.json`，不进多端同步；
也认环境变量 `VOLCENGINE_ACCESS_KEY_ID` / `VOLCENGINE_SECRET_ACCESS_KEY`（界面未配置时兜底）。

注意与 Kimi 卡片的差异：

- 接口**只返回各窗口的已用百分比与重置时间**，不返回绝对 token 额度；
- 本地日志里的用量记在 Claude Code / dsh 等具体工具名下，无法归因到方舟账号。

所以这张卡片不做绝对额度估算，也不参与真实容量观测——官方百分比与重置时间本身就是
账号级准确数据，这是「接口给不出」而不是「估不准」。

## 真实容量

官方接口只给百分比，不给绝对额度。原来的「估算总额」是外推出来的 —— 实测用量 ÷ 官方
百分比。用得少的时候分母很小，一点观测误差就被放大成几倍的额度误差；官方百分比与
token 也未必严格线性（不同模型权重不同）。

**真实容量不外推，只数数**：盯住一个窗口，从它满额（已用 0%）一路看到用完（已用 100%），
这一整轮实测到的 token 总量，就是这个窗口真实装得下多少。

一轮要被记成真实容量，四个条件缺一不可：

1. **从满额开始**：本轮第一次看到这个窗口时已用 ≤ 5%。半路才开始看的那一轮不算 ——
   前面消耗了多少无从得知
2. **口径覆盖整个账号**：单机使用，或多端已合并且各对端的分钟桶覆盖到窗口起点
3. **真的跑到用完**：官方已用百分比 ≥ 99%
4. **全程同一个账号**：中途重新登录成另一个账号，这一轮作废

任何一条中途变脏，这一轮就整轮丢弃，等下一次窗口重置重来。所以 5 小时窗口当天就能出数，
周窗口要等一个完整的、真的把额度用光的星期。没跑满时界面显示本轮已记多少、进度多少；
不满足条件时直接说明是哪一条没满足。

后台采集线程每 10 分钟顺带观测一次配额（不只在你打开配额页时才看），否则窗口在半夜
重置、在没开界面时跑满就接不上了。历史样本存 `data/capacity.json`，每个窗口留最近
12 轮，界面显示最新一轮，多轮时一并给出均值与区间。

> 「停止估计」开关只关外推，不影响真实容量 —— 那个开关往往正是因为外推值不可信才被
> 关掉的，而真实容量恰好是外推的替代品。

## 多端与账号

设备 id 是每台机器随机生成的，换台电脑就换一个，认不出「这两台是同一个账号」。
官方配额百分比却是**账号级**的：把另一个账号的用量也加进分子，估出来的额度会凭空缩水。

所以 TokenLens 会读各工具本地凭证里的账号唯一 id：

| 工具 | 来源 | 字段 |
| --- | --- | --- |
| Claude Code | `~/.claude.json` | `oauthAccount.accountUuid`（显示名用登录邮箱）；未登录 OAuth 时退回本地 `userID` |
| Codex CLI | `~/.codex/auth.json` | `tokens.account_id`；API Key 模式退回密钥哈希（密钥原文不落盘） |
| Kimi Code | 官方配额接口响应 | `user.userId`（缓存进 `data/accounts.json`，离线时也能用） |

Cline / opencode 等纯本地日志的工具没有账号概念，不参与匹配。DeepSeek Harness 也不参与：
`~/.dsh` 里能拿到的只有一个每台机器随机生成的匿名 id，跨设备对不上，认它反而会把
同一个账号判成两个。

> 「多端与账号」只影响配额窗口与真实容量。dsh 的用量统计不受影响，照常合并所有设备。

写进共享目录的只有 `sha256(账号 id)` 的前 16 位：同一账号在任何设备上算出的哈希一致，
足够做匹配，但反推不回邮箱和 uuid，放进网盘也不会把身份带出去。邮箱这类可读信息只留在
本机，供多端设置面板显示。

匹配规则是「不知道就放行，确认不一样才排除」：

- 账号对得上 → 计入配额窗口的实测用量
- 对端没报账号（老版本 TokenLens，或那台机器没装这个工具）→ 仍计入，界面标注「未报账号」
- 报了但对不上 → 从配额窗口的实测里剔除，界面标注「其他账号」并说明已排除

总览、项目、模型这些统计仍然合并所有设备的数据（同步目录本来就是你自己配的），
只有配额窗口和真实容量按账号收紧 —— 那两处混入别的账号会直接算错。

## 外观

左下角「外观」可以设置背景壁纸和主题色。

选中的图片在浏览器里先缩到 1920px、压成 JPEG（受服务端 1 MB 请求体上限约束），再存成
`data/wallpaper.jpg`——不上传任何地方，跟着 `data/` 一起被 `.gitignore` 忽略。

三个滑块：**压暗**和**模糊**只作用于背景层；**通透度**调的是卡片与侧栏的透明度。
总览页的面板几乎铺满整屏，不给一档可调的通透度，壁纸只能从卡片缝里露出来。
挂上壁纸后各层面板会自动从「提亮的白」切成「压深的磨砂」，次要文字同时提一档亮度
并加描边阴影，通透度拉高也不会糊。

壁纸、压暗、模糊、通透度、主题色都属于本机显示偏好，不进多端同步数据。

> 用别人的作品当壁纸请注意授权：自己的照片、CC 授权素材（如 Wikimedia Commons）、
> 或明确允许非商用个人使用的素材都没问题，转发或公开展示时按其授权要求署名。

## 隐私与安全

- 服务只监听 `127.0.0.1`，并校验 `Host` / `Origin`，可挡 DNS rebinding 与跨站请求
- `data/` 目录包含你的真实使用数据（含本机文件路径），**不要提交到公开仓库**——本仓库 `.gitignore` 已默认忽略
- 外网请求仅用于：models.dev 价格目录、Kimi 官方配额 / 套餐接口（使用本机 Kimi CLI 的 OAuth 凭证，
  为了给真实容量连续观测，后台每 10 分钟查一次）、火山引擎 OpenAPI（你配置的 AK/SK，
  仅查询 Coding Plan 配额百分比）
- 账号识别只读取凭证文件里的账号标识字段（accountUuid / account_id / userId），不解析也不外传
  access_token；进共享目录的只有不可反推的哈希。方舟的 AK/SK 只写本机 `data/settings.json`，
  任何接口响应都不会带出 SK 原文

## 开发

```bash
python -m unittest discover -s tests   # 运行测试
```

打包 exe 使用 PyInstaller（静态资源打进 exe，数据文件落在 exe 同目录）。

## License

[MIT](LICENSE)
