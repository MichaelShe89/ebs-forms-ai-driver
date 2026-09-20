<#
  EBS 界面自动化 —— 一键落地脚本

  它替你做掉手册 §2.2 的八步里最容易出错的那几步，尤其是：
    R2  每次构建都用【全新包名 + 全新 jar 路径】—— 本脚本用时间戳自动生成，
        人不可能违反，也就不会撞上那个"改了代码却跑旧逻辑"的坑。
    R4  loadAgent 的 jar 路径和 args 里的 jar 路径【逐字节相同】—— 本脚本
        全程用同一个变量，不给手打两遍的机会。

  用法：
    .\setup.ps1 -CheckOnly     只体检，不改任何东西（先跑这个）
    .\setup.ps1                体检 + 构建 + 附加 + 握手验证
    .\setup.ps1 -Port 8200     换端口

  前提：Forms 已由【人工】登录并在运行。本脚本不会去拉起 Forms（手册 D9）。
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [int]$Port = 8159,
    [int]$TargetPid = 0
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$mcp  = Join-Path $root 'tools\forms-mcp'
$ok = $true

function Say($t)  { Write-Host $t }
function Pass($t) { Write-Host "  [PASS] $t" -ForegroundColor Green }
function Fail($t) { Write-Host "  [FAIL] $t" -ForegroundColor Red; $script:ok = $false }
function Warn($t) { Write-Host "  [ .. ] $t" -ForegroundColor Yellow }
function Head($t) { Write-Host ""; Write-Host ("=" * 62); Write-Host $t; Write-Host ("=" * 62) }

# 每次运行都提醒一次。这条不该只写在文档里 —— 文档会被跳过，屏幕不会。
Write-Host ""
Write-Host ("!" * 62) -ForegroundColor Yellow
Write-Host "  TEST ENVIRONMENTS ONLY - NOT FOR PRODUCTION" -ForegroundColor Yellow
Write-Host "  仅限测试环境（开发 / 测试 / 验收 / 沙箱），禁止用于生产实例。" -ForegroundColor Yellow
Write-Host "  面向 IT / ERP 团队；使用前需取得 IT 与信息安全批准。" -ForegroundColor Yellow
Write-Host "  详见 SECURITY.md" -ForegroundColor Yellow
Write-Host ("!" * 62) -ForegroundColor Yellow

# ---------------------------------------------------------------- 1. Python
Head "1. Python 解释器"

function Find-RealPython {
    $cands = @()
    # 项目自带的 venv 最优先：位数、依赖都已经对好了
    $cands += (Join-Path $mcp '.venv\Scripts\python.exe')
    $cands += (Join-Path (Split-Path $root -Parent) 'tools\forms-mcp\.venv\Scripts\python.exe')
    foreach ($c in (Get-Command python, python3 -ErrorAction SilentlyContinue)) { $cands += $c.Source }
    foreach ($d in @("$env:LOCALAPPDATA\Programs\Python", 'C:\Python313', 'C:\Python312', 'C:\Python311')) {
        if (Test-Path $d) {
            $cands += (Get-ChildItem $d -Recurse -Filter python.exe -Depth 2 -ErrorAction SilentlyContinue |
                       Select-Object -Expand FullName)
        }
    }
    foreach ($c in ($cands | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path $c)) { continue }
        # Windows Store 的占位符是 0 字节，调用它只会弹商店
        if ((Get-Item $c).Length -eq 0) { continue }
        try { $v = & $c -V 2>&1 } catch { continue }
        if ("$v" -match 'Python 3') { return [pscustomobject]@{ Path = $c; Version = "$v" } }
    }
    return $null
}

$py = Find-RealPython
if ($py) {
    Pass "$($py.Version)  ->  $($py.Path)"
    $pyBits = & $py.Path -c 'import sys;print(64 if sys.maxsize>2**32 else 32)'
    Pass "位数：$pyBits 位"
} else {
    Fail "找不到可用的 Python 3"
    Say  "        PATH 上的 python 很可能是 Windows Store 的 0 字节占位符（手册 P26）"
    Say  "        判据： (Get-Item (Get-Command python).Source).Length  为 0 即是占位符"
    Say  "        解法： 装一个 Python 3 并勾选 Add to PATH，或指出已有 venv 的绝对路径"
    $pyBits = 0
}

# ------------------------------------------------------------------ 2. Forms
Head "2. Forms 客户端（必须已由人工登录）"

$procs = Get-Process -Name jp2launcher, javaw, javaws -ErrorAction SilentlyContinue
if ($procs) {
    foreach ($p in $procs) { Pass "pid $($p.Id)  $($p.ProcessName)" }
    if ($TargetPid -eq 0) { $TargetPid = $procs[0].Id }
    Say "        目标 pid = $TargetPid"
} else {
    Fail "没有发现 Forms 的 JVM 进程"
    Say  "        请【人工】打开并登录 Forms，然后重跑本脚本。"
    Say  "        不要试图自己拉起 Forms —— 会卡在 Starting application（手册 D9）"
}

# ------------------------------------------------------- 3. Access Bridge DLL
Head "3. Java Access Bridge"

