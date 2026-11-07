# 聊天数据库导出

在「数据与同步」页面的「导出聊天数据集」中，选择 SQLite 或 JSONL，点击「开始导出」，完成后点击「另存」。桌面版会弹出保存位置选择框；浏览器版使用浏览器下载。

默认导出当前账号的全部已导入聊天。可按名称搜索数据集，筛选单聊或群聊，并勾选多个会话；选择会跨分页和搜索保留，清空选择会清除全部已勾选会话。「选择已显示会话」仅选择当前已加载的列表；导出全部范围不受分页限制。已勾选模式按所选会话导出，不受当前列表类型筛选影响。

「预览数据集」显示实际匹配消息数、会话数、日期范围和最近 5 条原文。日期筛选独立于手动导入日期，按北京时间包含所选结束日期全天。预览和导出使用各自读取快照，同步期间数量可能增加。

导出以后台任务执行，显示统计、消息写入、索引建立和文件校验阶段。写入阶段每批更新已处理条数；可以取消任务，临时文件会清理。页面刷新后会恢复当前进行中的任务。大文件校验阶段取消会等当前文件操作结束。服务退出会中断未完成任务；已完成文件继续保留。

0.4.4 起，导出使用独立任务锁，不再被自动同步、首次导入或手动读取占用。导出读取开始时已提交的消息快照，同步可继续运行；导出开始后新增的消息可在下次全量或增量导出中获取。同一账号一次只能运行一个导出任务，重复点击会恢复已有任务的进度；导出期间请等任务完成或取消后再切换账号。

文件只含聊天数据，不包含模型密钥、发送授权、Agent 对话或应用设置。非文本消息保留占位，附件原文件尚未导出。各账号的导出文件分别保存在对应数据目录下的 `wecom-exports` 文件夹，页面显示最近 10 份；旧文件仍在该文件夹内。

## SQLite

生成的 `.sqlite3` 为独立数据库，无需企业微信、解密密钥或旁边的 WAL 文件。包含：

- `messages`：id、dedup_key、platform、conversation_id、conversation、conversation_type、sender_id、sender、timestamp、content、is_self、source_id。
- `metadata`：键值表，value 为 JSON 编码。包含账号、记录数、导出时间、筛选参数和 `next_cursor`。

`timestamp` 为 Unix 毫秒。`id` 是当前本地库编号；跨重建去重应使用元数据中的账号与消息的 `conversation_id`、`source_id` 组合。非原生导入来源可能有不同的编号语义，应自行对账。

服务器使用 Python 标准库即可读取：

```python
import json
import sqlite3

with sqlite3.connect('chats.sqlite3') as db:
    db.row_factory = sqlite3.Row
    metadata = {r['key']: json.loads(r['value'])
                for r in db.execute('SELECT * FROM metadata')}
    print(metadata['account'], metadata['count'], metadata['next_cursor'])
    for row in db.execute('SELECT * FROM messages ORDER BY id'):
        message = dict(row)
        # 在这里接入自己的入库或分析任务。
```

## JSONL

UTF-8，每行一个 JSON 对象。首行为 `type=metadata`，之后各行为 `type=message`。消息行含 account 及上述消息字段，正文换行保留为 JSON 转义，适合逐行处理。

## 增量

首次游标填 0。之后在相同账号、相同本地数据库及相同筛选范围下填入上次文件中的 `next_cursor`，导出比该编号大的已入库消息。较早时间的补导消息仍可进入增量。每次导出使用一致的读取快照，导出期间新入库的消息留到下一次。

日期、会话范围改变，或者数据库恢复、重建后，应从 0 重新导出并去重。该功能是新增消息导出，不是编辑、删除、撤回的完整同步日志。

本版本生成本地文件，不自动上传。后续服务器同步可由本机上传程序发送文件或读取本机增量接口，服务端确认接收后再保存上传游标。
