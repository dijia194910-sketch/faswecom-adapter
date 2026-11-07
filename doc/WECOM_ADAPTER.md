# 企业微信本地适配器 0.3.7

独立 HTTP 适配层，采用 OneBot 风格的动作名称和响应封装，默认监听 `127.0.0.1:19091`。
它是首版动作子集，**不是完整 OneBot v11/v12 实现，也不是 NapCat 的企微版本**。
QQ 原连接和企微阅读页面（7863）均保持独立。适配器不调用大模型、不启动自动回复。

## 当前能力

| 能力 | 状态 |
|---|---|
| Token 鉴权、本机 HTTP、动作/params/echo | 已实现 |
| 已导入会话目录、按入库游标取增量 | 已实现；不是原生实时推送 |
| 单聊/群聊纯文本发送接口 | 已实现；单聊支持原生调用，窗口方式支持单聊/群聊 |
| 去重、发送回执、重启后的不确定结果保护 | 已实现 |
| 窗口处于后台时发送 | `background_window` 已完成真实单聊测试，不激活前台、不移动鼠标、不使用全局键盘输入 |
| 原生发送的窗口要求 | 不使用窗口输入和OCR；最小化/锁屏场景尚未真实验证 |
| 原生登录身份、服务端消息编号 | 原生模式核对运行中账号及服务器消息回执；不代表对方已读或送达 |
| WebSocket、反向 WebSocket、图片/文件、群管理 | 未实现 |

界面发送目前要求 Windows 桌面可操作，企微已登录且目标在左侧可见会话列表中。
它会把企微切到前台并打开目标，先检查唯一名称和聊天标题，再核对输入框和发送文字。
没有识别到、存在重名、输入框有草稿或被遮挡时停止。仅首版测试过的布局可用；客户端升级可能需要适配。
名称核对不能等同稳定账号ID验证，同名联系人不能使用此发送器。请在独立、明确的单账号环境使用。

后台方式 `background_window` 使用 PrintWindow 读取企微窗口、PostMessage 向该窗口传递鼠标及字符消息。
它仍会在企微内部选择会话，但不激活企微、不占用系统键鼠。企微必须已经处于后台、未最小化，目标须在可见会话列表。
操作期间前台窗口或企微位置发生变化即中止；不自动降级为前台操作。截图、标题或草稿核对失败同样停止。
若当前聊天标题已匹配目标，直接核对草稿，不重复选择会话。搜索联系人不是后台适配器的能力：实测搜索结果弹窗可能主动激活企微，需要先完成会话定位，再把企微置于后台。

## 启动与配置

```powershell
.\start-wecom-adapter.ps1
```

第一次会初始化 `data/wecom-adapter.json`，Token 只保存在该本地文件，不打印到日志。
**默认发送关闭、目标列表为空**。用户可编辑本地文件以启用指定会话；API/模型没有修改授权的接口。

```json
{
  "token": "保留自动生成的随机Token",
  "send_enabled": true,
  "backend": "desktop_ocr",
  "targets": {
    "test-contact": {"name": "测试联系人", "kind": "private", "enabled": true},
    "test-group": {"name": "测试群", "kind": "group", "enabled": false}
  }
}
```

这里的 `test-contact` 是用户给接收目标配置的适配器ID，**不是企微原生账号ID，也不是QQ号**。
`name` 必须精确对应企微显示名。目录接口只列本地配置的目标，不宣称返回完整通讯录。
保存配置后，下一次请求重新读取。`send_enabled: false` 停止后续发送；Token 更换即撤销旧凭据。
执行中的已点击发送无法撤回，停用后请核对在途结果。

`backend: "native"` 支持 5.0.11.6029 x86 的已有单聊，校验进程文件哈希、函数签名、登录账号、单聊双方账号和确切正文后调用原生接口。版本不匹配会停止，不会自动切换窗口方式。新对话默认原生方式，旧配置与待发送对话保留原选择。
后台窗口模式改为 `backend: "background_window"`；`get_status` 会返回 `background_send: true`、`headless_send: false`。
停止服务：`./stop-wecom-adapter.ps1`。

## 调用方式

所有请求均使用 `Authorization: Bearer <token>` 和 `Content-Type: application/json`。
支持 `POST /动作名称`（body就是参数）以及 `POST /` 的动作封装。
拒绝来自浏览器网页的 Origin 请求，供本机程序/MCP桥接层使用，不暴露公网。

```python
import json
from pathlib import Path
import httpx

# 从本源码目录运行；不要把本地配置文件或Token提交到仓库。
cfg = json.loads(Path('data/wecom-adapter.json').read_text('utf-8'))
with httpx.Client(base_url='http://127.0.0.1:19091',
                  headers={'Authorization': 'Bearer '+cfg['token']},
                  timeout=100, trust_env=False) as client:
    print(client.post('/get_status', json={}).json())
    print(client.post('/', json={
        'action': 'get_friend_list', 'params': {}, 'echo': 'example'
    }).json())
```