if ($pyBits -gt 0) {
    $dll = "$env:WINDIR\System32\WindowsAccessBridge-$pyBits.dll"
    if (Test-Path $dll) {
        Pass "$dll"
    } else {
        Fail "缺少 $dll"
        Say  "        执行： jabswitch -enable    然后【重启 Forms】（不重启不生效）"
    }
} else {
    Warn "跳过（Python 未就绪，无法确定位数）"
}

# --------------------------------------------------------------------- 4. JDK
Head "4. JDK 8（Attach API 需要 tools.jar）"

function Find-Jdk8 {
    $dirs = @()
    foreach ($b in @('C:\Program Files\Eclipse Adoptium', 'C:\Program Files\Java',
                     'C:\Program Files\Microsoft', 'C:\Program Files (x86)\Java')) {
        if (Test-Path $b) { $dirs += Get-ChildItem $b -Directory -ErrorAction SilentlyContinue }
    }
    foreach ($d in $dirs) {
        $javac = Join-Path $d.FullName 'bin\javac.exe'
        $tools = Join-Path $d.FullName 'lib\tools.jar'
        # tools.jar 只存在于 JDK 8 及更早版本（JDK 9+ 已移除），
        # 所以它存在本身就等价于"这是 JDK 8"。不调 javac -version ——
        # 原生程序的 stderr 配 2>&1 在 PowerShell 5.1 下会被包成 ErrorRecord 而中断。
        if ((Test-Path $javac) -and (Test-Path $tools)) { return $d.FullName }
    }
    return $null
}

$jdk = Find-Jdk8
if ($jdk) { Pass $jdk } else {
    Fail "找不到带 tools.jar 的 JDK 8"
    Say  "        JRE 不行，必须是 JDK（tools.jar 里才有 Attach API）"
    Say  "        推荐 Eclipse Adoptium jdk8"
}

# ------------------------------------------------------------------- 5. 源码
Head "5. 交付包完整性"

$need = @(
    'jab.py', 'bg.py', 'server.py', 'keys.py',
    'driver\drive.py', 'driver\nav.py', 'driver\flow.py', 'driver\witness.py', 'driver\smoke.py',
    'agent\Attach.java', 'agent\src\si\Loader.java', 'agent\src\si\Driver.java'
)
$missing = @($need | Where-Object { -not (Test-Path (Join-Path $mcp $_)) })
if ($missing.Count -eq 0) { Pass "$($need.Count) 个必需文件齐全" }
else { Fail "缺文件： $($missing -join ', ')"; Say "        解压时不要拆散目录结构" }

# ------------------------------------------------------------------ 体检结束
if (-not $ok) {
    Head "体检未通过"
    Say "上面标 [FAIL] 的先解决掉，再重跑。没解决之前不要往下走 ——"
    Say "跳过前置条件去硬试 agent，只会得到误导性的报错（手册 §0.2）。"
    exit 1
}
if ($CheckOnly) {
    Head "体检全部通过"
    Say "下一步： .\setup.ps1        （构建 + 附加 + 验证）"
    exit 0
}

# ============================================================== 构建 + 附加
#
# 两条硬约束，都由代码保证，不交给人：
#
#  R2  一个包名在一个 JVM 里【只能成功加载一次】。之后同包名再 attach 必失败，
#      而且报错和"jar 有问题"一模一样 —— 这是重试时最容易被误导的地方。
#      所以每次尝试都生成全新包名。
#
#  目录  agent jar 放在哪里，目标 JVM 不一定读得到。实测同一份 jar（SHA 相同）：
#          %USERPROFILE%\.formsdrive      OK
#          %USERPROFILE%\<任意新建目录>    OK
#          %LOCALAPPDATA%\Temp            OK
#          %LOCALAPPDATA%\<其它新建目录>  FAIL
#          含非 ASCII 字符的路径         FAIL
#        根因未完全查清，所以按候选目录依次重试，直到成功。

function New-AgentJar {
    param([string]$JarDir, [string]$Pkg, [string]$Jdk, [string]$Mcp)
    $b = Join-Path $env:TEMP "ebsagent_$Pkg"
    New-Item -ItemType Directory -Force -Path "$b\src\com\aitest\$Pkg", "$b\classes", $JarDir | Out-Null
    $u = New-Object System.Text.UTF8Encoding($false)          # javac 不吃 BOM
    foreach ($fn in 'Loader', 'Driver') {
        $txt = Get-Content (Join-Path $Mcp "agent\src\si\$fn.java") -Raw -Encoding UTF8
        $txt = $txt -replace 'package com\.aitest\.si;', "package com.aitest.$Pkg;" `
                    -replace 'com\.aitest\.si\.',        "com.aitest.$Pkg."
        [System.IO.File]::WriteAllText("$b\src\com\aitest\$Pkg\$fn.java", $txt, $u)
    }
    # 不用 2>&1：PowerShell 5.1 会把原生 stderr 包成 ErrorRecord 并中断
    & "$Jdk\bin\javac.exe" -source 8 -target 8 -nowarn -d "$b\classes" `
        "$b\src\com\aitest\$Pkg\Loader.java" "$b\src\com\aitest\$Pkg\Driver.java" 2> "$b\javac.log"
    if ($LASTEXITCODE -ne 0) {
        Get-Content "$b\javac.log" -EA SilentlyContinue | ForEach-Object { Say "        $_" }
        return $null
    }
    $jar = Join-Path $JarDir "agent_$Pkg.jar"
    [System.IO.File]::WriteAllText("$b\manifest.txt",
        "Agent-Class: com.aitest.$Pkg.Loader`r`nCan-Retransform-Classes: true`r`n", $u)
    & "$Jdk\bin\jar.exe" cfm $jar "$b\manifest.txt" -C "$b\classes" com
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not (Test-Path "$b\Attach.class")) {
        & "$Jdk\bin\javac.exe" -cp "$Jdk\lib\tools.jar" -d $b (Join-Path $Mcp 'agent\Attach.java') | Out-Null
    }
    return [pscustomobject]@{ Jar = $jar; Build = $b }
}

