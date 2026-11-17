# 公告修订与附件原件的连续领取

程序围绕「一次可证明的发布状态」组织采集：把公告索引、正文和附件作为同一轮次（round）关联取证，版本身份由内容摘要决定，不依赖时间戳、文件名或来源自报的 revision 号。项目只使用 Python 标准库，不接触真实企业系统。

## 模块划分

- `source.py` 单次响应的读取、报文序列化与身份校验；`Response.response_id` 是状态码加实体摘要的完整响应身份，`sha256` 是实体摘要。
- `transport.py` 传输层。`HttpTransport` 实时抓取，`PacketTransport` 按请求顺序回放报文；URL 或来源身份不符、报文耗尽都会中断轮次，防止用不同发布时刻的响应拼凑一轮。
- `store.py` 工作目录与原件内容存储。每个原始响应按完整响应身份寻址，只存一份；所有 JSON 先写临时文件再原子替换。
- `versioning.py` 索引解析、撤回（tombstone）识别、跨资源一致性判定与版本身份计算。
- `round.py` 轮次执行器：开场索引 → 按索引顺序取正文与全部附件（每取一个响应即落盘）→ 收场再读同一索引；中断后从轮次文档恢复，不重复抓取已存响应。
- `registry.py` 跨轮次账本。`events.jsonl` 只追加，确认版本只增不覆盖；无法证明一致的条目进入待核视图，已确认版本永不被待核轮次改写。
- `pipeline.py` 流水线与命令行入口，串联「一轮采集 → 入册 → 交付视图」。
- `service.py` / `extract.py` / `ledger.py` 既有单响应流程，保持不变。

## 版本身份与一致性

- 版本身份 = 规范哈希（`entry_id` + 正文实体 sha256 + 附件 `(名称, 实体sha256)` 清单）。同名附件换内容、正文被替换都会产生新版本；仅改标题则身份不变，revision 号只作元数据留痕。
- 收场索引与开场索引的状态码或实体摘要不一致，即证明抓取途中来源更新，整轮标记 `index_drifted`，全部条目只进待核，不确认任何版本。
- 正文自报 revision、附件摘要与实际响应不符、非 200 状态等问题按条目记录；索引稳定时通过的条目照常确认，失败条目进待核。
- 撤回以索引 tombstone 表示：条目标记 `withdrawn`，历史版本完整保留。

## 工作目录布局

    <workdir>/
      responses/<response_id>.json   # 原始响应报文（状态、头、base64 实体）
      rounds/round-000001.json       # 轮次状态，open 时可恢复，sealed 后不可变
      ledger/events.jsonl            # 只追加的轮次入册事件
      ledger/views/versions.json     # 当前确认版本与版本历史
      ledger/views/pending.json      # 待核条目及具体原因
      ledger/views/deliverables.json # 检索人员交付视图（含原件指针与附件摘要）

交付视图中每条资料都带索引、正文、附件的原始响应路径与摘要；沿 `response_path` 可还原完整响应并独立复核 sha256。

## 运行

单响应（既有流程）：

    python -m app.service fixtures/sample.json

轮次采集（报文重放，报文按请求顺序排列）：

    python -m app.pipeline replay <工作目录> <索引URL> <来源ID> <报文.json...>

轮次采集（实时抓取）：

    python -m app.pipeline live <工作目录> <索引URL> <来源ID>

传输中断时轮次保持 `open`，再次运行同一命令即从最后一个已存响应恢复；已封存轮次不会被重复入册。

隔离模式下（`python -I`）解释器忽略 `PYTHONPATH`，测试请用仓库自带入口：

    python3 -I tests/run_tests.py

普通模式测试：

    python -m unittest discover -s tests -v

检查编译：

    python -m compileall -q app tests

来源与字段约定见 docs/source-contract.md。