已明确授权发送后，发送请求示例：

```json
{
  "action": "send_private_msg",
  "params": {
    "user_id": "test-contact",
    "message": [{"type": "text", "data": {"text": "hi"}}],
    "idempotency_key": "test-contact-hi-001"
  },
  "echo": "send-001"
}
```

群聊改用 `send_group_msg` 和 `group_id`。消息也可直接传字符串；目前限单行1–2000字。
同一条发送的重试必须沿用相同 `idempotency_key`，编号绑定目标、类型和正文；不允许换正文重用编号。
发送间隔至少5秒，同时只处理一个发送请求。文字核对失败后可能留下草稿，用户检查后再处理。

## 动作与结果

| 动作 | 参数 / 用途 |
|---|---|
| `get_version_info` | 版本及协议能力说明 |
| `get_status` | 发送开关、后台发送能力、历史库绑定来源；不代表实时登录验证 |
| `get_friend_list` / `get_group_list` | 用户配置的接收目标白名单 |
| `get_conversations` | 已导入会话，最多500项 |
| `get_updates` | `cursor`、`limit`（1–200）；首次不传cursor只返回当前游标，避免把历史当新消息 |
| `send_private_msg` | `user_id`、`message`、`idempotency_key` |
| `send_group_msg` | `group_id`、`message`、`idempotency_key` |
| `get_operation` | `operation_id`，读取已保存发送结果 |

`get_updates` 读取 faswecom 已导入数据库。主服务开启自动同步后，新消息会自动入库；暂停或关闭主服务时不再自动更新，也可手动导入。补导入历史也会形成入库增量。
本接口还不能直接作为无限自动回复的实时触发器。调用方应检查消息时间、本人标记和业务规则。

响应包含 `status`、`retcode`、`data`、`echo`。失败另含 `message`。
发送结果在 `data.state`：

- `VISIBLE_OUTGOING`：客户端出现新增的本人发送气泡。`status=ok, retcode=0`，不代表对方已读。
- `SERVER_RECORDED`：原生消息编号和服务器回执已匹配，`status=ok, retcode=0`；不代表对方已读。
- `NOT_SENT`：未执行发送，`retcode=1502`；窗口模式可能保留待检查的草稿。
- `UNKNOWN`：无法可靠判断，`retcode=1503`；**不可换编号自动重发**。
- `SENDING`：同一编号正在处理，`status=async, retcode=1`。

`operation_id` 是适配器本地操作编号；原生成功回执包含 `native_message_id`、`server_message_id` 和 `sequence`，窗口模式的原生消息编号为 null。
服务在外部操作前落盘状态；进程中断恢复成UNKNOWN，保留幂等记录，不自动重发。
数据库、回执、Token和截图均保存在忽略目录中，不包含在源码包里。

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/check_wecom_adapter.py
```

使用合成数据覆盖鉴权、Origin/Host、Token撤销、发送权限、重名、幂等冲突、未知结果恢复、增量分页、错误标题/草稿拒绝和旧气泡误判。
2026-10-08 在企微 Windows 5.0.0.8002 x86 完成经用户授权的真实单聊测试：前台 HTTP 适配器发送成功；后台窗口发送器单独测试也出现新增本人消息气泡，前台窗口保持不变。
后台窗口测试验证英文单聊短文本，随后通过 HTTP 后台适配器验证一条中文单聊测试信息；正文发送阶段前台窗口保持不变。
群聊、长文本、最小化和锁屏场景未做真实发送验证。搜索新联系人时弹窗曾激活企微，故不宣称从搜索到发送整个流程均不抢占前台。

## 0.3.6 原生回执

原生模式通过 Frida 加载随包版本限定脚本，仅提交已确认的单条文本。提交前持久化 UNKNOWN 防止崩溃重发。返回消息编号后，对只读源数据库快照匹配本人、会话、全文、消息编号以及非零 server_id 和 sequence，成功状态为 SERVER_RECORDED。回执超时或连接中断仍为 UNKNOWN，不会自动重试。已有真实验证来自此前单条发送实验，本次正式接入使用合成回归；没有额外发送真实消息。

## 0.3.7 检查复用

原生辅助进程在适配器存活期间复用；只缓存组件和当前进程/文件版本校验，发送许可仍逐次检查。验证当前接收人时按稳定账号索引查询，不重复汇总所有联系人。每次发送仍创建并卸载独立的原生请求脚本，重新核对实际登录账号和函数签名。辅助进程异常或超时仍返回 UNKNOWN，不重试已提交消息。