Head "6. 生成口令"
$driveDir = Join-Path $mcp 'agent\drive'
New-Item -ItemType Directory -Force -Path $driveDir | Out-Null
$secretFile = Join-Path $driveDir 'secret.txt'
$secret = [Guid]::NewGuid().ToString('N')
[System.IO.File]::WriteAllText($secretFile, $secret, (New-Object System.Text.ASCIIEncoding))
Pass "已写入 $secretFile（32 字符，无换行）"
Say  "        用完请删除；不要提交代码库，不要放进要外发的压缩包。"

Head "7. 构建并附加（自动换包名 / 换目录重试）"

$candidates = @(
    (Join-Path $env:USERPROFILE '.ebs-ai-agent'),
    $env:TEMP,
    (Join-Path $env:USERPROFILE '.formsdrive')
)
$attached = $false
foreach ($dir in $candidates) {
    $pkg = 'a' + (Get-Date -Format 'MMddHHmmssfff')      # 每次尝试都是新包名 -> R2
    Say ""
    Say "  尝试： 包名 com.aitest.$pkg"
    Say "         目录 $dir"
    $built = New-AgentJar -JarDir $dir -Pkg $pkg -Jdk $jdk -Mcp $mcp
    if (-not $built) { Warn "构建失败，换下一个目录"; continue }

    # jar 路径全程只用 $built.Jar 这一个变量 —> loadAgent 与 args 逐字节一致（R4）
    & "$jdk\bin\java.exe" -cp "$($built.Build);$jdk\lib\tools.jar" `
        Attach $TargetPid $built.Jar "$($built.Jar)|$Port|$secret" 2> "$($built.Build)\attach.log"
    if ($LASTEXITCODE -eq 0) {
        Pass "agent loaded  <-  $($built.Jar)"
        $attached = $true
        break
    }
    $err = (Get-Content "$($built.Build)\attach.log" -EA SilentlyContinue) -join ' '
    if ($err -match 'AgentLoadException|not found or no Agent-Class') {
        Warn "该目录下目标 JVM 读不到 jar，换下一个"
    } else {
        Warn "attach 失败：$err"
    }
}

if (-not $attached) {
    Head "附加失败"
    Say "所有候选目录都试过了。按这个顺序排查："
    Say "  1. pid 选对了吗？ jps -l 里那个 com.sun.javaws.Main 才是 Forms"
    Say "  2. JDK 主版本要和 Forms 的 JRE 一致（都是 8）"
    Say "  3. 换个自己新建的目录试： .\setup.ps1 -JarDir C:\Users\<你>\myagent"
    Say "  注意：同一个包名在同一个 JVM 里只能成功加载一次，"
    Say "        本脚本每次都换新包名，所以可以放心重跑。"
    exit 1
}

Head "8. 握手验证"
Start-Sleep -Milliseconds 800
try {
    $cli = New-Object System.Net.Sockets.TcpClient('127.0.0.1', $Port)
    $st  = $cli.GetStream()
    $w   = New-Object System.IO.StreamWriter($st); $w.NewLine = "`n"; $w.AutoFlush = $true
    $r   = New-Object System.IO.StreamReader($st)
    $w.WriteLine("auth $secret"); $a = $r.ReadLine()
    if ($a -eq 'OK auth') { Pass "auth   -> $a" } else { Fail "auth   -> $a" }
    $w.WriteLine('ping');   Pass ("ping   -> " + $r.ReadLine())
    $w.WriteLine('frames'); $fr = $r.ReadLine()
    if ($fr -match 'showing=true') {
        Pass ("frames -> " + $fr.Substring(0, [Math]::Min(78, $fr.Length)))
    } else { Fail "frames 没看到可见窗口： $fr" }
    $cli.Close()
} catch {
    Fail "连不上 127.0.0.1:$Port —— $($_.Exception.Message)"
    exit 1
}

Head "全部通过"
Say "读写通道就绪。接着跑自检第 2、3 条："
Say ""
Say "  cd `"$mcp\driver`""
Say "  `"$($py.Path)`" -X utf8 smoke.py"
Say ""
Say "停 agent（也会随 Forms 关闭自动消失）： 连上 $Port 发 auth 再发 stop"
Say ""