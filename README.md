# faswecom · 企业微信适配版（WeCom Adapter）

企业微信本地数据适配器 + 本机发送网关，带 Web 操作台与 MCP 接口。
可以只读地读取本机企业微信的聊天数据，通过操作台浏览、检索、导出，也可以按授权向指定会话发送文本/文件。

> 本仓库基于 Tulpa WeCom edition（MIT 许可）修改而来，保留原版权声明。
> **发布前请确认你拥有该适配器源代码的发布权**；如果你不是作者，建议先建**私有仓库**并与作者/公司确认。

## 功能概览

- **本地读取**：只读访问本机企业微信数据库（不修改客户端、不注入进程）
- **操作台**：聊天浏览、会话检索、导入/导出、发送权限设置、模型设置
- **MCP 服务**：供 Codex / 其他 Agent 按连接授权查询，可选授权直接发送
- **发送方式**（三选一，配置在操作台）：
  - 原生接口（native）：需要向客户端注入代码，风险极高，已弃用，不推荐
  - 后台窗口（background_window）：PostMessage，无需前台，但不能切换会话
  - 前台桌面（desktop_ocr）：OCR + 模拟输入，需要企微窗口可见、未最小化

## 目录结构

~~~
.
├─ app_wecom.py              主应用（页面路由、数据接口、MCP 安装）
├─ app_wecom_adapter.py      适配器网关（OneBot 风格本地 HTTP）
├─ desktop/serve_wecom.py    桌面服务入口（控制台 + 适配器双监听）
├─ chatlocal/                后端核心模块（读取、存储、发送、MCP、控制台…）
├─ scripts/                  辅助脚本（发送 worker 等）
├─ web/                      Web 操作台前端（HTML/CSS/JS + Logo）
├─ doc/                      设计文档（适配器、聊天、控制台、导出、本地化、原生兼容）
├─ requirements-wecom.txt        基础依赖
├─ requirements-wecom-adapter.txt 发送相关依赖（OCR / Windows）
├─ LICENSE                   MIT
└─ 开始使用.txt               安装包内的快速说明
~~~

## 环境要求

- Windows 10 / 11 x64
- Python 3.12
- 企业微信 PC 版（已验证 5.0.11.6029 / 5.0.12.6018 / 5.0.13.6005 x86）
- 依赖：

~~~powershell
pip install -r requirements-wecom.txt
pip install -r requirements-wecom-adapter.txt
~~~

## 从源码运行

桌面服务需要一个会话令牌与就绪文件，和打包版启动方式一致：

~~~powershell
$env:CHATWEAVE_SESSION_TOKEN = ( -join ((48..57)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_}) )
python desktop\serve_wecom.py dev.ready
~~~

启动后控制台地址会写入 data/wecom-runtime.json 的 console_url，浏览器打开即可。

> 首次运行需要识别企业微信数据目录并解锁读取器（只读），具体见 doc/WECOM_LOCAL.md。

## 数据与隐私

- data/ 目录（聊天数据库、令牌、DPAPI 密钥、模型配置、运行状态）不在仓库中，.gitignore 已排除
- 仓库内没有任何聊天记录、手机号、令牌或密钥；如需提交问题截图，请自行打码
- 所有数据只保存在本机，默认仅监听 127.0.0.1

## 风控与合规提醒

- 不要使用 frida / 原生注入发送：企业微信会判定"涉嫌使用外挂、非官方客户端或模拟器"并强制下线（有真实案例），且可能封号
- 窗口模式发送会短暂占用前台，长文本输入耗时几十秒；发送期间不要切换/缩放企微窗口
- 批量发送有频率上限（操作台可配），请勿高频群发
- 本工具仅用于合规的客服辅助场景，请遵守企业微信使用条款

## 本次改动（2026-11）

- 操作台 UI 重构：四个页面（聊天 / 助手 / 导入导出 / 设置）统一左侧菜单，固定品牌区与账号区
- 新增「设置」页：Agent / MCP 连接管理（从导入导出页拆出）
- 新增「智能客服接管」总开关（适配器配置 ai_takeover，供外部自动回复系统读取）
- 聊天页：记住上次会话、可拖拽分栏、移除旧的"当前会话自动回复"条
- 助手页：搜索范围改为右侧抽屉（点外部/箭头收起）、最近对话可拖拽调宽
- 修复若干界面细节，保留全部原有发送/读取业务逻辑

## 文档

- 适配器接口：doc/WECOM_ADAPTER.md
- 聊天与发送：doc/WECOM_CHAT.md
- 操作台：doc/WECOM_CONSOLE.md
- 导入导出：doc/WECOM_EXPORT.md
- 本地读取：doc/WECOM_LOCAL.md
- 原生兼容：doc/WECOM_NATIVE_COMPAT.md
- 第三方许可：doc/THIRD_PARTY.md

## 许可

MIT License，详见 LICENSE。
原版权：Copyright (c) 2026 Tulpa contributors；faswecom 修改：Copyright (c) 2026 darkfashi。
