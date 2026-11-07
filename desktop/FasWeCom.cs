using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: System.Reflection.AssemblyCopyright("Copyright © 2026 darkfashi")]

static class Program {
    internal static string Root = AppDomain.CurrentDomain.BaseDirectory;
    internal static string Connect = "";
    internal static bool Background = false;
    internal static uint ActivateMessage;
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern uint RegisterWindowMessage(string name);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr hwnd,uint msg,IntPtr w,IntPtr l);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        SetProcessDPIAware();
        try {
            if(File.Exists(Path.Combine(Root,".tmp","upgrade.lock")))
                throw new Exception("程序正在升级，或上次升级意外中断。请先检查 reports/private/upgrades 中的备份记录和 doc/UPGRADE.md，再启动。");
            if(args.Length==1 && args[0]=="--background")Background=true;
            else if(args.Length!=0) {
                if(args.Length!=2 || args[0]!="--connect") throw new Exception("无法识别启动参数。");
                Uri uri;
                if(!Uri.TryCreate(args[1],UriKind.Absolute,out uri) || uri.Scheme!="http" || uri.Host!="127.0.0.1" || uri.AbsolutePath!="/" || uri.Query!="" || uri.UserInfo!="" || uri.Fragment!="")
                    throw new Exception("审阅连接仅允许本机 http://127.0.0.1:端口。");
                Connect=uri.GetLeftPart(UriPartial.Authority);
            }
            string id;
            using(var sha=SHA256.Create())id=BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(Root.ToLowerInvariant()+Connect))).Replace("-","").Substring(0,24);
            ActivateMessage=RegisterWindowMessage("ChatWeave.Activate."+id);
            bool created;
            using(var mutex=new Mutex(true,"Local\\ChatWeave."+id,out created)) {
                if(!created){PostMessage(new IntPtr(0xffff),ActivateMessage,IntPtr.Zero,IntPtr.Zero);return;}
                Application.Run(new DesktopWindow());
            }
        } catch(Exception e) {MessageBox.Show("faswecom 未能启动。\n\n"+e.Message+"\n\n请完整解压文件夹，再运行 faswecom.exe。","faswecom",MessageBoxButtons.OK,MessageBoxIcon.Information);}
    }
}

