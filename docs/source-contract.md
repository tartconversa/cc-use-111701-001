# 来源契约

来源提供公告索引 JSON，`entry_id` 在同一来源内稳定；每项含 `revision`、`body_url`、`attachments[{name,url}]`。详情响应可含 `revision` 和附件摘要（`attachments[{name,sha256}]`），也可能缺失；非 JSON 正文只做字节级身份，不做字段核对。源站不保证多请求事务；撤回以索引 tombstone 表示（字段 `tombstone: true`，或 `status` 取值 `tombstone/withdrawn/deleted`）。来源许可只覆盖配置的主机和路径，相对链接按索引 URL 解析。

## 单响应约定

单响应输入为 `fixtures/sample.json`，含 `source_id`、`url`、`status`、`headers` 和 `body_base64`。实体摘要由 `source.Response.sha256` 给出，完整响应身份由 `source.Response.response_id`（状态码 + 实体摘要）给出——同字节实体在不同状态码下是不同响应，原件必须分别留痕。`extract.extract` 只读取 records 列表中的 `entry_id`，不能把空列表解释为完整采集；`ledger.receipt` 关联单次响应摘要与提取条目。

## 轮次约定（跨请求的发布状态证明）

一次轮次按固定顺序请求：开场索引 → 逐条正文与附件（严格按索引中的附件顺序）→ 收场索引。轮次资格规则：

1. 开场索引无法解析或非 200，整轮待核，不确认任何条目。
2. 收场索引与开场索引的状态码或实体 sha256 不一致，判定为抓取途中来源更新（`index_drifted`），整轮待核；该轮取回的正文与附件不得与任一已确认版本拼合。
3. 索引稳定时逐条核对：正文/附件 HTTP 状态、正文自报 revision 与索引 revision、正文附件摘要与实际附件实体 sha256。通过的条目按内容身份确认或重验，未通过的条目进待核，其已确认版本保持不变。
4. 传输异常（连接中断、重放报文不足或身份不符）不封存轮次：轮次保持 `open`，已取响应均已内容寻址落盘；恢复时以同一份开场索引原件为准继续，缺哪个响应补哪个，不重新抓取已存响应。

## 版本身份

版本身份是对 `{entry_id, body_sha256, attachments:[{name, sha256}]}` 的规范 JSON 做 sha256。时间戳、URL、文件名与 revision 号都不参与身份计算：同名附件换内容、正文字节变化必然产生新版本；仅标题变化不产生新版本。`manifest_digest` 再把版本身份与所属索引快照摘要绑定，作为发布状态证据链指针。

## 账本与交付

`ledger/events.jsonl` 只追加，轮次序号即版本顺序（不使用时间戳）。确认版本进入 `views/versions.json` 并永久保留历史；待核条目进入 `views/pending.json`，后续轮次通过后解除。`views/deliverables.json` 面向检索人员，每条确认资料带索引、正文、附件的原始响应路径、状态码与摘要，附件摘要即实际响应的实体 sha256，可逐项回溯原件。

来源配置与真实凭据由运行时提供，样例与测试均为合成数据。
