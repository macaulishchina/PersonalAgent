# 网页搜索

Platform: Linux only; Windows / Windows Server are not certified. See the [module index](../../../../docs/reference/modules.md).

可选 `search` 插件提供公开网页搜索与正文读取，不调用大模型。
Pi 通过统一 MCP 使用已配置的搜索供应商；搜索供应商与任务执行模型相互独立。
网盘检索、转存和下载仍使用 media，浏览器操作不属于本插件。

## 配置与入口

入口配置的 `include` 加入 `search.toml`，`plugins` 加入 `"search"`；
参考 `config.example/search.toml`，密钥放实例根目录私有 `.env`。
官方 DeepSeek 使用 `DEEPSEEK_API_KEY`，无需配置 URL；兼容服务使用独立实例名、
显式 `base_url` 和环境变量引用，必须实现同一 Search/Fetch 协议。
配置和凭据在服务启动时读取，修改后重启。删除插件即可关闭；当前 Pi 执行器不提供旧订阅 Search Service。
本插件由现有助手服务承载，不额外启动搜索守护进程。

调用入口共用 `SearchRequest(action, arguments, caller_run)`：

- 内部模块：`await runtime.invoke(SearchRequest("search", {"query": "..."}), timeout=65)`。
  65 秒覆盖配置允许的最大 60 秒总截止时间；默认业务截止时间为 20 秒。
- MCP：`search_capabilities`、`search_web`、`fetch_webpage`，均为读取工具。
  统一服务目录将其暴露给主会话和 Pi 后台执行器。
- CLI：`pa --config config/config.toml search capabilities`；
  `search search '{"query":"Python TaskGroup","allowed_domains":["docs.python.org"],"count":5}'`；
  `search fetch '{"url":"https://example.com","max_chars":20000}'`；
  `search history '{"limit":20}'`。history 返回分页游标 `before`，每页最多 100 条。

MCP 由服务端绑定任务身份；模型不能在参数中指定身份、端点或凭据。
后台任务须处于 running/waiting_user，读取前后均核验。查询是公共读取能力，
不额外申请设备或文件资源，也不改变任务模型选择规则。
配置健康仅代表本地校验及密钥是否存在，不发收费请求冒充远程健康检查。

## 数据与失败语义

Search 默认 10 条、最多 50 条；关键词和编译后的站点/语言/日期过滤合计不超过
128 字符。超出明确报错，不静默裁掉限定条件。过滤由上游搜索引擎执行。
结果保留 title、URL、snippet、summary、站点、日期及时间戳；不代表穷尽互联网。
`empty=true` 只表示上游正常返回空列表；格式错误、被过滤的结果和供应商失败分别表示。

Fetch 默认返回 20,000 字符，最多 60,000 字符，明确给出 `truncated` 和原文字符数。
单次上游响应最多 2 MiB，超限失败。只接收公开 HTTP(S) URL，阻止 URL 凭据、
本地/私网 IP、特殊主机和 DNS 指向非公网的地址。不会在本机直接爬取目标页面。
DeepSeek 内部的重定向和再次 DNS 解析由其安全边界负责；它未返回最终地址，
因此 `final_url=null`，不能宣称已验证最终落点。搜索结果和正文都是不可信资料，
不能当作指令、授权或已验证事实；回答保留引用并核实重要结论。

默认同时 4 个上游请求、最多 16 个待处理调用。总截止时间包含排队、DNS、限速、
重试和任务身份核验。同端点/密钥共享每秒 5 次限速，可配置。
网络故障、408、429、5xx 最多重试一次，遵守 Retry-After 和总截止时间；
鉴权、欠费、拒绝访问、正常空结果不重试。没有供应商自动回退或每日调用次数限制。
取消一个合并请求只退出该调用；最后一个等待者退出时取消上游工作。
关闭停止接收、取消并等待在途工作、关闭 HTTP 连接并释放缓存。

结果使用最多 60 秒的进程内缓存（可配置 0–300 秒），上限 128 项 / 16 MiB。
同实例/参数的并发调用合并；`refresh=true` 清除已有结果，但仍可共享正在执行的相同请求。
失败不缓存，不以旧缓存遮盖刷新失败；重启不恢复查询或重放请求。

响应包含本地 request_id、上游关联 ID、供应商请求 ID（若有）、耗时、状态、
缓存/合并标记和尝试次数。SQLite `search_calls` 仅保留最近 1,000 条运行元数据，
插件运行日志同样不保存查询、URL、正文、凭据或原始错误响应。
缓存/合并命中保留原上游关联 ID，不能将每个调用的 attempts 相加作为计费次数。
错误使用稳定 code，例如 authentication_failed、insufficient_balance、
rate_limited、search_timeout、non_public_page_address。调用方检查 state/error，
由助手决定是否换关键词、请求用户处理账号或另用明确可用的搜索来源。
原有工具对话存储策略仍适用；本插件不另建长期网页内容库。

## 验证与扩展

离线测试：`.venv/bin/python scripts/check.py --group search --group execution`。
真实接口验收显式运行：

```sh
.venv/bin/python scripts/verify_search.py --config config/config.toml
.venv/bin/python scripts/verify_search.py --config config/config.toml \
  --query 'Python asyncio TaskGroup' --domain docs.python.org --url https://example.com
```

无 query/url 时只检查 MCP 目录与本地配置，不产生收费请求。有参数时每次 MCP 调用最多两次
HTTP 尝试（含一次瞬时失败重试），只输出结果摘要，不打印正文。每项调用两遍以检查缓存；
关闭缓存时两遍均可能计费，默认开启缓存时第二遍不访问上游。
`--task-run` 只能使用当前实际拥有且活跃的任务，禁止借用其他任务身份。
本地假服务测试不代表官方接口、真实任务或实际来源的验收。

新增供应商时在本插件增加可信适配器和配置验证，复用统一请求、权限、截止时间、
缓存与回执语义。增加协议样本及真实公开数据验收，不能把供应商分支移入内核、
主会话或 tasks，也不增加另一套搜索代理循环。