sealed class DesktopWindow:Form {
    readonly WebView2 view=new WebView2();
    readonly PictureBox logo=new PictureBox();
    readonly Label title=new Label(),hint=new Label();
    readonly System.Windows.Forms.Timer health=new System.Windows.Forms.Timer();
    readonly JavaScriptSerializer json=new JavaScriptSerializer();
    readonly NotifyIcon tray=new NotifyIcon();
    Process backend;
    readonly object stageLock=new object();
    int exitLogged;
    Job owner;
    string service="",token="",ready="";
    bool closing=false,loaded=false,exitRequested=false,closePending=false,trayNotice=false,cleaned=false;
    public DesktopWindow() {
        Text="faswecom";StartPosition=FormStartPosition.CenterScreen;
        using(var graphics=CreateGraphics()) {
            double scale=graphics.DpiX/96.0;var screen=Screen.FromControl(this).WorkingArea;
            ClientSize=new Size((int)Math.Min(1320*scale,screen.Width*.90),(int)Math.Min(860*scale,screen.Height*.88));
        }
        MinimumSize=new Size(900,650);
        PlaceWindow();
        Shown+=delegate{PlaceWindow();};
        BackColor=Color.FromArgb(250,249,245);Font=new Font("Segoe UI",10);
        Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        tray.Icon=Icon;tray.Text="faswecom · 本机资料服务";
        var menu=new ContextMenuStrip();
        menu.Items.Add("打开 faswecom",null,delegate{ShowWindow();});
        if(Program.Connect=="") {
            var startup=new ToolStripMenuItem("开机启动（当前文件夹）");startup.CheckOnClick=true;
            using(var key=Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
                startup.Checked=key!=null && (string)key.GetValue("faswecom","")==StartupCommand();
            startup.Click+=delegate {
                try {using(var key=Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run")) {
                    if(startup.Checked)key.SetValue("faswecom",StartupCommand());
                    else if((string)key.GetValue("faswecom","")==StartupCommand())key.DeleteValue("faswecom",false);
                }}catch {startup.Checked=!startup.Checked;MessageBox.Show(this,"无法保存开机启动设置。","faswecom");}
            };
            menu.Items.Add(startup);
        }
        menu.Items.Add("彻底退出",null,delegate{exitRequested=true;Close();});
        tray.ContextMenuStrip=menu;tray.DoubleClick+=delegate{ShowWindow();};tray.MouseClick+=delegate(object sender,MouseEventArgs e){if(e.Button==MouseButtons.Left){if(Visible&&ShowInTaskbar)HideToTray();else ShowWindow();}};tray.Visible=true;
        using(var stream=typeof(DesktopWindow).Assembly.GetManifestResourceStream("faswecom.Logo.png"))
        using(var image=Image.FromStream(stream))logo.Image=new Bitmap(image);
        logo.Size=new Size(96,96);logo.SizeMode=PictureBoxSizeMode.Zoom;logo.BackColor=Color.Black;
        title.Text="faswecom";title.Font=new Font("Segoe UI",30,FontStyle.Bold);title.ForeColor=Color.FromArgb(23,23,20);title.AutoSize=true;
        hint.Text="正在展开你的通信工作空间…";hint.AutoSize=true;hint.ForeColor=Color.FromArgb(115,114,108);
        Controls.Add(logo);Controls.Add(title);Controls.Add(hint);Resize+=delegate{CenterSplash();};CenterSplash();
        Shown+=async delegate{await Start();};FormClosing+=OnClosing;
        health.Interval=4000;health.Tick+=delegate {
            if(!closing&&loaded&&backend!=null&&backend.HasExited){health.Stop();LogBackendExit();ShowWindow();tray.Text="faswecom · 本机服务已停止";hint.Text="后台服务已停止（"+BackendExitCode()+"）。请从托盘彻底退出后重开。";CenterSplash();logo.BringToFront();title.BringToFront();hint.BringToFront();MessageBox.Show(this,"本机服务已停止，退出码："+BackendExitCode()+"。\n诊断保存在 .tmp/desktop-startup.log 和 .tmp/desktop-fault.log。\n已保存的记录保留，请退出并重新打开 faswecom。","faswecom",MessageBoxButtons.OK,MessageBoxIcon.Information);}
        };
    }
    void PlaceWindow(){
        var area=Screen.PrimaryScreen.WorkingArea;
        int x=area.X+(area.Width-Width)/2,y=area.Y+(area.Height-Height)/2;
        if(Left!=x||Top!=y){StartPosition=FormStartPosition.Manual;Location=new Point(x,y);}
    }
    void CenterSplash(){logo.Left=(ClientSize.Width-logo.Width)/2;logo.Top=ClientSize.Height/2-175;title.Left=(ClientSize.Width-title.Width)/2;title.Top=ClientSize.Height/2-55;hint.Left=(ClientSize.Width-hint.Width)/2;hint.Top=ClientSize.Height/2+15;}
    string StartupCommand(){return "\""+Application.ExecutablePath+"\" --background";}
    void ShowWindow(){Show();ShowInTaskbar=true;if(WindowState==FormWindowState.Minimized)WindowState=FormWindowState.Normal;Activate();}
    void HideToTray(){Hide();ShowInTaskbar=false;if(!trayNotice){trayNotice=true;tray.ShowBalloonTip(4000,"faswecom 仍在运行","MCP 和实时读取继续运行。单击托盘图标打开，右键可彻底退出。",ToolTipIcon.Info);}}
    protected override void WndProc(ref Message m){if(m.Msg==Program.ActivateMessage)ShowWindow();base.WndProc(ref m);}
    object GetJson(string path){var req=(HttpWebRequest)WebRequest.Create(service+path);req.Proxy=null;req.Timeout=2000;using(var r=req.GetResponse())using(var sr=new StreamReader(r.GetResponseStream()))return json.DeserializeObject(sr.ReadToEnd());}
    readonly Stopwatch startupClock=Stopwatch.StartNew();
    void Stage(string stage){try{lock(stageLock)File.AppendAllText(Path.Combine(Program.Root,".tmp","desktop-startup.log"),DateTime.UtcNow.ToString("o")+" "+stage+" "+startupClock.ElapsedMilliseconds+"ms"+Environment.NewLine);}catch{}}
    string BackendExitCode(){try{return "0x"+unchecked((uint)backend.ExitCode).ToString("X8");}catch{return "unknown";}}
    void LogBackendExit(){if(Interlocked.Exchange(ref exitLogged,1)==0)Stage("service-exited code="+BackendExitCode());}
    async Task PrepareBrowser() {
            string browser=Path.Combine(Program.Root,"_internal","WebView2");
#if MCP_ONLY
            if(!File.Exists(Path.Combine(browser,"msedgewebview2.exe"))){
                browser=null;
                try{CoreWebView2Environment.GetAvailableBrowserVersionString();}
                catch(Exception){throw new Exception("轻量版使用系统 Microsoft Edge WebView2。请安装 Microsoft 官方 WebView2 Evergreen Runtime 后重新打开。下载：https://developer.microsoft.com/microsoft-edge/webview2/");}
            }
#else
            if(!File.Exists(Path.Combine(browser,"msedgewebview2.exe")))throw new Exception("缺少随包提供的界面运行组件，请完整解压 ZIP。");
#endif
            var options=new CoreWebView2EnvironmentOptions();
#if QA
            // Test build only: exercise the actual WebView2 DOM/rendering with
            // Playwright. The distributed executable has no debugging endpoint.
            int qaPort;
            if(Int32.TryParse(Environment.GetEnvironmentVariable("CHATWEAVE_QA_PORT"),out qaPort)&&qaPort>=1024&&qaPort<=65535)
                options.AdditionalBrowserArguments="--remote-debugging-port="+qaPort;
#endif
            var env=await CoreWebView2Environment.CreateAsync(browser,Path.Combine(Program.Root,"data","desktop-webview"),options);
            if(closing)return;
            view.Dock=DockStyle.Fill;view.DefaultBackgroundColor=BackColor;Controls.Add(view);logo.BringToFront();title.BringToFront();hint.BringToFront();
            await view.EnsureCoreWebView2Async(env);
            Stage("browser-ready");
    }
    async Task Start() {
        try {
            Directory.CreateDirectory(Path.Combine(Program.Root,".tmp"));
            Directory.CreateDirectory(Path.Combine(Program.Root,"data"));
            // Fail early on a read-only archive/Program Files location.
            string probe=Path.Combine(Program.Root,".tmp","write-probe");File.WriteAllText(probe,"");File.Delete(probe);
            var startupLog=Path.Combine(Program.Root,".tmp","desktop-startup.log");
            try{if(File.Exists(startupLog)&&new FileInfo(startupLog).Length>262144)File.WriteAllText(startupLog,"");}catch{}
            Stage("start");
            Task browserReady=PrepareBrowser();
            if(Program.Connect!="")service=Program.Connect;
            else {
                hint.Text="正在启动…";CenterSplash();
                token=Guid.NewGuid().ToString("N")+Guid.NewGuid().ToString("N");
                ready=Path.Combine(Program.Root,".tmp","desktop-ready-"+Guid.NewGuid().ToString("N")+".json");
#if WECOM
                var start=new ProcessStartInfo(Path.Combine(Program.Root,"runtime","python.exe"),"-X utf8 -B desktop/serve_wecom.py "+Path.GetFileName(ready));
#else
                var start=new ProcessStartInfo(Path.Combine(Program.Root,"runtime","python.exe"),"-X utf8 -B desktop/serve.py "+Path.GetFileName(ready));
#endif
                start.WorkingDirectory=Program.Root;start.UseShellExecute=false;start.CreateNoWindow=true;start.RedirectStandardOutput=true;start.RedirectStandardError=true;
                foreach(string key in new[]{"PYTHONHOME","PYTHONPATH","API_KEY","API_BASE","MODEL","REPLY_ONEBOT_URL","REPLY_ONEBOT_TOKEN","REPLY_ONEBOT_WS_URL","REPLY_ONEBOT_WS_TOKEN","DSH_HOME","DEEPSEEK_HARNESS_RUNTIME_MODE"})start.EnvironmentVariables.Remove(key);
                start.EnvironmentVariables["CHATWEAVE_SESSION_TOKEN"]=token;
                start.EnvironmentVariables["PYTHONUTF8"]="1";
                start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"]="1";
                start.EnvironmentVariables["PYTHONFAULTHANDLER"]="1";
                owner=new Job();backend=new Process();backend.StartInfo=start;
                backend.EnableRaisingEvents=true;backend.Exited+=delegate{LogBackendExit();};
                backend.OutputDataReceived+=delegate{};
                backend.ErrorDataReceived+=delegate(object sender,DataReceivedEventArgs e){if(e.Data!=null)try {var log=Path.Combine(Program.Root,".tmp","desktop-service.log");if(!File.Exists(log)||new FileInfo(log).Length<1048576)File.AppendAllText(log,e.Data+Environment.NewLine,Encoding.UTF8);}catch{}};
                backend.Start();owner.Add(backend);backend.BeginOutputReadLine();backend.BeginErrorReadLine();
                for(int i=0;i<600&&!File.Exists(ready);i++){if(closing)return;if(backend.HasExited)throw new Exception("本机服务启动失败，退出码："+BackendExitCode()+"。诊断保存在 .tmp/desktop-startup.log 和 .tmp/desktop-fault.log。");await Task.Delay(150);}
                if(!File.Exists(ready)){Stage("service-startup-timeout");throw new Exception("本机服务启动超时。诊断已保存在 .tmp/desktop-fault.log，请将该文件发回排查。");}
                var data=json.Deserialize<System.Collections.Generic.Dictionary<string,object>>(File.ReadAllText(ready));service=(string)data["url"];
            }
            Stage("service-ready");
            var status=await Task.Run(()=>GetJson("/api/desktop/status"));
            var statusData=(System.Collections.Generic.Dictionary<string,object>)status;
            if((string)statusData["app"]!="ChatWeave" && (string)statusData["app"]!="faswecom")throw new Exception("目标服务不是兼容的 faswecom 工作台。");
            hint.Text="正在载入界面…";CenterSplash();
            await browserReady;
            if(closing)return;
            Stage("browser-and-service-ready");
            view.CoreWebView2.Settings.AreDevToolsEnabled=false;
            view.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled=false;
            view.CoreWebView2.Settings.IsStatusBarEnabled=false;
            view.CoreWebView2.Settings.AreDefaultContextMenusEnabled=false;
            view.CoreWebView2.Settings.IsPasswordAutosaveEnabled=false;
            view.CoreWebView2.Settings.IsGeneralAutofillEnabled=false;
            view.CoreWebView2.DOMContentLoaded+=(s,e)=>{
                if(closing)return;
                view.BringToFront();Stage("page-dom-ready");
            };
            view.CoreWebView2.NavigationCompleted+=(s,e)=>{
                if(!e.IsSuccess&&e.WebErrorStatus!=CoreWebView2WebErrorStatus.OperationCanceled){hint.Text="页面加载未完成："+e.WebErrorStatus+"。请退出后重试。";CenterSplash();logo.BringToFront();title.BringToFront();hint.BringToFront();}
                Stage(e.IsSuccess?"page-loaded":"page-load-failed");
            };
            view.CoreWebView2.NavigationStarting+=(s,e)=>{
                Uri uri;if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out uri)) {e.Cancel=true;return;}
                if(uri.GetLeftPart(UriPartial.Authority)!=service){e.Cancel=true;}
            };
            view.CoreWebView2.NewWindowRequested+=(s,e)=>{e.Handled=true;Uri uri;if(Uri.TryCreate(e.Uri,UriKind.Absolute,out uri)&&(uri.Scheme=="https"||uri.Scheme=="http"))Process.Start(new ProcessStartInfo(uri.AbsoluteUri){UseShellExecute=true});};
            view.CoreWebView2.PermissionRequested+=(s,e)=>{e.State=CoreWebView2PermissionState.Deny;};
            view.CoreWebView2.DownloadStarting+=(s,e)=>{
                Uri downloadUri;
                if(!Uri.TryCreate(e.DownloadOperation.Uri,UriKind.Absolute,out downloadUri)
                    ||downloadUri.GetLeftPart(UriPartial.Authority)!=service
                    ||!System.Text.RegularExpressions.Regex.IsMatch(downloadUri.AbsolutePath,@"^(?:/w/(?:legacy|[a-f0-9]{64}))?/api/wecom/(?:exports/[a-f0-9]{32}|chat/media/[1-9][0-9]*)/download$")){e.Cancel=true;return;}
                bool attachment=downloadUri.AbsolutePath.Contains("/chat/media/");
                var deferral=e.GetDeferral();
                BeginInvoke(new Action(()=>{
                    try{
                        using(var dialog=new SaveFileDialog()){
                            dialog.Title=attachment?"保存聊天附件":"保存聊天导出文件";dialog.FileName=Path.GetFileName(e.ResultFilePath);
                            bool jsonl=dialog.FileName.EndsWith(".jsonl",StringComparison.OrdinalIgnoreCase);
                            dialog.Filter=attachment?"聊天附件 (*.*)|*.*":jsonl?"JSONL 消息记录 (*.jsonl)|*.jsonl":"SQLite 数据库 (*.sqlite3)|*.sqlite3";
                            dialog.DefaultExt=attachment?Path.GetExtension(dialog.FileName).TrimStart('.'):jsonl?"jsonl":"sqlite3";dialog.AddExtension=true;dialog.OverwritePrompt=true;
                            if(dialog.ShowDialog(this)==DialogResult.OK){e.ResultFilePath=dialog.FileName;e.Handled=true;}
                            else e.Cancel=true;
                        }
                    }catch{e.Cancel=true;}finally{deferral.Complete();}
                }));
            };
            view.CoreWebView2.WebMessageReceived+=(s,e)=>{
                Uri senderUri;
                if(!Uri.TryCreate(e.Source,UriKind.Absolute,out senderUri)||senderUri.GetLeftPart(UriPartial.Authority)!=service)return;
                string command;try{command=e.TryGetWebMessageAsString();}catch{return;}
                if(command=="faswecom:navigate:console")view.CoreWebView2.Navigate(service+"/console?desktop=1");
                else if(command=="faswecom:navigate:data")view.CoreWebView2.Navigate(service+"/?desktop=1");
            };
#if QA
            view.CoreWebView2.WebMessageReceived+=(s,e)=>{if(e.Source.StartsWith(service+"/")){var command=e.TryGetWebMessageAsString();if(command=="chatweave-qa-close"){exitRequested=true;Close();}else if(command=="faswecom-qa-hide")Close();else if(command=="faswecom-qa-show")ShowWindow();}};
#endif
#if WECOM
            Text="faswecom · 企业微信";
            view.CoreWebView2.Navigate(service+"/shell?desktop=1");
#else
            view.CoreWebView2.Navigate(service+"/?desktop=1");
#endif
            loaded=true;health.Start();
            if(Program.Background)HideToTray();
        } catch(Exception e){Stage("startup-failed type="+e.GetType().Name+" code=0x"+unchecked((uint)e.HResult).ToString("X8"));if(!closing){MessageBox.Show(this,e.Message,"faswecom 启动提示",MessageBoxButtons.OK,MessageBoxIcon.Information);closing=true;Close();}}
    }
    async void OnClosing(object sender,FormClosingEventArgs e) {
        if(closing){Cleanup();return;}
        if(e.CloseReason==CloseReason.WindowsShutDown||e.CloseReason==CloseReason.TaskManagerClosing){closing=true;Cleanup();return;}
        e.Cancel=true;
        if(closePending)return;closePending=true;
        if(loaded&&backend!=null&&!backend.HasExited){
            try {
                if(!exitRequested){
                    var prefs=(System.Collections.Generic.Dictionary<string,object>)await Task.Run(()=>GetJson("/api/desktop/preferences"));
                    if((bool)prefs["background"]){closePending=false;HideToTray();return;}
                }
                var status=(System.Collections.Generic.Dictionary<string,object>)await Task.Run(()=>GetJson("/api/desktop/status"));
                if((bool)status["busy"]&&MessageBox.Show(this,"还有任务正在执行。退出会停止当前任务，已保存的内容会保留。\n\n现在退出？","faswecom",MessageBoxButtons.YesNo,MessageBoxIcon.Question)!=DialogResult.Yes){closePending=false;exitRequested=false;return;}
            }catch{}
        }
        closing=true;health.Stop();Hide();
        if(backend!=null&&!backend.HasExited&&service!="") {
            await Task.Run(()=>{try {var req=(HttpWebRequest)WebRequest.Create(service+"/api/desktop/quit");req.Proxy=null;req.Method="POST";req.ContentLength=0;req.Timeout=2000;req.Headers["Authorization"]="Bearer "+token;using(var response=req.GetResponse()){}backend.WaitForExit(6000);}catch{}});
        }
        Cleanup();Close();
    }
    void Cleanup(){if(cleaned)return;cleaned=true;health.Stop();tray.Visible=false;tray.Dispose();view.Dispose();if(logo.Image!=null){logo.Image.Dispose();logo.Image=null;}if(owner!=null){owner.Dispose();owner=null;}if(backend!=null){backend.Dispose();backend=null;}if(ready!="")try{File.Delete(ready);}catch{}}
}

