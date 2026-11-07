# 桌面启动器（faswecom.exe）说明

启动器是 C# / WinForms + WebView2，源码在 `desktop/FasWeCom.cs`，与 Python 服务相互独立：

- 创建窗口、托盘图标、加载网页；
- 启动 `runtime\python.exe -X utf8 -B desktop/serve_wecom.py <ready.json>` 并等待就绪；
- 页面地址固定为 `/chat?desktop=1`（启动即聊天页）。

## 本仓库相对上游的改动

1. 启动页由 `/console` 改为 `/chat`；
2. 托盘图标支持**单击**恢复窗口（保留右键菜单与双击）；
3. 窗口位置在构造时显式居中（`PlaceWindow()`），不再依赖 `StartPosition`，避免不同环境下窗口落到屏幕外；
4. 网页控件使用 `DockStyle.Fill`，始终贴合客户区。

## 重新编译

需要 Windows 自带的 .NET Framework 4.x 编译器（无需安装 VS）：

```powershell
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
& $csc /nologo /target:winexe /platform:x64 /optimize+ `
  /win32icon:wecom.ico `
  /r:System.dll /r:System.Core.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll `
  /define:WECOM /r:System.Web.Extensions.dll `
  /r:..\_internal\Microsoft.Web.WebView2.Core.dll `
  /r:..\_internal\Microsoft.Web.WebView2.WinForms.dll `
  /resource:..\web\wecom-logo.jpg,faswecom.Logo.png `
  /win32manifest:desktop\FasWeCom.manifest `
  /out:faswecom.exe desktop\FasWeCom.cs
```

图标可用 Pillow 从 `web/wecom-logo.jpg` 生成：

```python
from PIL import Image
Image.open('web/wecom-logo.jpg').convert('RGBA').save(
    'wecom.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
```

编译产物可直接替换安装目录里的 `faswecom.exe`（先备份旧文件）。
