# 来源契约

来源提供公告索引 JSON，entry_id 在同一来源内稳定；每项含 revision、body_url、attachments[{name,url}]。详情响应可含 revision 和附件摘要，也可能缺失。源站不保证多请求事务；撤回以索引 tombstone 表示。来源许可只覆盖配置的主机和路径。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 entry_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