sealed class Job:IDisposable {
    IntPtr handle;
    [StructLayout(LayoutKind.Sequential)]struct Basic {public long UserTime,JobTime;public uint Flags;public UIntPtr Min,Max;public uint Active;public UIntPtr Affinity;public uint Priority,Scheduling;}
    [StructLayout(LayoutKind.Sequential)]struct IO {public ulong ReadOps,WriteOps,OtherOps,ReadBytes,WriteBytes,OtherBytes;}
    [StructLayout(LayoutKind.Sequential)]struct Extended {public Basic Basic;public IO IO;public UIntPtr ProcessMemory,JobMemory,PeakProcess,PeakJob;}
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)]static extern IntPtr CreateJobObject(IntPtr attr,string name);
    [DllImport("kernel32.dll",SetLastError=true)]static extern bool SetInformationJobObject(IntPtr job,int type,ref Extended info,uint length);
    [DllImport("kernel32.dll",SetLastError=true)]static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll")]static extern bool CloseHandle(IntPtr handle);
    public Job(){handle=CreateJobObject(IntPtr.Zero,null);var info=new Extended();info.Basic.Flags=0x2000;if(handle==IntPtr.Zero||!SetInformationJobObject(handle,9,ref info,(uint)Marshal.SizeOf(info)))throw new Exception("无法建立本机服务进程管理。");}
    public void Add(Process p){if(!AssignProcessToJobObject(handle,p.Handle)){try{p.Kill();}catch{}throw new Exception("无法托管本机服务进程。");}}
    public void Dispose(){if(handle!=IntPtr.Zero){CloseHandle(handle);handle=IntPtr.Zero;}}
}