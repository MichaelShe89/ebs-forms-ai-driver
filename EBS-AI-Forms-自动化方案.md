# 用 AI 驱动 Oracle EBS Forms —— 技术方案与执行规格

> **读者是 AI 助手，不是人。** 这份文档是给助手直接执行的规格说明：
> 包含完整可编译源码、通信协议、判定规则、39 条故障字典、7 条排查规矩。
>
> **交付物 = 本文件 + `tools/` 整个目录**（清单见 §2.1，缺一不可）。
> 人类交付者在 **§15** 填入环境信息和具体任务，然后把两者一起交给助手。
> 助手拿到后**先按 §2.2 落地、通过 §13 自检**，再开始做任务。
>
> **文档内所有示例值都是占位符**，需要换成你们自己实例的实际值；
> 每一处都说明了怎么用 SQL 查出来。键位映射、LOV 快捷键、字段状态行为
> 这些**因实例而异**，凡是写"我们实测的那个实例上如何如何"的地方，
> 都要在你们自己的环境上重新验证一遍，不要直接当成事实。

---

## 0. 你的身份与任务边界

你正在被要求**通过图形界面操作 Oracle EBS R12**，完成配置（setup）与业务测试。

### 0.1 不可协商的约束

| # | 约束 | 原因 | 违反的后果 |
|---|---|---|---|
| C1 | **只走界面，禁止写数据库** | Forms Personalization / WHEN-VALIDATE-ITEM / 客制化校验只在界面层触发 | 直接 INSERT 绕过全部客制化逻辑，测试结论无效 |
| C2 | **禁止抢占鼠标键盘** | 用户在同一台机器上同时工作 | 用户当场中断你；`SendInput`/`keybd_event`/`mouse_event`/PyAutoGUI 全部禁用 |
| C3 | **每步用只读 SQL 核对** | 界面提示"成功"与落库成功不等价（见 P13） | 报告假阳性 |
| C4 | **改配置前先报计划** | 配置错误影响其他人在用的环境 | 未经确认写配置 = 事故 |
| C5 | **不碰任务范围外的功能** | 环境是共享的 | 同上 |

C2 的技术含义：**唯一允许的动作通道是"注入到 Forms JVM 内部派发 AWT 事件"**。
为什么别的通道全都不行，见 §1.1 的对照表 —— 那张表能让你不必再自己验证一遍。

C2 有两个已知例外，都在代码里标了，都有替代路径，**不要拿它们当放宽 C2 的借口**：

- `forms_key`（MCP 工具）会抢键盘 —— 功能键必须是真按键。
  **改用** Forms 自己的菜单（Save / Enter Query / Select All 等大多数功能键都有菜单等价项），
  或 `drive.py` 的 `key()`，后者在 JVM 内部发按键事件，完全不进输入队列。
- `ensure_awake` 在 Forms 处于最小化时会闪一次前台 —— Windows 不给图标化窗口 active 状态，
  只能先还原、再立刻把前台还回去。**每个 session 一次，不是每个动作一次**，
  且开工前请用户自己把 Forms 窗口留在屏幕上，这一次也能省掉。

除此之外，任何"只好抢一下焦点"的念头都是错的，先回 §1.1 的对照表找已有的通道。

### 0.2 开工前必须确认的三件事

```
1. Forms 客户端已由人工登录并处于运行状态 —— 你不负责登录
2. 只读数据库连接可用
3. agent 端口与口令已就绪（tools/forms-mcp/agent/drive/secret.txt）
```

若 Forms 未启动：**要求人类打开它**。不要尝试自己拉起（见 D9）。

> **先读 §14《排查方法》。** 手册里 36 条故障条目是结论，§15 是产生这些结论的方法。
> 你一定会遇到没写进字典的新问题，那时候能不能自己走出来，取决于 §14 而不是字典。
>
> **给"从零复现"的你：不要另起炉灶写探针 agent。**
> 直接用 §4.2 的 `Loader.java` 原文 + §4.3 的 `Driver.java` 原文，只改包名。
> 自己写最小 agent 去试探沙箱边界，会得到误导性的结论 —— 已经有复现者因此
> 断言"沙箱级限制、方案不可行"，而真实原因只是 jar 路径没对上（P25 / R4）。
> 这套机制在带 `AWTAppletSecurityManager` 的 JVM 上被反复验证通过。

---

## 1. 三条通道

这不是"屏幕自动化"。是三条职责分离的通道：

| 通道 | 用途 | 实现 | 能力边界 |
|---|---|---|---|
| **读** | 界面结构、字段名、当前值、状态、屏幕坐标 | Java Access Bridge (JAB) | 只读。对文本项**不暴露任何 action** |
| **写** | 点击、按键、输入、勾选、选择 | 注入 Forms JVM，`Component.dispatchEvent()` | 全能，且不进系统输入队列 |
| **验** | 真实落库状态 | 只读 SQL | 唯一可信的结果来源 |

### 1.1 为什么写必须走 JVM 注入

| 方案 | 结论 | 机理 |
|---|---|---|
| `SendInput` | ❌ 抢占输入 | 进入系统共享输入队列，只落到当前前台窗口 |
| `PostMessage` 键盘消息 | ❌ 无效 | 键盘消息需 focus owner；窗口非激活时 Java 不分配，消息被丢弃 |
| `PostMessage` 鼠标消息给 frame / SunAwtCanvas | ❌ 无效 | 任何坐标都不移动 Forms 当前项 |
| JAB `setTextContents` | ❌ 空操作 | 返回 true，值不变 |
| JAB `requestFocus` | ❌ 半无效 | 只移动 Java 焦点，Forms 当前项不跟随 |
| **JVM 内 `dispatchEvent`** | ✅ **可用** | 事件在进程内产生并分发，Forms 无法区分真假；系统输入队列不参与 |

### 1.2 不干扰的自证方法

不要靠感觉。跑完一轮后必须能给出这三条：

```
we_took_foreground = never      # 前台窗口全程未被抢走
keys_stolen        = never      # 用户在别处打的字一个不少
forms_hwnd_active  = false      # Forms 窗口自始至终不是激活窗口
```

实现见 `driver/witness.py`：`start()` 记录当前前台窗口，`check()` 只在 **Forms 窗口自身取得焦点** 时报警。
**注意：witness 自身不得弹窗、不得置顶。** 早期版本用记事本做见证窗口，本身就是干扰源（见 P16）。

---

## 2. 环境准备

| 组件 | 要求 | 验证命令 |
|---|---|---|
| EBS Forms 客户端 | 人工已登录，Navigator 可用 | 目视 |
| JDK 8 | 主版本必须与 Forms 的 JRE 一致 | `jps -l` 能列出 `com.sun.javaws.Main` |
| `tools.jar` | Attach API 所在 | `dir "%JDK%\lib\tools.jar"` |
| Java Access Bridge | 已启用 | `jabswitch -enable`，然后**重启 Forms** |
| **Python 与 JRE 位数一致** | 见下方红框 | `python -c "import sys;print(sys.maxsize>2**32)"` |
| Python 3 | 驱动端。**必须是真的 Python，不是 Windows Store 占位符** | `python -V` 要打印版本号；若无输出或弹出商店，见 P26 |
| 只读 DB 账号 | SELECT APPS 业务表 | 单独申请，只给 SELECT |
| **独立自动化 EBS 账号** | 独立于个人账号 | 见 §10，比看上去重要 |

```bat
:: 找到 Forms 的 JVM 进程号
"%JDK%\bin\jps.exe" -l
:: 输出形如：  12345 com.sun.javaws.Main     <-- 这个 pid 就是目标

:: 启用 Access Bridge（之后必须重启 Forms）
jabswitch -enable
```

> **位数必须一致，这条最容易漏。**
> Access Bridge **不跨位数**：64 位 Python 只能加载 `WindowsAccessBridge-64.dll`，
> 也只能看见 64 位 JVM。**如果 Forms 跑的是 32 位 JRE，你的 Python 也必须是 32 位。**
> 症状：`bg.Session().forms()` 返回空列表，一切正常但什么都看不见 —— 极易误判成
> "Access Bridge 没启用"而浪费大量时间。
> `jab.py` 已按解释器位数自动选择 DLL，位数不匹配时会直接报错并说明原因。

### 2.1 交付物清单

本手册**必须连同 `tools/forms-mcp/` 目录一起交付**。目录里没有一个文件是可省略的：

```
setup.ps1          一键落地：体检 + 构建 + 附加 + 验证。【从这里开始】
tools/ebs-db/      数据库通道：ebsql.py（命令行查 SELECT）+ db.py
                   —— 没有它，规矩 4「界面答不了就查库」根本没法执行
README.md          三行快速开始
tools/forms-mcp/
├─ jab.py          327 行  Java Access Bridge 的 ctypes 绑定。最底层，读界面靠它
├─ bg.py           710 行  后台会话：窗口枚举/恢复、节点快照、菜单、按钮、LOV、树行、选择
├─ server.py       661 行  JAB 动作封装（_do_action 等），bg 与 driver 都依赖
├─ keys.py         483 行  剪贴板与按键辅助（已修 64 位句柄截断，见 P15）
├─ navigate.py     130 行  职责/菜单导航辅助
├─ agent/
│  ├─ drive/               口令目录，自己生成 secret.txt 放这里（§2.2 步骤④）
│  ├─ Attach.java           把 agent 装进运行中的 JVM
│  └─ src/si/
│     ├─ Loader.java        突破 JWS 沙箱（引导类加载器提权）
│     └─ Driver.java  805 行 常驻命令服务器
└─ driver/
   ├─ drive.py     213 行  Python 驱动端（Forms 类）——你主要用这个
   ├─ smoke.py     140 行  落地自检脚本，跑通它再开始做任务
   ├─ nav.py       119 行  导航树
   ├─ flow.py      132 行  分阶段可续跑编排（模板）
   ├─ witness.py    54 行  不干扰的被动自证
   └─ cdp.py        79 行  网页端 CDP 客户端
```

> **关于 Python 解释器**：交付包**不含** `.venv`（几千个文件，体积太大）。
> 你需要机器上有一个真的 Python 3。若原项目目录还在，
> `tools/forms-mcp/.venv/Scripts/python.exe` 就是现成可用的，用绝对路径调即可。
> 注意 Windows 上 PATH 里的 `python` 很可能是 0 字节占位符（P26）。

**依赖链**（缺一不可，这是最容易漏的地方）：

```
flow.py ──→ drive.py ──→ bg.py ──→ server.py ──→ jab.py ──→ WindowsAccessBridge.dll
   │            │           │
   └─→ nav.py ──┘           └─→ keys.py
```

`drive.py` 用 `parents[1]` 相对定位父目录来 import `bg`/`jab`/`server`。
**不要把这些文件拆开放，也不要把绝对路径写回去** —— 整个目录原样解包即可运行。

### 2.2 首次落地步骤

> **优先用一键脚本。** 交付包根目录有 `setup.ps1`，它把下面 ①–⑦ 全做了，
> 并且**把 R2、R4、R5 三条硬约束变成代码保证**（每次自动换包名、路径只用一个变量、
> 候选目录依次重试）—— 这三条正是手工操作最常翻车的地方。
>
> ```powershell
> .\setup.ps1 -CheckOnly    # 只体检，不改任何东西
> .\setup.ps1               # 体检 + 构建 + 附加 + 握手验证
> ```
>
> 脚本跑完全绿，就等于手册 §13 自检的第 1 条已过。下面的手工步骤留作原理参考，
> 以及脚本在你的环境里跑不通时的排查依据。

#### 手工步骤（按顺序执行，每步有验收标准）

```
① 解包
   把 tools/forms-mcp/ 放到任意位置。不要拆散目录结构。

②a 验证数据库通道（规矩 4 的前提）
   配好 ~/.ebs/connections.json 后：
       python tools/ebs-db/ebsql.py "select 1 from dual"
   通不过就先解决它 —— 没有数据库，你只能拿界面当唯一事实来源，
   而界面会骗你（P13 / P35 / §14.2a）。

② 确认 Python 是真的，再装依赖
   python -V
   # 必须打印出 "Python 3.x.x"。若无任何输出、或弹出 Microsoft Store，
   # 说明 PATH 上是 Windows Store 的 0 字节占位符 —— 见 P26，先解决它。
   python -m pip install websocket-client        # 仅网页端需要
   验收：python -c "import sys;print(64 if sys.maxsize>2**32 else 32)"
        这个数必须等于 Forms 的 JRE 位数（见上方红框）

③ 启用 Access Bridge
   jabswitch -enable          然后【重启 Forms】，不重启不生效
   验收：cd driver && python smoke.py
        应打印 "PASS 看到 N 个窗口"。返回 0 个窗口 = 没重启，或位数不匹配

④ 生成 agent 口令
   验收：agent/drive/secret.txt 存在且非空（生成命令见下）

⑤ 编译 agent
   见 §3.2。验收：agent_v1.jar 生成

⑥ 人工登录 Forms，取 pid
   "%JDK%\bin\jps.exe" -l          → 记下 com.sun.javaws.Main 的 pid
   验收：pid 是个数字

⑦ 附加 agent
   java -cp ".;%JDK%\lib\tools.jar" Attach <pid> <jar绝对路径> "<jar绝对路径>|8159|<口令>"
   验收：Python 端连上 127.0.0.1:8159，auth 返回 OK auth，ping 返回 Java 版本

⑧ 跑完整自检
   cd driver
   python smoke.py --write "<窗口名>" "<字段名>" "<值>"
   三条全过才算落地成功。第 3 条不过 = 方法不成立，停止。
```

**生成口令**（每台机器一次，不要复用别人的）：

```bat
mkdir tools\forms-mcp\agent\drive
powershell -c "[Guid]::NewGuid().ToString('N') | Out-File -Encoding ascii -NoNewline tools\forms-mcp\agent\drive\secret.txt"
```

口令要求：随机、只存本地文件、不进代码库、不写进脚本。
`drive.py` 从 `agent/drive/secret.txt` 读取，附加 agent 时用同一个值。

---

## 3. 构建与附加

### 3.1 三条不可违反的构建规则

```
R1  agent 类必须放在自有包名下（如 com.aitest.v1）。
    放在默认包会与 Oracle 的签名 jar 冲突：
    SecurityException: class "..." 's signer information does not match

R2  每次 attach，都必须用【全新包名】和【全新 jar 文件名】—— 不只是"改了代码才换"。
    ★ 一个包名在一个 JVM 里【只能成功加载一次】。加载过之后（成功或失败都算），
      同包名再 attach 一律失败，报错和"jar 有问题"长得一模一样。
      这让"改个地方重试一下"变成陷阱：你以为在验证修改，其实是在撞同名墙。
      —— 用时间戳做包名，或者直接用 setup.ps1（它每次自动换）。
    系统类加载器按路径缓存 jar，已定义的类直接复用。
    症状：改了代码、重新附加、报 agent loaded，但跑的还是旧逻辑，且无任何报错。
    这条会浪费你几个小时。

R3  Class.forName 必须用【不带加载器参数】的重载。
    Class.forName(name, true, null) 会触发 getClassLoader 权限检查 -> AccessControlException。
    普通委派会先问引导加载器，而 jar 已挂上去，效果相同且不触发检查。

    ★ R2 和 R4 是【同一个故障的两面】，这是最容易误诊的地方：
      违反 R2 时，Loader 类还是【旧 jar 里那个已定义的类】，它的 code source
      指向旧 jar；而 args 里传的是新 jar 路径 -> new JarFile(新路径) 被 checkRead
      拒绝 -> 抛 SecurityException。
      于是你看到的是"沙箱不让读文件"，真实原因却是"类没换成新的"。
      只要 R2 守住（新包名 + 新 jar 名），R4 自然成立。

R5  agent jar 放在哪个目录，目标 JVM 不一定读得到 —— 与 jar 内容无关。
    实测同一份 jar（SHA256 相同），只换位置：
        %USERPROFILE%\.formsdrive            OK
        %USERPROFILE%\<任意新建目录>          OK
        %LOCALAPPDATA%\Temp                  OK
        %LOCALAPPDATA%\<其它新建目录>         FAIL
        含非 ASCII 字符的路径                  FAIL
    失败时的报错依然是那句通用的 "Agent JAR not found or no Agent-Class attribute"，
    完全看不出是目录问题。根因未查清（Temp 在 LOCALAPPDATA 下却可以，故不是简单的
    目录黑名单）。工程解法：按候选目录依次重试，setup.ps1 已内置。

R4  传给 loadAgent 的 jar 路径，和 args 里那个 jar 路径，必须【逐字节相同】。
    new JarFile(jarPath) 能成功的唯一原因是：它读的是 agent【自己被加载的那个 jar】。
    类加载器只给每个 code source 授予"读自己"的权限；换成任何别的路径 ——
    哪怕是同一个文件的不同写法（大小写、正反斜杠、8.3 短名、相对路径）——
    checkRead 都会拒绝，抛 SecurityException。
    症状：new File(path) 成功，new JarFile(path) 失败。见 P25。
```

### 3.2 构建命令

```bat
set JDK=C:\Program Files\Java\jdk1.8.0_xxx
set PKG=com\aitest\v1

javac -source 8 -target 8 -d classes src\%PKG%\*.java
echo Agent-Class: com.aitest.v1.Loader > manifest.txt
jar cfm agent_v1.jar manifest.txt -C classes com

javac -cp "%JDK%\lib\tools.jar" Attach.java
java  -cp ".;%JDK%\lib\tools.jar" Attach <pid> <jar绝对路径> "<jar绝对路径>|8159|<口令>"
```

> **注意上面那行 `Attach` 命令里 `<jar绝对路径>` 出现了两次 —— 必须填成完全一样的字符串。**
> 第一个是 `loadAgent` 用来加载 agent 的，第二个是 agent 拿去 `new JarFile()` 的。
> 两者不一致就会在 `new JarFile()` 上抛 SecurityException（规则 R4 / P25）。
> 稳妥做法：先把路径存进一个变量再引用两次，不要手打两遍。

附加成功的标志：Python 端能连上 `127.0.0.1:8159`，`auth <口令>` 返回 `OK auth`，`ping` 返回 Java 版本。

`ping` 的回显里会带 `sm=sun.plugin2.applet.AWTAppletSecurityManager` —— **看到这个是正常的**，
说明沙箱确实在场，而 agent 已经在它下面拿到了权限。不要因为看到 SecurityManager 就以为失败了。

---

## 4. 完整源码

以下为实测通过的全部源码，可直接使用。

### 4.1 `Attach.java` — 把 agent 装进运行中的 JVM

```java
import com.sun.tools.attach.VirtualMachine;

/** Load an agent into an already-running JVM, by pid. */
public class Attach {
    public static void main(String[] a) throws Exception {
        VirtualMachine vm = VirtualMachine.attach(a[0]);
        try {
            vm.loadAgent(a[1], a[2]);
            System.out.println("agent loaded into pid " + a[0]);
        } finally {
            vm.detach();
        }
    }
}
```

### 4.2 `Loader.java` — 突破 Java Web Start 沙箱

**核心机制**：JWS 下的 agent 代码没有任何权限，且 **JWS 不认 `${user.home}/.java.policy`**（见 D8）。
解法是把自己的 jar 挂到**引导类加载器**上——由它加载的类 `ProtectionDomain` 为 `null`，等同 AllPermission。
读"自己的 jar"是被允许的（类加载器本就授予对自身 code source 的读权限），所以这条路不需要任何外部授权。

```java
package com.aitest.si;

import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.util.jar.JarFile;

/**
 * Stage-by-stage loader, to locate a failure that cannot be printed.
 *
 * The bootstrap-loading approach works under a mimic of the Web Start sandbox
 * but fails inside the real Forms client, whose stderr goes nowhere visible and
 * whose Attach API reports only "failed to initialize". Returning normally is
 * the one signal that does escape, so each stage stops just after one step: the
 * last stage that reports "agent loaded" is the last step that worked.
 *
 * args: "<jarPath>|<port>|<secret>|<stage>"
 */
public class Loader {

    public static void agentmain(String args, Instrumentation inst)
            throws Exception {
        String[] a = args.trim().split("[|]");
        String jarPath = a[0];
        String port = a.length > 1 ? a[1] : "8137";
        String secret = a.length > 2 ? a[2] : "";
        int stage = a.length > 3 ? Integer.parseInt(a[3]) : 9;

        if (stage <= 0) {
            return;
        }
        JarFile jf = new JarFile(jarPath);
        if (stage <= 1) {
            return;
        }
        inst.appendToBootstrapClassLoaderSearch(jf);
        if (stage <= 2) {
            return;
        }
        Class<?> driver = Class.forName("com.aitest.si.Driver");
        if (stage <= 3) {
            return;
        }
        if (driver.getClassLoader() != null) {
            throw new IllegalStateException("not bootstrap-loaded");
        }
        if (stage <= 4) {
            return;
        }
        Method start = driver.getMethod("start", String.class, String.class);
        if (stage <= 5) {
            return;
        }
        start.invoke(null, port, secret);
    }
}
```

### 4.3 `Driver.java` — 常驻命令服务器（完整）

阅读时重点看这四处，它们是踩坑后才对的：

| 位置 | 要点 |
|---|---|
| `start()` | 必须 `AccessController.doPrivileged`。权限检查看**整条调用栈**，栈底还有无权限的 Loader 帧，不截断则有效权限被压回去 |
| `eventThreadGroup()` | 服务线程必须建在 **Forms 应用自己的 AppContext** 里（JWS 用 codebase 命名 ThreadGroup，形如 `.../OA_JAVA/-threadGroup`）。建错组则 `Window.getWindows()` 返回空，所有点击报 no component |
| `serve()` | **每连接一线程 + `setSoTimeout`**。单线程 accept 循环一旦被死掉的客户端卡在 `readLine`，后续所有连接永久排队 |
| `glassAt()` / 命中测试 | Oracle EWT 盖了一层 `GlassMouseGrabProvider$Proxy` 全窗口玻璃层。**命中测试要跳过它**（否则永远只命中这层），**派发给 EWT 控件时要发给它**（否则控件收不到）。文本框和按钮自己能处理事件，不受影响——这解释了"按钮好使、下拉框死活不弹" |

```java
package com.aitest.si;

import java.awt.AWTEvent;
import java.awt.Component;
import java.awt.Container;
import java.awt.Frame;
import java.awt.KeyboardFocusManager;
import java.awt.Point;
import java.awt.Window;
import java.awt.event.ItemEvent;
import java.awt.event.KeyEvent;
import java.awt.event.MouseEvent;
import java.awt.event.MouseWheelEvent;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.security.AccessController;
import java.security.PrivilegedAction;

/**
 * Drives the Forms client from inside its own JVM, over a loopback socket.
 *
 * Loaded by the bootstrap class loader, so it runs with a null protection
 * domain and is not subject to the Web Start sandbox that leaves ordinary agent
 * code unable to read the thread list or open a socket.
 *
 * Injected events travel the same dispatch path as a real click or keypress, so
 * Forms cannot tell them apart and every Personalization fires as usual, while
 * nothing enters the operating system input queue - which is what made every
 * earlier approach take over the user's mouse and keyboard.
 *
 * AppContext matters here: Java Web Start runs the UI in its own AppContext and
 * Frame.getFrames() reports only the calling thread's context, so an agent
 * thread sees no frames at all. AppContext is keyed by ThreadGroup, so the
 * command loop runs on a thread created inside the event dispatch thread group.
 *
 * The socket is loopback-only, but any local process could otherwise drive the
 * user's authenticated session, so a caller must present the secret first.
 */
public class Driver {

    private static volatile ServerSocket server;
    private static volatile String secret = "";

    public static void start(String portText, String tokenText) {
        if (server != null) {
            return;
        }
        secret = tokenText == null ? "" : tokenText;
        final int port = Integer.parseInt(portText);
        // A permission check inspects the whole call stack, and the loader frame
        // below this one carries no permissions - so being bootstrap-loaded is
        // not enough on its own. doPrivileged cuts the stack here, where the
        // protection domain is null and therefore unrestricted.
        AccessController.doPrivileged(new PrivilegedAction<Void>() {
            public Void run() {
                Thread t = new Thread(eventThreadGroup(), new Runnable() {
                    public void run() {
                        serve(port);
                    }
                }, "aitest-forms-drive");
                t.setDaemon(true);
                t.start();
                return null;
            }
        });
    }

    private static ThreadGroup eventThreadGroup() {
        // Java Web Start runs the Forms applet in its own AppContext, keyed by
        // ThreadGroup (named after the codebase, e.g. ".../OA_JAVA/-threadGroup").
        // The server thread has to live in that group, or Window.getWindows()
        // and the KeyboardFocusManager see a different, empty context.
        ThreadGroup best = null;
        for (Thread t : Thread.getAllStackTraces().keySet()) {
            ThreadGroup g = t.getThreadGroup();
            if (g == null) continue;
            String gn = g.getName() == null ? "" : g.getName();
            if (gn.contains("OA_JAVA") || gn.contains("threadGroup")) return g;
            if (t.getName().startsWith("AWT-EventQueue") && !gn.startsWith("Plugin") && best == null) best = g;
        }
        if (best != null) return best;
        for (Thread t : Thread.getAllStackTraces().keySet()) {
            if (t.getName().startsWith("AWT-EventQueue")) return t.getThreadGroup();
        }
        return Thread.currentThread().getThreadGroup();
    }

    private static void serve(final int port) {
        AccessController.doPrivileged(new PrivilegedAction<Void>() {
            public Void run() {
                serveImpl(port);
                return null;
            }
        });
    }

    private static void serveImpl(int port) {
        // One thread per client, with a read timeout: a client that dies
        // mid-session used to leave the single accept loop blocked in readLine,
        // so every later connection queued forever.
        try {
            server = new ServerSocket(port, 8, InetAddress.getByName("127.0.0.1"));
            while (true) {
                final Socket s = server.accept();
                Thread t = new Thread(new Runnable() { public void run() { client(s); } }, "aitest-client");
                t.setDaemon(true);
                t.start();
            }
        } catch (Throwable t) {
            server = null;
        }
    }

    private static void client(Socket s) {
        try {
            s.setSoTimeout(120000);
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "UTF-8"));
            PrintWriter out = new PrintWriter(s.getOutputStream(), true);
            String hello = in.readLine();
            if (secret.length() > 0 && !("auth " + secret).equals(hello)) { out.println("ERR auth"); return; }
            out.println("OK auth");
            String line;
            while ((line = in.readLine()) != null) {
                if ("quit".equals(line)) break;
                try { out.println(handle(line)); } catch (Throwable t) { out.println("ERR " + t); }
            }
        } catch (Throwable ignored) {
        } finally {
            try { s.close(); } catch (Throwable ignored) { }
        }
    }

    private static String handle(String line) throws Exception {
        int sp = line.indexOf(' ');
        String cmd = sp < 0 ? line : line.substring(0, sp);
        String rest = sp < 0 ? "" : line.substring(sp + 1);

        if ("ping".equals(cmd)) {
            return "OK " + System.getProperty("java.version")
                    + " sm=" + System.getSecurityManager();
        }
        if ("focus".equals(cmd)) {
            return "OK " + describe(KeyboardFocusManager
                    .getCurrentKeyboardFocusManager().getFocusOwner());
        }
        if ("frames".equals(cmd)) {
            StringBuilder sb = new StringBuilder("OK");
            for (Frame f : Frame.getFrames()) {
                sb.append(" {").append(f.getTitle()).append(" showing=")
                        .append(f.isShowing()).append(" at=")
                        .append(f.getBounds()).append("}");
            }
            return sb.toString();
        }
        if ("at".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            return "OK " + describe(componentAt(Integer.parseInt(p[0]),
                    Integer.parseInt(p[1])));
        }
        if ("click".equals(cmd) || "dclick".equals(cmd)) {
            // Navigator rows are painted inside a lightweight list rather than
            // being components of their own, so they can only be reached by
            // coordinate - and a branch expands, or a leaf opens, on a double
            // click. Keyboard navigation is no help there: clicking a row
            // selects it but leaves the focus on a Forms item, so arrow keys
            // never reach the tree.
            int clicks = "dclick".equals(cmd) ? 2 : 1;
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component c = componentAt(sx, sy);
            if (c == null) {
                return "ERR no component at " + sx + "," + sy;
            }
            Point origin = c.getLocationOnScreen();
            int x = sx - origin.x;
            int y = sy - origin.y;
            long t = System.currentTimeMillis();
            for (int n = 1; n <= clicks; n++) {
                dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_PRESSED, t, 0,
                        x, y, n, false, MouseEvent.BUTTON1));
                dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_RELEASED, t + 8,
                        0, x, y, n, false, MouseEvent.BUTTON1));
                dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_CLICKED, t + 16,
                        0, x, y, n, false, MouseEvent.BUTTON1));
                t += 40;
            }
            Thread.sleep(200);
            return "OK clicked x" + clicks + " " + describe(c) + " focus="
                    + describe(KeyboardFocusManager
                            .getCurrentKeyboardFocusManager().getFocusOwner());
        }
        if ("findn".equals(cmd) || "clickn".equals(cmd)
                || "dclickn".equals(cmd)) {
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR no component named " + p[0] + " near " + p[1]
                        + "," + p[2];
            }
            if ("findn".equals(cmd)) {
                return "OK " + describe(c);
            }
            return clickComponent(c, "dclickn".equals(cmd) ? 2 : 1);
        }
        if ("wheel".equals(cmd)) {
            // The Navigator tree lives in a scrolling list and its Expand
            // button opens the whole tree, which pushes the wanted row out of
            // view; a wheel event scrolls it back into range the way a person
            // would, without any of it reaching the input queue.
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            int notches = Integer.parseInt(p[2]);
            Component c = componentAt(sx, sy);
            if (c == null) {
                return "ERR no component at " + sx + "," + sy;
            }
            Point o = c.getLocationOnScreen();
            long t = System.currentTimeMillis();
            dispatch(c, new MouseWheelEvent(c, MouseEvent.MOUSE_WHEEL, t, 0,
                    sx - o.x, sy - o.y, 0, false,
                    MouseWheelEvent.WHEEL_UNIT_SCROLL, 3, notches));
            Thread.sleep(200);
            return "OK wheel " + notches + " on " + describe(c);
        }
        if ("gclick".equals(cmd) || "gpress".equals(cmd)
                || "grelease".equals(cmd)) {
            // Oracle's toolkit lays a GlassMouseGrabProvider over the window and
            // routes real mouse events through it. Dispatching straight to the
            // target skips that machinery: buttons and fields cope, because they
            // handle their own events, but a poplist never opens its popup.
            // These send to the glass layer instead and let the toolkit route.
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component g = glassAt(sx, sy);
            if (g == null) {
                return "ERR no glass layer at " + sx + "," + sy;
            }
            Point o = g.getLocationOnScreen();
            int x = sx - o.x;
            int y = sy - o.y;
            long t = System.currentTimeMillis();
            if (!"grelease".equals(cmd)) {
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_MOVED, t, 0,
                        x, y, 0, false));
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_PRESSED, t + 4,
                        0, x, y, 1, false, MouseEvent.BUTTON1));
            }
            if (!"gpress".equals(cmd)) {
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_RELEASED,
                        t + 40, 0, x, y, 1, false, MouseEvent.BUTTON1));
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_CLICKED,
                        t + 48, 0, x, y, 1, false, MouseEvent.BUTTON1));
            }
            Thread.sleep(250);
            return "OK " + cmd + " via " + describe(g) + " focus="
                    + describe(KeyboardFocusManager
                            .getCurrentKeyboardFocusManager().getFocusOwner());
        }
        if ("gdrag".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component g = glassAt(sx, sy);
            if (g == null) {
                return "ERR no glass layer";
            }
            Point o = g.getLocationOnScreen();
            long t = System.currentTimeMillis();
            dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_DRAGGED, t,
                    MouseEvent.BUTTON1_DOWN_MASK, sx - o.x, sy - o.y, 0,
                    false));
            Thread.sleep(120);
            return "OK dragged to " + sx + "," + sy;
        }
        if ("xwindows".equals(cmd)) {
            return allContexts("list", 0, 0);
        }
        if ("xat".equals(cmd) || "xgclick".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            return allContexts("xat".equals(cmd) ? "at" : "gclick", Integer.parseInt(p[0]), Integer.parseInt(p[1]));
        }
        if ("windows".equals(cmd)) {
            StringBuilder sb = new StringBuilder("OK ");
            for (Window x : Window.getWindows()) {
                sb.append("{").append(x.getClass().getSimpleName())
                  .append(" showing=").append(x.isShowing());
                if (x.isShowing()) {
                    sb.append(" at=").append(x.getLocationOnScreen())
                      .append(" size=").append(x.getWidth()).append("x")
                      .append(x.getHeight())
                      .append(" kids=").append(x.getComponentCount());
                }
                sb.append("} ");
            }
            return sb.toString();
        }
        if ("hold".equals(cmd)) {
            // A poplist opens on press and commits on release; pressing and
            // releasing at the same point opens the popup and closes it again,
            // which is why every programmatic selection was ignored. This holds
            // the button down so the popup can be inspected.
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            long t = System.currentTimeMillis();
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_PRESSED, t, 0,
                    c.getWidth() / 2, c.getHeight() / 2, 1, false,
                    MouseEvent.BUTTON1));
            Thread.sleep(400);
            return handle("windows");
        }
        if ("release".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component c = componentAt(sx, sy);
            if (c == null) {
                return "ERR no component at " + sx + "," + sy;
            }
            Point o = c.getLocationOnScreen();
            long t = System.currentTimeMillis();
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_RELEASED, t, 0,
                    sx - o.x, sy - o.y, 1, false, MouseEvent.BUTTON1));
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_CLICKED, t + 8, 0,
                    sx - o.x, sy - o.y, 1, false, MouseEvent.BUTTON1));
            Thread.sleep(250);
            return "OK released on " + describe(c);
        }
        if ("toggle".equals(cmd)) {
            // Same shape as the poplist: neither a click nor the space key
            // ticks a Forms check box, because Forms keeps its own item value
            // and only updates it when the selection event arrives.
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            boolean want = p.length < 4 || !"false".equals(p[3]);
            StringBuilder tried = new StringBuilder();
            for (String setter : new String[]{"setState", "setSelected"}) {
                try {
                    c.getClass().getMethod(setter, boolean.class)
                            .invoke(c, want);
                    Thread.sleep(150);
                    if (c instanceof java.awt.ItemSelectable) {
                        dispatch(c, new ItemEvent((java.awt.ItemSelectable) c,
                                ItemEvent.ITEM_STATE_CHANGED, c,
                                want ? ItemEvent.SELECTED
                                     : ItemEvent.DESELECTED));
                    }
                    Thread.sleep(250);
                    return "OK " + setter + "(" + want + "); "
                            + handle("read " + p[0] + "|" + p[1] + "|" + p[2]);
                } catch (Throwable t) {
                    tried.append(setter).append(":").append(t).append(" ");
                }
            }
            return "ERR " + tried;
        }
        if ("cookies".equals(cmd)) {
            // The Forms applet is the one thing on this machine that is already
            // authenticated through SSO and that we can reach without touching
            // the user's browser or input. Its cookie store carries the EBS
            // session, so HTTP work can be done from in here.
            StringBuilder sb = new StringBuilder("OK ");
            java.net.CookieHandler h = java.net.CookieHandler.getDefault();
            sb.append("handler=").append(h == null ? "null" : h.getClass().getName());
            if (h instanceof java.net.CookieManager) {
                for (java.net.HttpCookie c : ((java.net.CookieManager) h)
                        .getCookieStore().getCookies()) {
                    sb.append(" {").append(c.getDomain()).append(" ")
                      .append(c.getName()).append("=")
                      .append(c.getValue().length() > 12
                              ? c.getValue().substring(0, 12) + "..." : c.getValue())
                      .append("}");
                }
            }
            return sb.toString();
        }
        if ("http".equals(cmd)) {
            // http <METHOD> <url> [body]  - performed from inside the JVM so it
            // inherits the applet's proxy settings and any cookies it holds.
            String[] p = rest.trim().split("[ ]+", 3);
            String method = p[0];
            String url = p[1];
            String body = p.length > 2 ? p[2] : null;
            java.net.HttpURLConnection con = (java.net.HttpURLConnection)
                    new java.net.URL(url).openConnection();
            con.setInstanceFollowRedirects(false);
            con.setRequestMethod(method);
            con.setConnectTimeout(20000);
            con.setReadTimeout(30000);
            if (body != null) {
                con.setDoOutput(true);
                con.setRequestProperty("Content-Type",
                        "application/x-www-form-urlencoded");
                byte[] b = body.getBytes("UTF-8");
                con.getOutputStream().write(b);
            }
            int code = con.getResponseCode();
            StringBuilder sb = new StringBuilder("OK status=" + code);
            String loc = con.getHeaderField("Location");
            if (loc != null) {
                sb.append(" location=").append(loc);
            }
            java.io.InputStream in = code >= 400 ? con.getErrorStream()
                    : con.getInputStream();
            if (in != null) {
                byte[] buf = new byte[4000];
                int n = in.read(buf);
                sb.append(" body=").append(n > 0 ? new String(buf, 0, n, "UTF-8")
                        .replace('\n', ' ').replace('\r', ' ') : "");
            }
            return sb.toString();
        }
        if ("read".equals(cmd)) {
            // The Access Bridge reports no value for a Forms poplist, so a
            // chosen option cannot be verified through it - and choosing blind
            // is not acceptable when neighbouring actions change data. These
            // getters are what the component itself exposes.
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            StringBuilder sb = new StringBuilder("OK ");
            for (String g : new String[]{"getValue", "getSelectedItem",
                    "getText", "getSelectedIndex", "getItemCount",
                    "getState", "isSelected"}) {
                try {
                    Object v = c.getClass().getMethod(g).invoke(c);
                    sb.append(g).append("=").append(v).append(" ");
                } catch (Throwable ignored) {
                    // not every component offers every getter
                }
            }
            return sb.toString();
        }
        if ("items".equals(cmd)) {
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            StringBuilder sb = new StringBuilder("OK ");
            try {
                int n = (Integer) c.getClass().getMethod("getItemCount")
                        .invoke(c);
                for (int i = 0; i < n && i < 60; i++) {
                    Object v = c.getClass().getMethod("getItem", int.class)
                            .invoke(c, i);
                    sb.append("[").append(i).append("]").append(v).append(" ");
                }
            } catch (Throwable t) {
                return "ERR " + t;
            }
            return sb.toString();
        }
        if ("pick".equals(cmd)) {
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            int idx = Integer.parseInt(p[3]);
            // VPopList is Choice-like: it exposes select(int), not
            // setSelectedIndex. Several setters are tried so the caller does
            // not have to know which toolkit class is behind a given item.
            StringBuilder tried = new StringBuilder();
            for (String setter : new String[]{"select", "setSelectedIndex"}) {
                try {
                    c.getClass().getMethod(setter, int.class).invoke(c, idx);
                    Thread.sleep(200);
                    // Setting the widget is not enough: Forms keeps its own
                    // item value and only updates it when the selection event
                    // arrives, which is why Go kept running with no action.
                    Object item = null;
                    try {
                        item = c.getClass().getMethod("getItem", int.class)
                                .invoke(c, idx);
                    } catch (Throwable ignored) {
                        item = String.valueOf(idx);
                    }
                    if (c instanceof java.awt.ItemSelectable) {
                        dispatch(c, new ItemEvent((java.awt.ItemSelectable) c,
                                ItemEvent.ITEM_STATE_CHANGED, item,
                                ItemEvent.SELECTED));
                    }
                    Thread.sleep(300);
                    return "OK " + setter + "(" + idx + ") + ItemEvent; "
                            + handle("read " + p[0] + "|" + p[1] + "|" + p[2]);
                } catch (Throwable t) {
                    tried.append(setter).append(":").append(t).append(" ");
                }
            }
            return "ERR " + tried;
        }
        if ("type".equals(cmd)) {
            Component c = KeyboardFocusManager
                    .getCurrentKeyboardFocusManager().getFocusOwner();
            if (c == null) {
                return "ERR nothing has focus";
            }
            for (char ch : rest.toCharArray()) {
                long t = System.currentTimeMillis();
                dispatch(c, new KeyEvent(c, KeyEvent.KEY_PRESSED, t, 0,
                        KeyEvent.VK_UNDEFINED, ch));
                dispatch(c, new KeyEvent(c, KeyEvent.KEY_TYPED, t + 1, 0,
                        KeyEvent.VK_UNDEFINED, ch));
                dispatch(c, new KeyEvent(c, KeyEvent.KEY_RELEASED, t + 2, 0,
                        KeyEvent.VK_UNDEFINED, ch));
                Thread.sleep(12);
            }
            return "OK typed " + rest.length();
        }
        if ("key".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            int code = Integer.parseInt(p[0]);
            int mods = p.length > 1 ? Integer.parseInt(p[1]) : 0;
            Component c = KeyboardFocusManager
                    .getCurrentKeyboardFocusManager().getFocusOwner();
            if (c == null) {
                return "ERR nothing has focus";
            }
            long t = System.currentTimeMillis();
            dispatch(c, new KeyEvent(c, KeyEvent.KEY_PRESSED, t, mods, code,
                    KeyEvent.CHAR_UNDEFINED));
            dispatch(c, new KeyEvent(c, KeyEvent.KEY_RELEASED, t + 4, mods,
                    code, KeyEvent.CHAR_UNDEFINED));
            Thread.sleep(150);
            return "OK key " + code;
        }
        if ("stop".equals(cmd)) {
            ServerSocket s = server;
            server = null;
            if (s != null) {
                s.close();
            }
            return "OK stopping";
        }
        return "ERR unknown command " + cmd;
    }


    /**
     * Locate a component by accessible name and where it sits on screen.
     *
     * Coordinate hit-testing is unreliable here: Forms stacks internal frames
     * inside one AWT frame, so a point over the Find window also falls inside
     * the Order Organizer behind it, and the search kept returning fields from
     * the wrong window. The Access Bridge already tells the caller both the name
     * and the exact screen rectangle, so matching on the pair is exact.
     */
    private static Component byName(String wanted, int sx, int sy) {
        java.util.List<Component> all = new java.util.ArrayList<Component>();
        for (Window w : Window.getWindows()) {
            if (w.isShowing()) {
                collect(w, all);
            }
        }
        Component best = null;
        long bestDist = Long.MAX_VALUE;
        for (Component c : all) {
            if (!c.isShowing()) {
                continue;
            }
            String name = accessibleName(c);
            if (name == null || !name.equals(wanted)) {
                continue;
            }
            Point p;
            try {
                p = c.getLocationOnScreen();
            } catch (Throwable t) {
                continue;
            }
            long dx = p.x - sx;
            long dy = p.y - sy;
            long dist = dx * dx + dy * dy;
            if (dist < bestDist) {
                bestDist = dist;
                best = c;
            }
        }
        return bestDist <= 40 * 40 ? best : null;
    }

    private static void collect(Container parent, java.util.List<Component> out) {
        for (Component c : parent.getComponents()) {
            out.add(c);
            if (c instanceof Container) {
                collect((Container) c, out);
            }
        }
    }

    private static String accessibleName(Component c) {
        try {
            return c.getAccessibleContext() == null ? null
                    : c.getAccessibleContext().getAccessibleName();
        } catch (Throwable t) {
            return null;
        }
    }

    private static String clickComponent(Component c) throws Exception {
        return clickComponent(c, 1);
    }

    /**
     * Click a component, optionally twice.
     *
     * A tree row expands or opens on a double click, which is how the Navigator
     * is meant to be driven - the Expand button only acts on a row the tree
     * itself considers selected, and a single click does not get it there.
     */
    private static String clickComponent(Component c, int clicks)
            throws Exception {
        Point origin = c.getLocationOnScreen();
        int x = c.getWidth() / 2;
        int y = c.getHeight() / 2;
        long t = System.currentTimeMillis();
        for (int n = 1; n <= clicks; n++) {
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_PRESSED, t, 0, x, y,
                    n, false, MouseEvent.BUTTON1));
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_RELEASED, t + 8, 0,
                    x, y, n, false, MouseEvent.BUTTON1));
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_CLICKED, t + 16, 0,
                    x, y, n, false, MouseEvent.BUTTON1));
            t += 40;
        }
        Thread.sleep(200);
        return "OK clicked x" + clicks + " " + describe(c) + " at=" + origin
                + " focus=" + describe(KeyboardFocusManager
                        .getCurrentKeyboardFocusManager().getFocusOwner());
    }


    /** Run a snippet in every AppContext and gather the results. */
    private static String allContexts(final String what, final int sx, final int sy) {
        final StringBuilder out = new StringBuilder("OK");
        try {
            java.util.Set<?> ctxs = (java.util.Set<?>) Class.forName("sun.awt.AppContext")
                    .getMethod("getAppContexts").invoke(null);
            for (final Object ctx : ctxs) {
                ThreadGroup tg = (ThreadGroup) ctx.getClass().getMethod("getThreadGroup").invoke(ctx);
                final StringBuilder part = new StringBuilder();
                Thread t = new Thread(tg, new Runnable() { public void run() {
                    try {
                        if ("list".equals(what)) {
                            for (Window w : Window.getWindows()) {
                                if (!w.isShowing()) continue;
                                String title = (w instanceof Frame) ? ((Frame) w).getTitle()
                                        : (w instanceof java.awt.Dialog) ? ((java.awt.Dialog) w).getTitle() : "";
                                part.append(" {").append(w.getClass().getSimpleName()).append(" '").append(title)
                                    .append("' at=").append(w.getLocationOnScreen()).append(" size=")
                                    .append(w.getWidth()).append("x").append(w.getHeight()).append("}");
                            }
                        } else if ("at".equals(what)) {
                            Component c = componentAt(sx, sy);
                            if (c != null) part.append(" ").append(describe(c));
                        } else if ("gclick".equals(what)) {
                            Component g = glassAt(sx, sy);
                            if (g != null) {
                                Point o = g.getLocationOnScreen(); int x = sx - o.x, y = sy - o.y;
                                long tm = System.currentTimeMillis();
                                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_PRESSED, tm, 0, x, y, 1, false, MouseEvent.BUTTON1));
                                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_RELEASED, tm + 40, 0, x, y, 1, false, MouseEvent.BUTTON1));
                                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_CLICKED, tm + 48, 0, x, y, 1, false, MouseEvent.BUTTON1));
                                part.append(" clicked ").append(describe(g));
                            }
                        }
                    } catch (Throwable e) { part.append(" ERR ").append(e); }
                }}, "aitest-ctx");
                t.start(); t.join(8000);
                if (part.length() > 0) out.append(" [ctx ").append(tg.getName()).append("]").append(part);
            }
        } catch (Throwable e) { return "ERR " + e; }
        return out.toString();
    }

    private static void dispatch(Component c, AWTEvent e) {
        c.dispatchEvent(e);
    }

    /** Topmost component at a point, glass overlays included. */
    private static Component glassAt(int sx, int sy) {
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) {
                continue;
            }
            Point o = w.getLocationOnScreen();
            if (sx < o.x || sy < o.y || sx >= o.x + w.getWidth()
                    || sy >= o.y + w.getHeight()) {
                continue;
            }
            Component best = w;
            Container parent = w;
            int x = sx - o.x;
            int y = sy - o.y;
            for (int depth = 0; depth < 12; depth++) {
                Component hit = null;
                for (Component c : parent.getComponents()) {
                    if (c.isVisible() && c.getBounds().contains(x, y)) {
                        hit = c;
                        break;
                    }
                }
                if (hit == null) {
                    break;
                }
                best = hit;
                x -= hit.getX();
                y -= hit.getY();
                if (!(hit instanceof Container)) {
                    break;
                }
                parent = (Container) hit;
            }
            return best;
        }
        return null;
    }

    private static Component componentAt(int sx, int sy) {
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) {
                continue;
            }
            Point o = w.getLocationOnScreen();
            if (sx < o.x || sy < o.y || sx >= o.x + w.getWidth()
                    || sy >= o.y + w.getHeight()) {
                continue;
            }
            Component c = deepest(w, sx - o.x, sy - o.y);
            if (c != null) {
                return c;
            }
        }
        return null;
    }

    /**
     * Most specific component under a point, ignoring full-window overlays.
     *
     * Oracle's toolkit puts a GlassMouseGrabProvider proxy across the whole
     * window, so taking the first child whose bounds contain the point always
     * stops there and never reaches the field - which made every click land
     * nowhere while the typing went to whatever item Forms already had current.
     * Every matching child is followed instead, and the smallest result wins.
     */
    private static Component deepest(Container parent, int x, int y) {
        Component best = null;
        long bestArea = Long.MAX_VALUE;
        for (Component c : parent.getComponents()) {
            if (!c.isVisible() || !c.getBounds().contains(x, y)) {
                continue;
            }
            if (c.getClass().getName().contains("Glass")) {
                continue;
            }
            Component hit = (c instanceof Container)
                    ? deepest((Container) c, x - c.getX(), y - c.getY()) : c;
            if (hit == null) {
                hit = c;
            }
            long area = (long) hit.getWidth() * hit.getHeight();
            if (area < bestArea) {
                bestArea = area;
                best = hit;
            }
        }
        return best == null ? parent : best;
    }

    private static String describe(Component c) {
        if (c == null) {
            return "null";
        }
        String acc = "";
        try {
            if (c.getAccessibleContext() != null) {
                acc = "/" + c.getAccessibleContext().getAccessibleName();
            }
        } catch (Throwable ignored) {
            acc = "";
        }
        return c.getClass().getName() + acc + c.getBounds();
    }
}
```

### 4.4 `drive.py` — Python 驱动端

```python
"""Forms driver: read through the Access Bridge, act through injected events.

The Access Bridge is a good reader - it names every window, item, value and
state - but it exposes no action at all on a text item, so it could only ever
move the caret once per Forms-driven navigation. The events come from inside the
JVM instead, where a MouseEvent to a component moves focus to it and KeyEvents
fill it, along the same path a real click takes. Nothing enters the operating
system input queue, so the foreground never moves and no keystroke is diverted.
"""
import pathlib
import socket
import sys
import time

# driver/ 位于 forms-mcp/ 之下,bg/jab/server 在其父目录。相对定位,
# 任何机器上解包即可运行 - 不要改回绝对路径。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bg
import jab

VK_TAB, VK_ENTER, VK_END, VK_HOME, VK_DELETE, VK_ESCAPE = 9, 10, 35, 36, 127, 27
VK_DOWN, VK_UP, VK_F11 = 40, 38, 122
SHIFT, CTRL = 64, 128

AGENT = pathlib.Path(__file__).parents[1] / "agent"
SECRET = AGENT / "drive" / "secret.txt"


def _token():
    """Read the agent password.

    Read at call time, not at import: a fresh checkout has no secret yet, and a
    missing file used to break the import itself, so nothing in the package
    could even be inspected before the password existed.
    """
    if not SECRET.exists():
        raise RuntimeError(
            f"未找到 agent 口令文件: {SECRET}\n"
            f"生成方法（每台机器一次，不要复用别人的）:\n"
            f'  powershell -c "[Guid]::NewGuid().ToString(\'N\') | '
            f'Out-File -Encoding ascii -NoNewline \'{SECRET}\'"\n'
            f"附加 agent 时必须用同一个值。")
    return SECRET.read_text(encoding="ascii").strip()


class Forms:
    def __init__(self, port=8159):
        self.sess = bg.Session()
        # A minimized frame reports placeholder coordinates near -32000, and the
        # driver locates components by name plus screen position, so nothing
        # would match. Restoring costs one brief foreground blip per session.
        bg.ensure_awake(self.sess)
        self.s = socket.create_connection(("127.0.0.1", port), timeout=60)
        self.f = self.s.makefile("rw", encoding="utf-8", newline="\n")
        self.f.write(f"auth {_token()}\n")
        self.f.flush()
        if (self.f.readline() or "").strip() != "OK auth":
            raise RuntimeError("driver handshake refused")

    # ---- the injected-event half -------------------------------------------

    def cmd(self, line):
        self.f.write(line + "\n")
        self.f.flush()
        return (self.f.readline() or "").strip()

    def click(self, node):
        return self.cmd(f"clickn {node.name}|{node.x}|{node.y}")

    def dclick(self, node):
        """Double click - how a tree row expands, and how it opens."""
        return self.cmd(f"dclickn {node.name}|{node.x}|{node.y}")

    def key(self, code, mods=0):
        return self.cmd(f"key {code} {mods}" if mods else f"key {code}")

    def type(self, text):
        return self.cmd(f"type {text}")

    def wipe(self):
        """Empty the focused item. Typing appends, so this comes first."""
        self.key(VK_END)
        self.key(VK_HOME, SHIFT)
        self.key(VK_DELETE)

    # ---- the Access Bridge half --------------------------------------------

    def forms(self):
        return self.sess.forms()

    def fields(self, form, editable_only=True):
        return [n for n in self.sess.nodes()
                if n.form == form and n.role == "text" and (n.name or "")
                and (not editable_only or "editable" in (n.states or ""))]

    def field(self, form, name, index=0, editable=True):
        """Locate a field by name. Booked lines are read-only, so clicking a
        line to place the cursor has to be able to target a protected item."""
        pool = self.fields(form, editable_only=editable)
        c = [n for n in pool if n.name == name]
        if not c:
            c = [n for n in pool if name in (n.name or "")]
        if not c:
            raise LookupError(f"no editable field like {name!r} in {form!r}")
        return sorted(c, key=lambda n: (n.y, n.x))[index]

    def value(self, node):
        return next((m.value or "" for m in self.sess.nodes()
                     if (m.form, m.role, m.x, m.y)
                     == (node.form, node.role, node.x, node.y)), "")

    # ---- combined ----------------------------------------------------------

    def set(self, form, name, text, index=0, tab=False, settle=0.35):
        """Click the field, empty it, type, and read the value back."""
        node = self.field(form, name, index)
        got = self.click(node)
        if got.startswith("ERR"):
            return False, got
        self.wipe()
        self.type(text)
        if tab:
            self.key(VK_TAB)
        time.sleep(settle)
        back = self.value(node)
        return back.strip() == text.strip(), back

    def tab(self, form, title, settle=2.0):
        """Select a page tab.

        Tabs are drawn inside the toolkit's own tab bar rather than as separate
        components, so a click by name finds nothing - but the Access Bridge
        exposes them as selectable rows, and selecting one is a navigation Forms
        performs itself. Each channel is used for what it is good at.
        """
        lists = [n for n in self.sess.nodes()
                 if n.form == form and n.role == "page tab list"]
        for tl in lists:
            rows = bg.rows(self.sess, tl)
            hit = next((i for i, role, nm, st in rows if nm == title), None)
            if hit is None:
                continue
            fresh = next(n for n in self.sess.nodes()
                         if n.form == form and n.role == "page tab list"
                         and (n.x, n.y) == (tl.x, tl.y))
            bg.select(fresh, hit, settle=settle)
            return True
        return False

    def choose(self, form, combo_name, option, tries=4):
        """Pick an option from a Forms combo by name.

        bg.combo_set reads the list before it has settled and then reports the
        option missing; the visible window can also be scrolled part-way down,
        so the list is re-opened until the wanted entry actually appears.
        """
        import server as S
        for _ in range(tries):
            combo = next((n for n in self.sess.nodes()
                          if n.form == form and n.role == "combo box"
                          and n.name == combo_name), None)
            if combo is None:
                return False
            S._do_action(combo, "Toggle Drop Down", self.sess.hwnd)
            time.sleep(1.2)
            combo = next((n for n in self.sess.nodes()
                          if n.form == form and n.role == "combo box"
                          and n.name == combo_name), None)
            rows = bg.rows(self.sess, combo)
            hit = next((i for i, r, nm, st in rows if nm == option), None)
            if hit is None:
                hit = next((i for i, r, nm, st in rows if option in nm), None)
            if hit is not None:
                bg.select(combo, hit, settle=1.2)
                return True
            S._do_action(combo, "Toggle Drop Down", self.sess.hwnd)
            time.sleep(0.6)
        return False

    def press(self, form, label, settle=5.0, tries=2):
        return bg.press(self.sess, form, label, settle=settle, tries=tries)

    def pick(self, lov_form, wanted):
        return bg.pick(self.sess, lov_form, wanted)

    def close(self):
        try:
            self.f.write("quit\n")
            self.f.flush()
        finally:
            self.s.close()


def modals(f, answer=("ok", "yes"), known=()):
    """Report and answer any note or decision window that appeared."""
    seen = []
    for _ in range(6):
        ms = [x for x in f.forms()
              if x.lower().startswith(("note", "decision", "error", "caution",
                                       "forms"))
              and x not in known]
        if not ms:
            return seen
        for m in ms:
            texts = [n.value for n in f.sess.nodes()
                     if n.form == m and n.role == "text" and n.value]
            seen.append((m, texts[:2]))
            for a in answer:
                if f.press(m, a, settle=2.0):
                    break
            else:
                return seen
    return seen
```

### 4.5 `flow.py` — 分阶段可续跑编排（模板）

第 9.2 节给的是骨架，这是实际用的版本。**新流程照着这个改，不要从零写** ——
分阶段、状态落盘、每阶段计时这三件事都在里面了。

> **改之前先改第 20 行**：`ORDER = "<你的测试订单号>"`。
> 直接跑会去操作**别人的单据**。换成你自己的，或改成从命令行参数取。

```python
"""State-aware runner for the order-to-cash test.

Each step checks what is already on screen and does only what is missing, so the
run can be repeated after a failure instead of being rebuilt step by step. The
Forms half is driven by injected events; nothing reaches the operating system's
input queue, so the user's mouse and keyboard stay theirs throughout.
"""
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from drive import Forms, modals
import bg
import jab
import nav
import server as S

ORDER = "<你的测试订单号>"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def close_all_but_navigator(f):
    """Forms refuses to open another window while some of these are up."""
    for _ in range(6):
        others = [x for x in f.forms() if not x.startswith("Navigator")]
        if not others:
            return True
        for x in others:
            if f.press(x, "cancel", settle=1.5, tries=1) or \
               f.press(x, "close", settle=1.5, tries=1):
                continue
            fr = next((n for n in f.sess.nodes()
                       if n.role == "internal frame" and n.form == x), None)
            if fr is not None:
                S._do_action(fr, "Close Window", f.sess.hwnd)
                jab.settle(2.0)
        for m in [x for x in f.forms()
                  if x.lower().startswith(("decision", "forms", "note",
                                           "caution", "error"))]:
            for b in ("discard", "no", "ok"):
                if f.press(m, b, settle=1.5, tries=1):
                    break
    return not [x for x in f.forms() if not x.startswith("Navigator")]


def open_order(f):
    """Bring up the Sales Orders window for ORDER, whatever is open now."""
    so = next((x for x in f.forms() if x.startswith("Sales Orders")), None)
    if so and ORDER in so:
        return so
    if not any(x.startswith("Order Organizer") for x in f.forms()):
        close_all_but_navigator(f)
        ok, opened = nav.tree_open(f, "Orders, Returns:Order Organizer")
        log(f"open Order Organizer: {ok} {opened}")
    for x in [n for n in f.forms() if n.startswith("Open Folder")]:
        f.press(x, "cancel", settle=1.5, tries=1)

    find = next((x for x in f.forms() if x.lower().startswith("find")), None)
    if find is None:
        org = next(x for x in f.forms() if x.startswith("Order Organizer"))
        bg.menu(f.sess, "view", "find")
        jab.settle(5.0)
        find = next((x for x in f.forms() if x.lower().startswith("find")), None)
    if find is None:
        return None

    f.press(find, "clear", settle=2.5)
    time.sleep(1.0)
    for _ in range(3):
        ok, back = f.set(find, "Order Number", ORDER, tab=True, settle=1.2)
        log(f"   find order number = {back!r}")
        if ok:
            break
    f.press(find, "find", settle=18.0)
    time.sleep(3)
    modals(f, known=tuple(f.forms()))
    org = next((x for x in f.forms() if x.startswith("Order Organizer")), None)
    if org:
        # "open" alone matches "Open Folder..." too; the order's button has alt-O
        f.press(org, "open alt", settle=22.0)
        time.sleep(3)
    return next((x for x in f.forms() if x.startswith("Sales Orders")), None)


def goto_lines(f, so):
    tab = next((t for t in f.sess.nodes() if t.form == so
                and t.role == "page tab" and t.name == "Line Items"), None)
    if tab is not None:
        f.cmd(f"click {tab.x + tab.w // 2} {tab.y + tab.h // 2}")
        time.sleep(1.5)
    so = next(x for x in f.forms() if x.startswith("Sales Orders"))
    line = next((n for n in f.sess.nodes() if n.form == so and n.role == "text"
                 and "Ordered Item" in (n.name or "")
                 and (n.value or "").strip()), None)
    if line is not None:
        f.click(line)
        time.sleep(0.6)
    return so


if __name__ == "__main__":
    f = Forms()
    t0 = time.time()
    log(f"forms: {f.forms()}")
    so = open_order(f)
    log(f"order window: {so!r}   ({time.time() - t0:.0f}s)")
    if so is None:
        raise SystemExit("could not open the order")
    so = goto_lines(f, so)
    log(f"on the lines: {so!r}")

    log(f"Tools > ATP CHECK -> {bg.menu(f.sess, 'tools', 'atp check')}")
    time.sleep(5)
    log(f"forms: {f.forms()}")
    for x in [n for n in f.forms()
              if not n.startswith(("Navigator", "Order Organizer",
                                   "Sales Orders", "Find"))]:
        print(f"\n[{x}]")
        for nd in f.sess.nodes():
            if nd.form == x and nd.role in ("push button", "text", "combo box",
                                            "check box"):
                val = (nd.value or "")[:22]
                if nd.role == "push button" or val.strip():
                    print(f"   {nd.role:<12} {(nd.name or '')[:30]!r:<32} "
                          f"@({nd.x},{nd.y}) val={val!r}")
    log(f"elapsed {time.time() - t0:.0f}s")
    f.close()
```

### 4.6 `nav.py` — 导航树

```python
"""Navigate the Navigator by clicking tree rows.

bg.go walks the tree through the accessible-selection API and gives up when the
wanted row has not been scrolled into view. The rows are ordinary components
with real coordinates, so clicking one selects it exactly as a person would,
and the Expand and Open buttons do the rest.
"""
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from drive import Forms
import bg
import jab


def rows(f, nav):
    return [n for n in f.sess.nodes()
            if n.form == nav and n.role == "label" and (n.name or "").strip()]


def find_row(f, nav, text):
    """Match on the label text with the expand marker stripped off."""
    for n in rows(f, nav):
        clean = (n.name or "").strip().lstrip("+-").strip()
        if clean == text:
            return n
    for n in rows(f, nav):
        if text in (n.name or ""):
            return n
    return None


def scroll_to(f, nav, text, limit=30):
    """Scroll the tree until the wanted row is visible.

    Expand opens the whole tree, so the child of the branch just expanded is
    usually below the visible window; the list scrolls on a wheel event exactly
    as it would for a person.
    """
    rows_now = rows(f, nav)
    if not rows_now:
        return None
    anchor = rows_now[len(rows_now) // 2]
    for _ in range(limit):
        hit = find_row(f, nav, text)
        if hit is not None:
            return hit
        f.cmd(f"wheel {anchor.x + 60} {anchor.y} -3")
        time.sleep(0.35)
    return find_row(f, nav, text)


def tree_open(f, path, settle=18.0, verbose=True):
    """Select each branch, expand it, scroll to the next part, open the leaf.

    `path` is a list of tree labels, or a "a:b" string for the common case.
    Pass a list whenever a label contains a colon of its own - the custom
    custom functions are often named that way ("XX:Some Inquiry Form"), and
    splitting one of those on ":" looks for a branch that does not exist.
    """
    nav = next(x for x in f.forms() if x.startswith("Navigator"))
    parts = list(path) if isinstance(path, (list, tuple)) else path.split(":")
    before = set(f.forms())

    f.press(nav, "collapse all", settle=2.5)
    time.sleep(1.0)
    for i, part in enumerate(parts):
        row = scroll_to(f, nav, part)
        if row is None:
            visible = [(n.name or "").strip() for n in rows(f, nav)]
            return False, f"could not find {part!r}; visible: {visible}"
        if verbose:
            print(f"   {(row.name or '').strip()!r}")
        # Double click expands just this branch, so its children land right
        # below and stay visible. The Expand button opens the entire tree and
        # pushes them out of view instead.
        f.cmd(f"dclick {row.x + 60} {row.y + row.h // 2}")
        time.sleep(2.0 if i == len(parts) - 1 else 1.2)
        if i < len(parts) - 1 and find_row(f, nav, parts[i + 1]) is None:
            f.cmd(f"click {row.x + 60} {row.y + row.h // 2}")
            time.sleep(0.5)
            f.press(nav, "expand", settle=2.5)
            time.sleep(1.0)
    jab.settle(settle)
    opened = [x for x in f.forms() if x not in before]
    if not opened:
        f.press(nav, "open alt", settle=settle)
        jab.settle(3.0)
        opened = [x for x in f.forms() if x not in before]
    return bool(opened), opened


def describe(f, window):
    print(f"\n[{window}]")
    for nd in f.sess.nodes():
        if nd.form == window and nd.role in ("text", "push button", "combo box",
                                             "check box", "list", "page tab"):
            ed = "ed" if "editable" in (nd.states or "") else "  "
            print(f"   [{ed}] {nd.role:<12} {(nd.name or '')[:34]!r:<36} "
                  f"@({nd.x},{nd.y}) val={(nd.value or '')[:20]!r}")


if __name__ == "__main__":
    f = Forms()
    t0 = time.time()
    ok, res = tree_open(f, sys.argv[1])
    print(f"{ok} {res}   ({time.time() - t0:.0f}s)")
    print(f"forms: {f.forms()}")
    for n in (res if isinstance(res, list) else []):
        describe(f, n)
    f.close()
```

### 4.7 `witness.py` — 不干扰的自证

```python
"""Passive interference witness.

The previous version opened a Notepad and pulled it to the foreground at the
start of every script, so the tool meant to prove "nothing was disturbed" was
itself the thing disturbing the user. Nothing is raised or opened here: it only
records which window holds the foreground and reports when the Forms client
takes it, which is the only interference this driver could actually cause.
"""
import ctypes
from ctypes import wintypes as wt

user32 = ctypes.windll.user32


def describe(h):
    t = ctypes.create_unicode_buffer(200)
    c = ctypes.create_unicode_buffer(120)
    user32.GetWindowTextW(h, t, 200)
    user32.GetClassNameW(h, c, 120)
    safe = t.value[:44].encode("ascii", "replace").decode("ascii")
    return f"{c.value}:{safe}"


def start():
    fg = user32.GetForegroundWindow()
    return {"start_fg": int(fg), "start_desc": describe(fg), "samples": []}


def check(w, step, forms_hwnd=None):
    fg = int(user32.GetForegroundWindow())
    ours = forms_hwnd is not None and fg == forms_hwnd
    w["samples"].append((step, fg, describe(fg), ours))
    if ours:
        print(f"    [{step:<30}] <-- WE TOOK THE FOREGROUND")
    return not ours


def report(w):
    ours = [s[0] for s in w["samples"] if s[3]]
    print(f"\n  samples={len(w['samples'])}  "
          f"we_took_foreground={ours or 'never'}")
    return len(ours), 0


def _find(part):
    res = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        buf = ctypes.create_unicode_buffer(300)
        user32.GetWindowTextW(h, buf, 300)
        if part.lower() in buf.value.lower() and user32.IsWindowVisible(h):
            res.append(int(h))
        return True

    user32.EnumWindows(cb, 0)
    return res
```

### 4.8 `cdp.py` — 网页端（仅在有独立账号时使用，先读 §10）

```python
"""Minimal CDP client for the dedicated debuggable Edge (port 9333).

This browser was launched by us with its own profile, so driving it touches
neither the user's Edge nor the shared input queue. Runtime.evaluate is enough
for reading a page and clicking by selector; navigation goes through Page.navigate.
"""
import json, sys, time, urllib.request
import websocket

PORT = 9333


def page_target(prefer=("<你们的 SSO 域名>", "<EBS 主机名>", "microsoftonline")):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5))
    pages = [t for t in tabs if t.get("type") == "page"]
    for p in pages:
        if any(k in (p.get("url") or "") for k in prefer):
            return p
    return pages[0] if pages else None


class CDP:
    def __init__(self, target=None):
        t = target or page_target()
        if t is None:
            raise RuntimeError("no page target on port %d" % PORT)
        self.t = t
        # Edge rejects a browser-style Origin header on the debug socket; sending none avoids relaunching it with --remote-allow-origins
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def eval(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def navigate(self, url, settle=6.0):
        self.call("Page.navigate", url=url)
        time.sleep(settle)
        return self.eval("location.href")

    def click(self, selector, settle=4.0):
        ok = self.eval(f"(function(){{var e=document.querySelector({json.dumps(selector)});if(!e)return false;e.click();return true;}})()")
        time.sleep(settle)
        return ok

    def close(self):
        self.ws.close()


if __name__ == "__main__":
    c = CDP()
    print("url  :", c.eval("location.href"))
    print("title:", c.eval("document.title"))
    print("links/buttons:")
    print(c.eval("""
      Array.from(document.querySelectorAll('a,button,input[type=submit],input[type=button]'))
        .map(e => (e.tagName+' | '+(e.innerText||e.value||'').trim().slice(0,50)+' | '+(e.href||e.formAction||e.name||'')).slice(0,180))
        .filter(s => s.trim().length > 6).slice(0,40).join('\n')
    """))
    c.close()
```


### 4.9 `smoke.py` — 落地自检脚本

**跑通这个再开始做任务。** 不要自己另写自检 —— 这个脚本把 §13 的三条
（读 / 写 / 不干扰）都做了，失败时给出的是可操作的排查方向，不是一句报错。

```python
# -*- coding: utf-8 -*-
"""落地自检 - 跑通这个再开始做任务。

三条依次验证，任何一条不过就停下来，不要往下做：
  1. 读   - Access Bridge 能看到 Forms 的窗口和字段
  2. 写   - agent 能把值写进一个字段，并读回一致
  3. 不干扰 - 全程前台窗口没被抢走

用法：
    python smoke.py                 # 只跑 1（只读，绝对安全）
    python smoke.py --write <窗口名> <字段名> <值>
                                    # 跑 1+2+3，会真的写一个字段

第 3 条要人配合：脚本跑起来后，你在记事本里持续打字，结束看报告。
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def hr(t):
    print(f"\n{'=' * 60}\n{t}\n{'=' * 60}")


def step1():
    """读：Access Bridge 看得见 Forms 吗。"""
    hr("① 读 - Access Bridge")
    try:
        import jab
    except Exception as e:
        print(f"FAIL  jab 导入失败: {e}")
        return None
    print(f"  DLL: {jab.DLL}")
    print(f"  Python: {64 if sys.maxsize > 2**32 else 32} 位"
          "（必须与 Forms 的 JRE 位数一致）")

    import bg
    # bg.Session() 找不到 Java 窗口时会先轮询 60 秒再抛异常。自检要的是快速
    # 结论，而且这是新手最常撞到的一幕 - 不能让它变成一段裸 traceback。
    try:
        sess = bg.Session(timeout=5)
    except Exception as e:
        forms = []
        print(f"  ({type(e).__name__}: {e})")
    else:
        forms = sess.forms()
    if not forms:
        print("FAIL  没看到任何 Forms 窗口。按这个顺序查：")
        print("      1. Forms 是否已由【人工】登录并处于运行状态？")
        print("         —— 你不负责登录，也不要自己去拉起（手册 D9）")
        print("      2. jabswitch -enable 之后，是否【重启过 Forms】？")
        print("         —— 不重启不生效，这是第二常见原因")
        print(f"      3. 本 Python 是 {64 if sys.maxsize > 2**32 else 32} 位，"
              "与 Forms 的 JRE 位数一致吗？")
        print("         —— 位数不匹配时窗口列表就是空的，不会报错（手册 §2 红框）")
        return None
    print(f"PASS  看到 {len(forms)} 个窗口:")
    for f in forms[:10]:
        print(f"      - {f}")
    nodes = [n for n in sess.nodes() if n.role == "text" and n.name]
    print(f"      共 {len(nodes)} 个文本字段可见")
    return sess


def step2(form, name, value):
    """写：agent 能把值写进字段并读回。"""
    hr("② 写 - agent 事件注入")
    from drive import Forms, VK_F11
    try:
        f = Forms()
    except Exception as e:
        print(f"FAIL  连不上 agent: {e}")
        print("      检查：agent 是否已 attach（见手册 §2.2 步骤⑦）")
        print("            端口 8159 是否被占")
        print("            secret.txt 是否与 attach 时用的值一致")
        return False
    print("PASS  握手成功")
    print(f"  ping -> {f.cmd('ping')}")
    # editable=False：查询块的字段在按 F11 之前是只读的，而放光标并不需要可
    # 编辑（手册 P8）。用 editable=True 找会直接 LookupError，把"字段还没进
    # 查询模式"误报成"字段不存在"。
    try:
        node = f.field(form, name, editable=False)
    except LookupError as e:
        print(f"FAIL  找不到字段: {e}")
        print("      字段名会随上下文变化，用包含匹配（手册 P7）")
        print("      也确认窗口名对得上：", f.forms())
        return False
    states = next((m.states for m in f.sess.nodes()
                   if (m.form, m.x, m.y) == (form, node.x, node.y)), "")
    if "editable" not in (states or ""):
        print(f"  字段当前只读（states={states}）")
        print("  查询块要先按 F11 进入查询模式才可写 —— 正在按 F11")
        f.click(node)
        f.key(VK_F11)
        time.sleep(2.0)
    ok, back = f.set(form, name, value, tab=False)
    if ok:
        print(f"PASS  写入并读回一致: {back!r}")
    else:
        print(f"FAIL  写入后读回是 {back!r}，期望 {value!r}")
        print("      常见原因：同名字段写了A读了B（P6）/ Forms 拒绝了非法值")
    f.close()
    return ok


def main():
    sess = step1()
    if sess is None:
        print("\n第 1 条不过，停止。整套方法的读通道没打通。")
        return 1

    if "--write" not in sys.argv:
        print("\n只跑了第 1 条（只读）。")
        print("要验证写入与不干扰，加参数：")
        print('  python smoke.py --write "<窗口名>" "<字段名>" "<值>"')
        return 0

    i = sys.argv.index("--write")
    try:
        form, name, value = sys.argv[i + 1], sys.argv[i + 2], sys.argv[i + 3]
    except IndexError:
        print("用法: python smoke.py --write <窗口名> <字段名> <值>")
        return 2

    import witness
    w = witness.start()
    hwnd = getattr(sess, "hwnd", None)
    print(f"\n  起始前台窗口: {w['start_desc']}")
    print(">>> 现在请到记事本里持续打字，直到本脚本结束 <<<")
    time.sleep(3)
    # check() 是采样式的 - 单点采样说明不了什么，要在动作前后都取。
    witness.check(w, "before-write", hwnd)
    ok = step2(form, name, value)
    witness.check(w, "after-write", hwnd)

    hr("③ 不干扰 - 被动见证")
    stolen, _ = witness.report(w)
    if stolen:
        print("  FAIL  Forms 窗口取得过前台 - 这就是抢占。")
    else:
        print("  PASS  Forms 窗口全程未取得前台。")
    print("\n人工确认这两条（脚本无法代替你判断）：")
    print("  [ ] 前台窗口全程没被抢走")
    print("  [ ] 你在记事本打的字一个不少")
    print("\n第 3 条不过 = 整套方法不成立，不要继续。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

---

## 5. 协议参考

行文本协议。连接后**第一行必须**是 `auth <口令>`，回 `OK auth`。之后一行命令、一行响应。

| 命令 | 参数 | 作用 | 备注 |
|---|---|---|---|
| `ping` | — | 存活检测 | 回 Java 版本与 SecurityManager |
| `focus` | — | 当前焦点组件 | 回 `null` 表示 JVM 里**根本没有焦点属主**，此时 `type` 会回 `ERR nothing has focus`。注意 JAB 的 `focused` 状态与它可能不一致 —— 以本命令为准 |
| `frames` / `windows` | — | 本 AppContext 的窗口 | |
| `xwindows` | — | **所有 AppContext** 的窗口 | Java 安全提示框在 `Plugin Thread Group`，只有这条看得见 |
| `at` / `xat` | `x y` | 该坐标下是什么组件 | 调试定位用 |
| `click` / `dclick` | `x y` | 坐标点击 / 双击 | 跳过玻璃层，取面积最小的命中 |
| `findn` | `名称\|x\|y` | **只定位不点击**，回该组件的描述 | 先探后动。不确定坐标对不对时先用它，避免误点 |
| `clickn` / `dclickn` | `名称\|x\|y` | 名称 + 屏幕位置精确定位后点击 | **首选**，比纯坐标稳 |
| `gclick` / `xgclick` | `x y` | 经**玻璃层**路由的点击 | EWT 控件（下拉框等）必须用这个 |
| `gpress` / `grelease` / `gdrag` | `x y` | 玻璃层按下 / 抬起 / 拖拽 | |
| `hold` | `名称\|x\|y` | **按下不放**，随后返回窗口列表 | 下拉框专用，见 §6.4。按住期间弹出层才是打开的，可以被读取 |
| `release` | `x y` | 在该坐标抬起并补一次 click | 与 `hold` 配对使用 |
| `wheel` | `x y 格数` | 滚轮 | |
| `key` | `keyCode [mods]` | 按键 | 见下方键码表 |
| `type` | `文本` | 向当前焦点逐字符输入 | **追加**，先清空 |
| `read` | `名称\|x\|y` | 反射读 `getSelectedItem` / `getSelectedIndex` / `getState` | |
| `items` | `名称\|x\|y` | 反射列出下拉框**全部**选项 | JAB 只能看到滚动可见的一段（P5） |
| `pick` | `名称\|x\|y\|索引` | 按索引选中 + 派发 `ItemEvent` | |
| `toggle` | `名称\|x\|y\|true` | `setState` + `ItemEvent` | |
| `cookies` / `http` | — | 会话辅助 | |
| `stop` | — | 停止服务线程 | agent 随 Forms 关闭而消失，一般不用主动停 |
| `quit` | — | 关闭本次连接 | 只断连接，不停 agent。`drive.py` 的 `close()` 发的就是它 |

**键码表**（`java.awt.event.KeyEvent`）：

```
TAB=9  ENTER=10  ESC=27  END=35  HOME=36  UP=38  DOWN=40  DELETE=127  F11=122
修饰位：SHIFT=64  CTRL=128        用法： key 36 64   （Shift+Home）
```

---

## 5.1 自诊断：`probe()` 与 `diagnose()`

**这两个函数存在的意义，是让 AI 不必被人反复告知"哪个表单能录入"。**
所有判据都是机器可读的，所以就该由代码去读。

```python
f.status()            # Forms 状态栏原文（role=label 且 form is None 的节点）
f.probe(window)       # (能否录入, 证据)  —— 写哨兵 → 读回 → 还原
f.diagnose()          # 全会话体检：窗口、状态栏、每个窗口能否录入、下一步做什么
```

`diagnose()` 的典型输出（实测，卡在受保护记录时）：

```
windows : ['Navigator - ...', 'Order Organizer', 'Sales Orders - [New]']
status  : ['FRM-40200: Field is protected against update.', 'Record: 1/1']
writable:
   NO   Sales Orders - [New]
        'Main tab page Customer' refused input - Forms says: FRM-40200: ...
next    : no window accepts input. FRM-40200 means the record is protected -
          reopen the form the way that creates an enterable record.
```

**约定：任何一次意料之外的结果，第一件事是 `f.diagnose()`。**
不要凭 `OK typed N` 就认为写成功，也不要凭静态状态推断可录入性。

#### 换个模块还灵吗 —— 在 Purchase Orders 上实测过

同一套 `diagnose()`，不改一行，在采购模块上的表现：

```
windows : ['Error APP-FND-02904: ORG ID is required.', 'Navigator - ...', 'Purchase Orders - [New]']
writable:  YES  Purchase Orders - [New]   'Type Required...' accepted input
next    : dismiss the modal 'Error APP-FND-02904: ORG ID is required.' first
```

**传感器通用，药方不通用。** 它能跨模块判断"能不能录入"、发现模态窗口、
把 Forms 原话作为原因给出；但"该按哪个按钮才能得到可录入的记录"是每张表单
各自的知识，得靠 `diagnose()` 的 `next` 加上人/AI 当场探索。

> **这次实测直接改出一个 bug。** 第一版 `probe()` 只试窗口里的第一个字段，
> 而 PO 的第一个字段是 **LOV 专用、不接受自由输入**的，于是把一张本来可以
> 录入的表单误判成不可录入（假阴性）。现在的取样规则：
> **优先取 Forms 自己标为 `editable` 的字段，最多试 4 个，全被拒才下结论。**
> —— 只在一个场景上验证过的判据，换个场景就会翻车。

#### 各模块的 `editable` 表现差异很大

| 窗口 | JAB 报的 editable | 说明 |
|---|---|---|
| Sales Orders | **0 / 43** | 一个都没有，可录入时也是 0 |
| Purchase Orders | **30 / 238** | 正常标注 |
| Navigator | 0 / 2 | — |

**同一个 JAB、同一个 JVM，行为却不一致** —— 这就是为什么判据只能是"实际写一次"。

### 状态栏消息怎么读

| 前缀 | 含义 | 该做什么 |
|---|---|---|
| **`FRM-92102`** | **会话已断，一切观测过期** | **停止操作，人工重新登录 Forms**（P37 / §14.1a） |
| `FRM-40200`（**整条记录**全锁） | 职责没绑定 OU | **换职责**，不要找 OU 字段。看窗口标题有没有 `(OU-XX)`（P34） |
| `FRM-40200`（个别字段） | 该字段受保护 | 表单状态问题。用能创建可录入记录的方式重新打开 |
| `FRM-40102: Record must be entered or deleted first.` | 块里有未完成的新记录 | 先把它填完或清掉，再新建 |
| `FRM-` 其它 | Forms 层面的拒绝 | 按消息字面处理，不要动驱动 |
| `APP-` | 应用层提示，通常点名了前置条件 | 例：`APP-ONT-251736: Defaulting will occur after the Operating Unit is specified.` —— 先把 OU 填上 |
| 无消息但写不进 | 焦点或定位问题 | 查 `focus` 命令回的是不是目标组件 |

---

## 5.2 找控件：`locate()` —— 不要假设它是哪一类

**这是本手册最重要的一个函数。** 它存在的理由是一次实测事故：

> 一个 AI 要做 Ship Confirm，认定它是 Actions 下拉项，在四个页签的下拉里
> 查了 **104 项**，然后报告"该功能在当前实例不存在，可能是职责权限受限，
> 建议联系 IT 检查权限"。
>
> 事实：`Ship Confirm` **同时是** Delivery 页签 Actions 下拉的**第 24 项**
> **和**该页签上的一个**按钮**，两者都是 enabled。
> 更糟的是它以为自己扫了四个页签，实际把同一个隐藏 combo 读了四遍（P38），
> 104 次检查里 78 次是重复数据。

```python
hits = f.locate("Ship Confirm", form="Shipping Transactions")
```

实测输出（18 秒）：

```
poplist item (Actions)  'Ship Confirm'  Delivery 页签      索引 24   可用
push button             'Ship Confirm'  Delivery 页签               可用
poplist item (Actions)  'Ship Confirm'  Path by Trip 页签  索引 10   可用
push button             'Ship Confirm alt C'  Path by Trip 页签     可用
```

它做的事：

| | |
|---|---|
| 搜**所有控件类型** | 按钮 / 菜单项 / 页签 / 复选框 / 单选 / **下拉项** |
| 下拉项用**反射**读全量 | 绕开 P5 的"只返回滚动可见一段" |
| **逐个页签切过去重扫** | 绕开 P38 的"叠放画布上读到的是隐藏那个" |
| 返回**怎么调用** | 按钮给 `press`，下拉项给 `gclick → pick|索引 → Enter → Go` |

**规矩：要做一个业务动作，先 `locate()`，不要先假设它是按钮还是下拉项。**
找不到才是"这个功能可能不在本表单上"的证据；
**在一类控件里没找到，只能说明它不在这一类里。**

> 参数 `sweep_tabs=False` 可以只扫当前页签（快，但会漏别的页签上的控件）。
> 默认会切遍所有页签再扫，代价约 15–20 秒 —— 比 104 次白查便宜得多。

---

## 6. 控件驱动手册

EBS 的控件不是标准 Swing，是 Oracle 自研的 EWT + Forms 控件。**同一个"点击"在不同控件上要走不同通道。**
遇到陌生控件先查这张表，不要自己从头试。

### 6.1 文本字段

```
clickn 定位 → key 35 (End) → key 36 64 (Shift+Home) → key 127 (Del) → type 值 → key 9 (Tab)
→ 读回验证
```

- **输入是追加的**，不先清空会拼成垃圾值。
- **LOV 字段必须用 Tab 提交**：Tab 触发 Forms 项目校验，联动字段（客户带地址、物料带单位）才会出来。
- **写完必读回**。Forms 拒绝非法值时界面毫无提示，只有读回才知道。

**写入失败时：调 `f.diagnose()`，不要自己一步步猜。**

```python
r = f.diagnose()
r['status']    # Forms 状态栏原文
r['writable']  # 每个窗口 -> (能否录入, 证据)  ← 实际写哨兵值试出来的
r['next']      # 下一步该干什么
```

它内部按下面的顺序判断，你不必再记：

```
1. 有没有模态窗口挡着（Find/Note/Decision/Caution/Error）→ 有就先关掉
2. 逐个窗口 probe()：写哨兵 → 读回 → 还原    ← 唯一可靠的判据
3. 读 Forms 状态栏，把 FRM-/APP- 消息作为原因给出
```

> **不要用"字段 states 里有没有 `editable`"当判据 —— 这条是错的。**
> 实测：`Open` 打开的受保护表单是 `editable 0/43`，
> 用 `New Order` 正确打开的表单**同样是 `editable 0/43`**。
> 两者从静态状态上看一模一样，只有真写一次才能区分。

> **`OK typed N` 只代表"事件已分发"，不代表"值已写入"。** 两者之间隔着 Forms
> 的接受与否。看到 `OK typed 6` 就以为写进去了，是最容易走错的一步。

### 6.2 按钮

JAB 的 `doAccessibleActions("Click")`，按名称前缀匹配。最稳。
**前缀歧义**：`"open"` 会同时匹配 `Open Folder...`，用更长的前缀带助记符，如 `"open alt"`（P11）。

### 6.3 菜单

```
JAB 找 menu 节点 → doAccessibleActions("Toggle Drop Down") → 找 menu item → Click
```

- 一次不一定弹开，**重试直到能读到菜单项**。
- **用完必须收起**，否则挡住后续操作。

### 6.4 下拉框 / poplist —— 四步缺一不可

```
1. gclick 打开        （玻璃层路由，让 Forms 知道这个项被激活）
2. items 读全量        （反射 getItemCount/getItem(i)，不要用 JAB 的可见子项）
3. pick 索引           （VPopList 是 Choice 类，没有 setSelectedIndex，用 select(int)）
                       （随后派发 ItemEvent.ITEM_STATE_CHANGED）
4. key 10 (Enter)      （让 Forms 认账）
```

只做第 3 步：`read` 回来是对的，但 Go / 执行毫无反应 —— Forms 维护自己的项目值，不认被悄悄改掉的控件（P4）。

**为什么必须是"玻璃层点击"而不是普通点击**（Driver.java 里 `hold` 的由来）：

> poplist **按下时打开，松开时提交**。在同一个点按下又松开 = 弹出层开了立刻又关 ——
> 这就是早期所有程序化选择都被忽略的真正原因，而不是"设值没生效"。

如果需要在弹出层**打开的状态下**读它的内容（排查选项对不对），用 `hold` / `release` 拆开这一对：

```
hold 名称|x|y     ← 按下不放，弹出层保持打开，此时才读得到
（读 items / 检查）
release x y       ← 在目标项坐标上抬起，等于选中它
```

日常选择用 §6.4 的四步即可；`hold`/`release` 是排查手段。

### 6.5 复选框

`toggle 名称|x|y|true`（`setState(boolean)` / `setSelected(boolean)` + `ItemEvent`），随后确认状态含 `checked`。
**点击和空格键都只让它获得焦点，不会真的勾上。**

### 6.6 页签 —— 点它的【中心点】

```python
t = next(n for n in sess.nodes()
         if n.form == form and n.role == "page tab" and n.name == "Line Items")
f.cmd(f"click {t.x + t.w // 2} {t.y + t.h // 2}")      # ← 中心，不是原点
```

**这一条实测反复验证过，之前本手册写错过两次，以这版为准：**

| 做法 | 结果 |
|---|---|
| 点页签节点的**原点** `(t.x, t.y)` | ❌ 命中 TabBar 但不切换 |
| `addAccessibleSelectionFromContext`（JAB 选择） | ❌ **返回 True，页签不动** |
| `drive.py` 的 `Forms.tab()`（内部走 JAB 选择） | ❌ 同上，返回 True 但无效 |
| 玻璃层 `gclick` 原点 | ❌ 无效 |
| **点中心 `(t.x + t.w//2, t.y + t.h//2)`** | ✅ **有效** |

页签节点的原点在它的左上角，而 EWT 的 TabBar 靠命中位置判断点了哪一页；
落在原点上会被算到边框/上一页，所以必须取中心。`flow.py` 的 `goto_lines()`
就是这么写的，那是当初跑通订单流程的代码。

切不动时另查：表单里有未提交项目，或整条记录受保护（P14 / P34）—— 那种情况下
页签切不动只是连带症状，先解决记录状态。

### 6.7 导航树

```
Collapse All → 按坐标 dclick 逐级展开目标分支 → dclick 叶子打开
```

- 树行是画在 `LWDataSourceList$Content` 里的标签，**只能按坐标点**。
- **禁止用 Expand 按钮**：它展开整棵树，目标行被挤出可见区，且滚轮对该列表无效（P10）。

### 6.8 LOV 弹窗

读 list 行 → `addAccessibleSelectionFromContext` 选中 → 按 OK。
行多时先用它自带的 Find 框过滤（JAB 可见行同样只是一段）。

### 6.9 表格行

点击行内任意单元格 = 把**光标**放进去。
需要"被选中"的动作（发运、移动订单的按钮）认的是**行首 Select 复选框**，必须另外勾。
两者不等价。

### 6.10 查询模式（F11）—— 做无副作用测试的正确姿势

想验证写入通道但又不想动数据？**进查询模式写查询条件，然后取消。零提交。**

```
1. field(form, name, editable=False)   ← 查询块字段此时是只读的，但放光标不需要可编辑
2. click 该字段
3. key 122 (F11)                        ← Enter Query。字段 states 里会出现 editable
4. wipe + type 值 + 读回验证
5. key 27 (ESC) 取消查询                ← 不要按 Find/Ctrl+F11，那才会真的查
```

**实测状态变化**（在一张查询类表单上测得）：

```
按 F11 前: enabled,focusable,visible,showing
按 F11 后: enabled,focusable,visible,showing,focused,editable,single line
                                                        ^^^^^^^^
```

`smoke.py` 已内置这一步：发现字段只读时自动按 F11 再写。

> **F11 一般是安全的，F6 不一定。** F11 只是进查询模式；
> 而在我们实测的那个实例上，`F6` 绑的是 **Clear Record** —— 它曾经抹掉一张已录入的单据头。
> **键位映射因实例而异，不要假设你那边也一样。**
> **在业务表单上试探功能键是破坏性操作。** 要确认某个键的真实绑定，开 Forms 自己的
> Keys 窗口（通常 Ctrl+K）截图看 —— JAB 读不到它的行，只能截窗口区域放大。
> 不同实例的键位映射可能完全不同，不要假设标准绑定存在。

### 6.11 并发请求

```
View > Requests → Submit a New Request → 名称字段 set+Tab → LOV 选中
→ 参数窗口逐项 set → OK → Submit
```

- **参数必须限定范围**（订单号 / 日期），否则会把整个接口表的数据都跑掉。
- **提交后必须查库确认结果**，不能信 "completed successfully"（P13）。

---

## 7. 故障字典

按**症状**索引。大部分坑的表现都是"看起来成功了但什么都没发生"。

| ID | 症状 | 原因 | 处置 |
|---|---|---|---|
| P38 | 在 Shipping Transactions 里设 Actions 下拉，怎么设都不生效，Go 只报无关的警告 | **页签站错了，而且有两个同名的 Actions。** 这个表单有四个页签（`Lines/LPNs` / `Delivery` / `Path by Stop` / `Path by Trip`），叠放画布上**每个页签各有一个 `Actions` combo 和一个 `Go` 按钮**（P6）。在 `Delivery` 页签上设值，等于设了另一个控件 | 先确认页签：`Auto-create Deliveries` 作用于**交货行**，必须在 **`Lines/LPNs`** 页签上做。切过去（点页签**中心**，§6.6）之后，可见的 `Actions` combo 就只剩一个了 —— **combo 从两个变成一个，本身就是页签切对了的验证** |
| P39 | agent 的 `read` 显示 `getSelectedItem=Auto-create Deliveries`，但 JAB 读这个 combo 的 `value` 是 `None` / 空 | **JAB 对这个 combo 不暴露 value**，它的空值不代表没设进去。有 AI 据此判定"值没写进去"，然后一路往 P4 的方向排查 | **判断 poplist 有没有设上，用 agent 的 `read` 命令（反射），不要用 JAB 的 `value`。**<br>`f.cmd(f"read {name}\|{x}\|{y}")` → 回 `getSelectedItem=... getSelectedIndex=...` 才是真相 |
| **P37** | 出现窗口 `FRM-92102: A network error has occurred. The Forms Client has attempted to reestablish its connection to the Server 5 time(s) without success.` | **Forms 会话已经断了。** 但窗口、字段、下拉框全都还读得到 —— **读到的每一个字都是过期的**。在这种状态下做的任何判断都是错的，而且错得很有说服力（"这个 Action 不存在"、"这个字段是空的"） | **立刻停止一切操作。**<br>唯一解法：**人工重新登录 Forms**，再重跑 `setup.ps1` 附加新 agent。已落库的工作不会丢 —— 用 SQL 确认真实进度，从那里继续。<br>`f.diagnose()` 已能识别这个状态并短路返回，**不要绕过它继续 probe** |
| P1 | 改了 agent 代码，重新附加报 loaded，行为完全没变 | 系统类加载器按路径缓存 jar，类已定义则复用 | **换包名 + 换 jar 文件名**（规则 R2） |
| P2 | 所有点击报 `no component`，`frames` 返回空 | 服务线程建在了错误的 AppContext | 用 `eventThreadGroup()` 选 `OA_JAVA` / `threadGroup` 组 |
| P3 | `xwindows` 能看到 Security Warning，但 `at`/`gclick` 找不到里面的按钮 | 该对话框在 `Plugin Thread Group` 这个**另一个 AppContext** | 用跨上下文命令（每个 AppContext 起一线程）。另：**Run 按钮在勾上"我接受风险"前是禁用的**，且对话框会移动，探测前重读坐标 |
| P4 | 下拉框 `read` 回来是对的，Go 之后毫无反应 | 只设了控件值，Forms 不认 | 玻璃层点击 + 反射设值 + ItemEvent + Enter，四步齐全（6.4） |
| P5 | 下拉框里找不到明明存在的选项（只有 9 条，实际 26 条） | `getVisibleChildren` 只返回**滚动可见的一段** | 反射 `getItemCount` / `getItem(i)` 读全量，按索引选 |
| P6 | 值写进了字段，读回来是空的 | 叠放的页签画布上有**同名字段**（表头一个、行块一个），写了 A 读了 B | 定位和读回都用 `(窗口, 角色, x, y)` 四元组。**不要用名字，更不要用 node id**（id 每次快照都变） |
| P7 | 上一步还能找到的字段名，下一步 LookupError | Forms 的可访问名随上下文变化。物料带出描述后 `Main tab page Qty` → `Qty`；`Ordered Item` ↔ `Main tab page Ordered ItemList of Values` | 用**包含匹配**而非全等；**每步重新读快照** |
| P8 | 订单 Book 之后行字段找不到了 | Book 后行变只读，查找若过滤 `editable` 就会漏 | 放光标不需要可编辑：`field(..., editable=False)` |
| P9 | 坐标全是 -32000 附近的负数，点击全落空 | Forms 窗口被最小化，系统返回占位坐标 | 初始化时 `SW_SHOWNOACTIVATE` 恢复并立刻把前台还回去（`bg.ensure_awake`） |
| P10 | 导航树展开后可见行变成了另一个分支 | Expand 按钮展开**整棵树**，目标被挤出可见区 | Collapse All → 双击目标分支 → 双击叶子 |
| P11 | 想点 Open，弹出的是 Open Folder | 名称前缀匹配歧义 | 用更长前缀：`"open alt"` |
| P12 | 某一步之后不管点什么都没反应，Clear 也失灵 | 某个 LOV 字段留了非法值，Forms 不允许离开校验失败的项目，窗口被锁死 | 注入按键清空该字段（End / Shift+Home / Del）即解锁 |
| P13 | 并发请求 "completed successfully"，数据库毫无变化 | 参数没填全，作业范围为空。**实测**：拣货批次 `organization_id` 为空（没填仓库），程序正常完成、零条释放 | 并发请求一律查库确认。别信提示 |
| P14 | 页签之前能切，现在切不动 | 有未提交项目，Forms 阻止导航 | 先处理当前项 |
| P15 | 剪贴板方案第一次成功，之后 Edit > Paste 一直是灰的 | 64 位下 `GlobalAlloc` / `GlobalLock` / `SetClipboardData` 未声明 `restype`，句柄被截断，剪贴板实际是空的 | **根本不要用剪贴板**，直接注入按键。若必须用，声明 `restype = c_void_p` 并读回校验 |
| P16 | 用户抱怨"老弹出来东西干扰我" | 你的见证 / 日志窗口本身就是干扰源 | witness 必须被动：只记录前台窗口，不创建任何窗口、不置顶 |
| P17 | 点了 Forms 里的某个功能，什么窗口都没开 | 该功能是 **JSP 类型**，开在浏览器里了 | 动手前先查 `fnd_form_functions_vl.type`（`FORM` vs `JSP`），别在 Forms 里干等 |
| P18 | 提交后 evaluate 调用超时（网页端） | 页面正在跳转 | **那是成功的表现**，去数据库确认 |
| P19 | agent 端口连上后无响应，之前一直好用 | 单线程 accept 循环被死掉的客户端卡在 `readLine` | 每连接一线程 + `setSoTimeout(120000)` |
| P20 | `SecurityException: signer information does not match` | agent 类在默认包，与 Oracle 签名 jar 冲突 | 放进自有包名（规则 R1） |
| P21 | 目标字段明明在屏幕上，`field()` 却报 LookupError | 查询块的字段在按 F11 之前**是只读的**，而 `field()` 默认 `editable=True` | 用 `editable=False` 定位，再按 F11（见 §6.10）。把"没进查询模式"误报成"字段不存在"会让人查错方向 |
| P22 | `nav.tree_open("XX:Some Inquiry Form")` 找不到分支 | `tree_open` 按 `:` 拆路径，**而很多实例的自定义功能名自带冒号** | 传列表而非字符串：`tree_open(f, ["XX:Some Inquiry Form"])` |
| P23 | 菜单项读不到（`Close Form` 返回 None），但下拉框看着是开的 | 下拉一次不一定弹开，且快照要在弹开**之后**再取 | 循环重试直到读到菜单项再点（§6.3 已写，容易忘）。实测第 1 次就成 —— 但不加重试的版本前三次全失败 |
| P24 | `n.form.startswith(...)` 抛 `AttributeError: NoneType` | **菜单节点的 `form` 是 `None`**，它们不属于任何窗口 | 遍历节点时一律 `(n.form or "")`，不要假设 form 非空 |
| P29 | `type` 返回 `OK typed N`，字段值却始终为空。焦点查着也对（`focus` 回的就是那个 VTextField） | **Forms 拒绝了这些按键，并把原因写在自己的状态栏上。** 最常见的是 `FRM-40200: Field is protected against update.` —— 记录处于受保护状态，换成真人在那儿敲键盘结果完全一样。这**不是** EDT 问题，**不需要** `SwingUtilities.invokeLater`/`invokeAndWait`，也不用改 `Driver.java` | **写入失败第一件事：读 Forms 状态栏**，`drive.py` 的 `Forms.status()` 一行搞定。状态栏在 JAB 里是 `role=label` 且 **`form is None`** 的节点（属于 frame 不属于任何窗口），极容易被整个漏掉。看到 `FRM-` / `APP-` 开头的消息，那就是答案 —— 去修表单状态，不要修驱动 |
| P30 | 报 `LookupError: no editable field like 'Customer'`，可字段明明就在屏幕上 | `field()` 默认只找 `editable` 的节点，而**有些 Forms 客户端几乎从不把数据块字段标成 `editable`** —— 正常可录入的表单也是 0/43。于是"字段存在"被报成了"不存在"，把人引向查拼写、查窗口名的歧路 | 定位一律用 `editable=False`；能不能写要靠 `f.probe()` 实测。新版 `field()` 已区分"不存在"与"存在但拒绝输入"，并把 Forms 状态栏原文附在报错里 |
| P35 | 界面上行已经填好（物料、数量、单价都在），查库却一行都没有 | **Forms 还没提交。** 表头可能因为取号已经写进去了，但行只存在于表单内存里 | `File > Save`，看到状态栏 `FRM-40400: Transaction complete: N records applied and saved.` 才算落库，然后再查库确认。**"界面上有值"和"数据库有行"是两回事** |
| P36 | LOV 字段打不开值列表（F9 / Shift+Ctrl+F9 都没反应），不知道该填什么 | LOV 快捷键因实例而异，可能一个都不生效 | **别在界面上猜，去查库。** 例：合法订单类型 —— `select ttt.name from apps.oe_order_headers_all h join apps.oe_transaction_types_tl ttt on ttt.transaction_type_id=h.order_type_id and ttt.language='US' where h.org_id=<你的 org_id>` 取历史单据用过的值，直接 `set()` 写进去即可，Forms 会自己校验 |
| P34 | 新建的单据**整条记录**都是 `FRM-40200`，一个字段都写不进；找不到 Operating Unit 字段；页签也切不动 | **职责没有绑定业务实体（OU）。** 这不是表单状态问题，也不是驱动问题 —— 换个职责就好了。通用职责（如 `Order Management Super User`）没有默认 OU，EBS 直接把整条记录锁死；页签切不动只是同一个锁的连带症状（P14） | **看窗口标题**：带括号的业务实体代号（形如 `Sales Orders (OU-XX) - [New]`）就是 OU 已确定；没有括号就是没确定。<br>解法是 **File > Switch Responsibility 换成绑定了 OU 的业务职责**（哪个职责绑了 OU，查 `fnd_responsibility` 与 MO 相关 profile，或问你们的顾问），**不是**去某个页签上找 OU 字段填 —— 那个字段在这种情况下根本不存在 |
| P32 | `probe()` 报某窗口不可录入，可你确信它能录 | 取样取到了**只能从 LOV 选、不接受自由输入**的字段（PO 的第一个字段就是这种），于是单个字段的拒绝被当成了整窗口的结论 | 已修：优先取 `editable` 字段、最多试 4 个。自己写检查时也要遵守 —— **一个字段不能代表一张表单** |
| P33 | 关表单时值列表反复自己弹出来，关不掉 | Forms 退出前会校验未完成的记录，光标回到必填的 LOV 字段，于是 LOV 又弹出来，形成循环 | 先 `Cancel` 关掉 LOV → `Esc` 让光标退出该字段 → 再 `File > Close Form` → 出现 `Forms Close this form?` 时按 **Yes**（丢弃未完成记录） |
| P31 | 想用"窗口里有几个 editable 字段"判断这个表单能不能录入 | **判据无效。** 实测同一个 Sales Orders：`Open` 打开（受保护）是 `editable 0/43`，`New Order` 打开（正确）**也是 `editable 0/43`**。JAB 的 `editable` 在这个 Forms 客户端上基本不反映可录入性 | 用 `f.probe(window)`：写哨兵值 → 读回 → 还原。**能不能写，只有写一次才知道**，静态状态一律不可信 |
| P27 | attach 报 `AgentLoadException: Agent JAR not found or no Agent-Class attribute`，但 jar 和 MANIFEST 经检查完全正确 | **这句是 HotSpot 的通用包装**，`agentmain` 里抛的任何异常、以及 JVM 读不到 jar，都显示成这一句。真实原因被藏了。实测两大来源：① 该包名在此 JVM 已加载过（R2）；② jar 所在目录目标 JVM 读不到（R5） | 换全新包名 **且** 换个目录重试。**不要**据此怀疑 jar 损坏 —— 先 `jar tf` / 读 MANIFEST 确认 jar 没问题，再按 R2、R5 排查。直接用 `setup.ps1`，两者它都自动处理 |
| P28 | 想在 `agentmain` 里写文件打日志做诊断，结果日志文件根本没生成，报错还变成了 `AgentInitializationException` | **沙箱拦 `checkWrite`**。`new FileWriter(...)` 在 `agentmain` 上下文里会抛 SecurityException，于是诊断代码自己成了故障源 | 不要在 agent 里写文件做诊断。要看真实异常就把信息**通过 socket 回传**，或者干脆用 `setup.ps1` 的分目录重试去定位 |
| P26 | `python` 无任何输出，或弹出 Microsoft Store；`pip` 报找不到 | Windows 在 `%LOCALAPPDATA%\Microsoft\WindowsApps\` 放了一个 **0 字节的 python.exe 占位符**，它排在 PATH 前面。机器上可能**根本没装 Python**，也可能装了但没进 PATH | 三条路，按顺序试：① 找现成的 venv —— 本项目源码树里就有 `tools/forms-mcp/.venv/Scripts/python.exe`，直接用它的绝对路径调用（**交付包为减小体积没有带 .venv**）；② `where python` / `Get-ChildItem C:\Python*, $env:LOCALAPPDATA\Programs\Python` 找已装的；③ 都没有就装一个,勾选 Add to PATH。**判据：`(Get-Item <路径>).Length` 是 0 就是占位符** |
| P25 | `new JarFile(path)` 抛 SecurityException，而 `new File(path)` 却成功 | **不是沙箱禁止一切文件访问**。`new File()` 从不做权限检查（只包一个路径字符串），成功说明不了任何事；`new JarFile()` 会 `checkRead(path)`，它失败只说明**调用方对那个具体路径没有读权限** | **先查 R2，再查 R4。** 最常见的根因是**违反了 R2**：Loader 类还是旧 jar 里那个已定义的类，code source 指向旧 jar，而 args 传的是新 jar 路径，于是 `checkRead(新路径)` 被拒。换新包名 + 新 jar 名后自然消失。其次才是手打两遍路径写得不一致。**不要据此推断"沙箱级限制、方案不可行"**：本手册的机制在带 `AWTAppletSecurityManager` 的 JVM 上反复验证通过 |

---

## 8. 死路清单

**以下全部实测不通。不要重复验证，直接跳过。**

| ID | 做法 | 结论 |
|---|---|---|
| D1 | `SendInput` 注入键鼠 | 永远落到当前前台窗口，必然抢占用户输入 |
| D2 | 把 Forms 移到**独立 Windows 桌面**再 SendInput | 桌面隔离成立，但 `SendInput` 只对*正在显示*的桌面生效，返回 0 事件。要显示就得 SwitchDesktop = 占满屏幕 |
| D3 | `PostMessage` 发键盘消息 | 需要焦点归属，窗口非激活时被丢弃 |
| D4 | `PostMessage` 发鼠标消息给 frame 或 SunAwtCanvas 子窗口 | 任何坐标都不移动 Forms 当前项 |
| D5 | JAB `setTextContents` / `selectTextRange` | 空操作，返回 true 但值不变 |
| D6 | JAB `requestFocus` 移动当前项 | 只移动 Java 焦点，Forms 当前项不跟随 |
| D7 | JAB 选择接口切页签 / 点页签**原点**坐标 | 两者都报成功而页签不动。必须点页签的**中心点**（§6.6） |
| D8 | `${user.home}/.java.policy` 给 agent 提权 | **JWS 不认这个策略文件**。用引导类加载器提权（4.2） |
| D9 | 从自动化浏览器拉起 Forms | 带票据的链接能启动 JVM，但卡在 "Starting application..."。让人工开一次 Forms 再附加，最快 |
| D10 | 复制运行中 Edge 的 cookie 库复用会话 | 文件被浏览器独占锁定，无法复制 |
| D11 | 用自动化浏览器登录企业 SSO 门户 | **会把用户本人顶下线**（单设备 SSO）。见第 10 节 |
| D12 | 把 Driver 内联进 Loader，"避开文件读取"以绕过沙箱 | **方向性错误。** `appendToBootstrapClassLoaderSearch` 本身就是获取 AllPermission 的手段，不是障碍。内联之后没有这一步，Driver 的逻辑跑在 Loader 受限的 code source 下；`doPrivileged` 只能截断调用栈，**不能授予 code source 本身没有的权限**。结果是 socket、线程枚举、`Window.getWindows()` 在更靠后的地方全部照样被拦 |

---

## 9. 工作纪律

### 9.1 四步循环（每个操作都走一遍）

```
1. 先查库   —— 只读 SQL 取当前状态，写入日志
2. 报计划   —— "当前值 → 目标值" 列给人看；配置类操作默认等确认
3. 界面执行 —— 每字段写完读回，每按钮按完检查弹窗
4. 再查库   —— 确认落库。界面成功而数据库没变，是最常见的假阳性
```

### 9.2 脚本必须分阶段、可续跑

```python
STAGES = {"order": st_order, "hold": st_hold, "atp": st_atp,
          "delivery": st_delivery, "pick": st_pick, "transact": st_transact,
          "ship": st_ship, "invoice": st_invoice, "receipt": st_receipt}

for name in sys.argv[1:]:
    t0 = time.time()
    STAGES[name](f, st)                  # st 是跨阶段共享的状态字典
    st["times"][name] = round(time.time() - t0, 1)
    save(st)                             # 每阶段落盘，随时可续跑

# 用法： python o2c.py order hold atp
#        某步失败，修完只跑： python o2c.py pick
```

**每阶段计时**，最后给出耗时表 —— 这是唯一能说明"到底快不快"的数据。

### 9.3 卡住的处理

**卡住超过 10 分钟就停下来汇报现状和判断，不要闷头试。**
汇报格式：当前在哪个窗口、哪个字段、试了什么、观察到什么、你的判断是什么。

### 9.4 安全底线

```
- agent 端口只绑 127.0.0.1 且必须带口令。否则本机任何进程都能操纵这个已登录的 EBS 会话。
- 口令用随机串，存本地文件，不写进脚本、不提交代码库。
- agent 不随 Forms 自启，随 Forms 关闭而消失。不要做成常驻服务。
- 数据库账号只给 SELECT —— 既是安全边界，也保证测试有效性（你没有能力绕过界面）。
```

---

## 10. 网页端（OA Framework）与 SSO

EBS 有相当比例功能是网页。**在客户服务、采购这类职责下，网页功能的数量往往超过 Forms 功能。**
先用 §14.10 的第二条查询统计一下你们自己的比例，再决定网页端值不值得投入。

### 10.1 先看这条

> **如果 SSO 是单设备登录：自动化用独立浏览器配置文件登录门户，门户视其为新设备，会把用户本人顶下线。**
> 这种打扰比抢鼠标更隐蔽 —— 抢鼠标当场看得见，顶下线要等用户点开 EBS 才发现。
> **唯一正确解法：为自动化申请独立的 EBS 账号。** 绕行方案（复制 cookie、共用会话）都试过，不成立（D10 / D11）。

**在拿到独立账号之前，不要碰网页端。** 遇到网页功能就停下来说明原因。

### 10.2 有独立账号之后

```bat
msedge --user-data-dir=<独立目录> --remote-debugging-port=9333 --window-position=-32000,-32000
```

- CDP 驱动（Python `websocket-client`），握手必须 `suppress_origin=True`，否则 Edge 返回 403。
- 企业门户首次要求邮件验证码做设备绑定，绑定后该配置文件长期可信。
- 验证码通常是 6 个 `maxlength=1` 输入框：必须用**原生 value setter + 派发 input 事件**填（前端框架的双向绑定不认直接赋值）。
- **提交必须调用页面自己的 `submitForm(...)`**，不要"设值 + 点按钮" —— OA Framework 会把表单刷回空白。

### 10.3 深链技巧

OA 页面可直接深链打开，不必层层点：

```
OA.jsp?OAFunc=<功能短名>&<主键参数>=<值>
```

功能短名与授权关系查 `fnd_form_functions_vl` 和 `fnd_compiled_menu_functions`。
**顺带查出功能类型是 `FORM` 还是 `JSP` —— 动手前先查，避免 P17。**

---

## 11. 验证 SQL 索引

常用核对表，按流程阶段索引：

| 阶段 | 表 | 关键字段 |
|---|---|---|
| 订单头 / 行 | `oe_order_headers_all` / `oe_order_lines_all` | `flow_status_code`, `booked_flag`, `open_flag` |
| 发运明细 | `wsh_delivery_details` | `released_status`, `delivery_detail_id`, `shipped_quantity` |
| 交货 | `wsh_new_deliveries` | `delivery_id`, `status_code` |
| 拣货批次 | `wsh_picking_batches` | **`organization_id`（为空则释放零条，见 P13）** |
| 移动订单 | `mtl_txn_request_headers` / `mtl_txn_request_lines` | `line_status`, `quantity_delivered` |
| 应收接口 | `ra_interface_lines_all` | AutoInvoice 输入 |
| 发票 | `ra_customer_trx_all` / `ar_payment_schedules_all` | `trx_number`, `status`, `amount_due_remaining` |
| 收款 | `ar_cash_receipts_all` / `ar_receivable_applications_all` | `receipt_number`, `amount_applied` |
| 并发请求 | `fnd_concurrent_requests` | `status_code`, `phase_code`, `request_id` |
| 工作流卡点 | `wf_item_activity_statuses` | 卡在哪个活动（如 `PRE-BILLING_ACCEPTANCE`） |
| 会话 | `icx_sessions` | 排查登录 / 会话问题 |
| 功能类型 | `fnd_form_functions_vl` | `type` = `FORM` / `JSP`（见 P17） |

---

## 12. 成本预期

一条完整订单到收款流程的**实测**耗时，全程由驱动端完成、未抢占输入：

| 阶段 | 耗时 |
|---|---|
| 建单 + 表头 + 行 + Book | 179 s |
| 释放 ATP 保留 | 33 s |
| ATP 检查（Refresh + Confirm） | 106 s |
| 创建交货 | 127 s |
| 拣货释放 | 86 s |
| 移动订单过账 | 188 s |
| 发运确认 | 288 s |
| AutoInvoice 开票 | ~300 s |
| 收款录入与核销 | ~300 s |
| **合计** | **约 27 分钟** |

单字段写入约 **4.6 s**（定位 + 清空 + 输入 + 校验 + 读回）。上述耗时含大量保守等待，有优化空间。

> **真正的成本在别处**：以上是**流程跑顺之后**的时间。
> 第一次面对陌生表单，摸清控件脾气要 1–2 小时。这是**一次性成本**，摸清一个固化一个。
> 前几个流程会很慢，之后快得多。**提前把这个预期讲清楚，比事后解释有用。**

---

## 13. 自检清单

搭好之后，做到这三条才算通。**已有现成脚本 `driver/smoke.py`，不要自己另写。**

```bat
cd tools\forms-mcp\driver

:: 只跑第 1 条（纯只读，绝对安全，先跑这个）
python smoke.py

:: 跑全部三条（会真的写一个字段，挑个无害的字段）
python smoke.py --write "<窗口名>" "<字段名>" "<值>"
```

| # | 内容 | 判定 |
|---|---|---|
| 1 | **读** —— 用户不动手，读出所有窗口和字段值 | `smoke.py` 打印 "PASS 看到 N 个窗口" |
| 2 | **写** —— 写入一个字段并读回一致 | `smoke.py --write` 打印 "PASS 写入并读回一致"。**再去数据库确认落库** |
| 3 | **不干扰** —— 用户全程在别处打字 | `we_took_foreground=never`，且**人工确认**前台没被抢、字一个不少 |

第 3 条不通过 = 整套方法不成立，不要继续。
第 3 条脚本只能证明"Forms 没取得前台"，**"打的字一个不少"必须由人确认** —— 脚本代替不了。

#### 实测记录（脱敏）

这套方法在一个重度客制化的 EBS R12 实例上，由驱动端完成过下列动作，
**每一步都用只读 SQL 核对过落库结果**：

```
销售订单：空白 → 填表头（客户/客户 PO/订单类型）→ 录行（物料/数量）
          → Save → Book → 释放保留 → ATP 检查 → 创建交货
          → 拣货释放 → 移动订单过账 → 发运确认（released_status 到 'C'）

采购订单：空白 → 表头（供应商/地点/采购员）→ 录行（物料/数量/单价）
          → Save → 改币种（Currency 窗口）→ Save
```

全程未抢占键盘鼠标，用户在别的窗口正常办公。
单字段写入约 4–6 秒（定位 + 清空 + 输入 + 校验 + 读回）。

其中一段由**另一个 AI 助手**在无人值守的情况下独立完成，
事后用 §11 的方式做数据库核对通过 —— 说明这套东西不依赖某一个特定助手。
## 14. 排查方法：怎么解决手册里没写的问题

> **这一节比故障字典更重要。** 字典是别人踩过的坑；这一节是你自己踩新坑时该怎么走。
> 前面 39 条 P 全部是用下面这套方法产生的 —— 学会方法，你就能自己往字典里加。

### 14.1 七条硬规矩

```
规矩 0  先确认会话还活着。死会话上的一切观测都是过期的，而且看起来完全正常。
规矩 1  控制变量：一次只变一个。
规矩 1.5 动手之前先确认【你在哪条记录上】。不可逆动作尤其如此。
        界面上的查询结果可能是过期的、别人的、上一次的。
规矩 2  卡住十分钟必须【换方向】，不是【同方向更深】。
        但【换方向】的第一选择是自己动手做实验，不是停下来问。
        便宜、可逆、一分钟能做完的实验 —— 先做完再说（见 14.3a）。
规矩 3  已经跑通过的代码是最高权威，先读它，再写新的。
规矩 4  界面上答不了的问题，去数据库问。
规矩 5  "动作已发出" ≠ "状态已改变"，永远以读回为准。
规矩 6  每个结论都要能指出支持它的那次观测；说不出来就是猜的。
```

### 14.1a 规矩 0：先确认会话还活着

**这是所有排查的前提，而且是最隐蔽的一种失效。**

Forms 客户端与服务器断线后会弹出 `FRM-92102`，但**界面不会消失**：窗口还在、
字段还能读、下拉框还能展开 —— 读到的全是断线前的快照。在这种状态下你会得到
非常有说服力的错误结论：

> 实测：有 AI 在死会话上读 Shipping Transactions 的 Actions 下拉，
> 得出"`Auto-create Deliveries` 这个选项在当前实例不存在"，
> 并据此认为手册写错了。实际上会话早就断了。

**这一条现在由代码强制，你不需要记得去查：**

| 时机 | 行为 |
|---|---|
| `Forms(...)` 构造时 | 检测到就直接抛 `DeadSessionError`，根本连不上死会话 |
| `set()` 写入没生效时 | 先查会话，是死的就抛，不再返回"字段拒绝输入"这种误导结论 |
| `field()` 找不到字段时 | 同上 ——"字段不存在"和"会话已死"长得一模一样 |
| `f.diagnose()` | 最先检查，命中就短路返回，不再往下 probe |

正常路径不查（快照有成本，而且写入读回成功本身就证明会话活着），
**只在"出事了"的那一刻查** —— 那正是会被假象骗到的时刻。

```python
try:
    f = drive.Forms(port=8181)
except drive.DeadSessionError as e:
    print(e)      # 请人工重新登录 Forms，然后重跑 setup.ps1
```

设计上刻意选择了**抛异常而不是返回状态**：死会话不是"可以绕过的失败"，
继续往下走只会得到自信、详细、而且错误的结论。

判据（任选其一）：

```python
any('FRM-92102' in w for w in f.forms())          # 窗口标题里
'dead_session' in f.diagnose()                     # diagnose 的短路标志
```

死会话唯一的解法是**人工重新登录 Forms**。已落库的数据不会丢，
用 SQL 确认真实进度再从那里继续。

### 14.2 规矩 1：控制变量 —— 最容易翻车的地方

**反面教材（本手册作者本人犯的）**：排查"agent jar 放哪个目录能加载"时，
第一次做的矩阵是**全部 FAIL**，差点得出"所有目录都不行"的错误结论。

原因：那次矩阵**复用了同一个 jar**，而它的包名在第一次成功后已经加载进 JVM，
之后同包名再加载必然失败（R2）。**变量不止一个，结论就是废的。**

重做时每个目录用**全新包名**，立刻得到干净结果：

```
%USERPROFILE%\.formsdrive      OK          %LOCALAPPDATA%\<新建目录>  FAIL
%USERPROFILE%\<任意新建目录>    OK          含非 ASCII 字符的路径      FAIL
%LOCALAPPDATA%\Temp            OK
```

**最有力的一次实验**是这样设计的：把**同一份 jar（SHA256 相同）**复制到两个目录，
其余全部不变。结果一个 OK 一个 FAIL —— 这就锁死了"是目录问题"，不需要再解释机理。

> 设计实验时先问自己：**这次只变了一个东西吗？**
> 答不上来，就先把实验改到能答上来为止。

### 14.2a 规矩 1.5：动手之前先确认你在哪条记录上

**这是唯一一条"做错了会伤到别人数据"的规矩。**

> **实测**：一个 AI 在 Shipping Transactions 里查询失效、界面反复显示
> **一年多以前的旧数据** —— 界面上显示的是另一张早已完结的单据。
> 它在那条旧交货上执行了 **Ship Confirm** —— 一个不可逆的业务动作，
> 而且对象是别人的测试数据。（这次侥幸没生效，但那是运气，不是设计。）

界面查询给你的记录，可能是：**过期的缓存结果**、**别人的数据**、
**保存的文件夹查询条件带出来的**、**上一次操作留下的**。
**"屏幕上显示着"不等于"这是我要操作的那条"。**

不可逆动作（Book / Ship Confirm / Transact / 提交并发请求 / 删除）之前，
必须先做这个三问，答不上来就不许按：

```
1. 这条记录的主键是多少？          → 从界面读出来，念给自己听
2. 和我任务里的目标一致吗？        → 和任务单上的号码逐位对比
3. 数据库确认过吗？                → ebsql.py 查一次，别信界面
```

代码里就是这样：

```python
ctx = f.value(f.field(ST, 'Context', editable=False))   # 例：'Line - <明细ID>'
assert TARGET_ID in ctx, f'目标记录不对，当前是 {ctx}，停止操作'
```

> **查询结果不对劲的时候，最危险的不是"查不到"，而是"查到了别的"。**
> 查不到你会停下来；查到别的你会接着干。

### 14.3 规矩 2：卡住要横向走，不要纵向钻

有个 AI 遇到 `type` 不生效，判断是 EDT 线程问题，准备改 `Driver.java` 包
`SwingUtilities.invokeAndWait`。它在**同一个假设上越钻越深**。

正确的动作是**横向验证中间环节**，一层层把范围切掉：

```
点击命中目标组件了吗？        -> click 返回里有没有目标组件名
JVM 里真有焦点属主吗？        -> focus 命令。回 null 就是没有
按键发出去了吗？              -> type 回 OK typed N
值变了吗？                    -> 读回
Forms 说什么？                -> f.status()  ← 最容易被跳过，也最常直接给出答案
```

实测走完这五步，答案是状态栏上的 `FRM-40200: Field is protected against update.`
—— 与 EDT 毫无关系。**改一百遍 Driver.java 也不会好。**

> **卡住十分钟的判据不是"我还没想明白"，而是"我做的下一步和上一步在同一条线上"。**
> 同一条线上就停，换一条线。

**"换一条线"最常被做成"在同一条线上走得更远"。** 实测：一个 AI 在下拉框里
没找到 Ship Confirm，它的"换方向"是**换个页签再查下拉框** —— 查了四遍。
真正的换方向是**换控件类型**：它是不是按钮？是不是菜单项？
一次 `f.locate()` 就答完了（§5.2）。

> 穷举得越彻底，越像尽力，也越难发现自己在错的空间里。

### 14.3a 停下来汇报 ≠ 停下来等指令

这是规矩 2 最容易被做歪的地方，而且**做歪的样子看起来很守规矩**。

> **实测**：一个 AI 在 Shipping Transactions 上设 Actions 下拉，反复不生效。
> 它做得相当好 —— 查了 P4/P5、用 `items` 读到全量 26 项、找到索引、
> 完整记录了六次尝试的输出、没有乱改驱动代码、到点就停下来汇报。
> 汇报里它提了三个方向，**其中第一个是对的**：
> "这个 combo 是否需要先物理点击（glass-layer click）才能激活 Forms 的值绑定？"
>
> 它把一个**一分钟就能试完、失败了也没有任何后果**的假设，拿来问人了。
> 而那个假设正是答案 —— 补上玻璃层点击之后交货立刻建成了。

**动手做还是先问，判据是这三条，不是"我有没有把握"：**

```
可逆吗？        —— 失败了能不能退回原状？（设个下拉、点个页签：能。按 Book/Ship：不能）
代价低吗？      —— 一分钟以内、不改代码、不动别人的数据？
范围切得动吗？  —— 不论成功失败，都能排除掉一半可能性？

三条都是 → 【直接做】，然后带着结果汇报
任何一条不是 → 汇报，并说清楚你为什么不敢做
```

**汇报的正确形态是"我试了 X，结果 Y，所以我判断 Z"，不是"我猜是 X，该不该试"。**
前者带来新信息，后者只是把决策推回去 —— 而对方掌握的信息还不如你多。

需要先问的，只有这几类：**不可逆的业务动作**（Book、Ship Confirm、提交并发请求、
删除记录）、**会影响别人的操作**（切职责、关掉用户开着的窗口）、
**要改驱动代码或 agent 的**。除此之外，先做。

> 一句话：**卡住时要换的是"方向"，不是"由谁来想"。**

### 14.4 规矩 3：先读已经跑通的代码

页签切不过去这个问题，本手册**连续写错过两次**（先说"用 JAB 选择"，
又说"点坐标"），而正确答案一直躺在 `flow.py` 的 `goto_lines()` 里：

```python
f.cmd(f"click {tab.x + tab.w // 2} {tab.y + tab.h // 2}")   # 点中心，不是原点
```

那是当初真正跑通订单流程的代码。**新写的代码不如已经跑通的代码可信。**

> 动手写任何新逻辑之前，先在 `flow.py` / `nav.py` / `bg.py` 里搜一遍：
> 这件事有没有人已经做成过？

### 14.5 规矩 4：界面答不了就去查库

`Order Type` 是必填 LOV，而在我们实测的那个实例上，LOV 快捷键（F9、Shift+Ctrl+F9）
全都打不开值列表。**不要在界面上继续试快捷键**，去问数据库历史单据用了什么：

```sql
select ttt.name, count(*)
  from apps.oe_order_headers_all h
  join apps.oe_transaction_types_tl ttt
    on ttt.transaction_type_id = h.order_type_id and ttt.language = 'US'
 where h.org_id = <你的 org_id>
 group by ttt.name order by 2 desc
```

拿到实际用过的订单类型名，直接 `set()` 写进去，Forms 自己会校验。
**一次查询解决一个界面死结。**

#### 你的数据库通道：`tools/ebs-db/ebsql.py`

```bash
python tools/ebs-db/ebsql.py "select order_number, flow_status_code
                                from apps.oe_order_headers_all
                               where order_number = <你的订单号>"
```

只接受 `SELECT` / `WITH`，其它语句一律拒绝；凭据不在交付包里，
由 `db.py` 从 `~/.ebs/connections.json` 读取（**每台机器自己配，不要提交**）：

```json
{"default": {"user": "...", "password": "...", "dsn": "host:port/service"}}
// 键名由你自己定；有多个环境就写多个键，用 --env 选
```

参数：`--env <环境键名>`、`--limit 500`、`--json`、`-f query.sql`。

> **没有这条通道，规矩 4 就是一句空话。** 实测有 AI 在没有数据库的情况下
> 只能拿界面当唯一事实来源，而界面正好显示着过期的查询结果 ——
> 它为一个**已经完成的步骤**挣扎了两百多个操作，因为它无法确认真实状态。
> 开工前先跑一条 `select 1 from dual` 验证通道可用。

同类用法：功能是 FORM 还是 JSP（`fnd_form_functions_vl.type`）、
某职责下有哪些功能、上一张成功单据的每个字段都填了什么。

### 14.6 规矩 5：区分"发出"和"生效"

这套系统里有三层，每层都会独立地骗你：

| 层 | 它说成功的意思 | 不代表 |
|---|---|---|
| agent | `OK typed 6` | 事件已分发。**不代表值变了** |
| Forms 界面 | 字段里显示有值 | 表单内存里有。**不代表落库了** |
| 并发请求 | `completed successfully` | 程序跑完了。**不代表处理了任何数据** |

对应的验证动作分别是：**读回**、**`File > Save` 后查库**、**查库看行数**。

> 实测：行在界面上填得好好的，查 `oe_order_lines_all` 一行都没有 —— 因为没 Save（P35）。
> 实测：拣货释放报 completed successfully，实际释放零条 —— 因为批次没有仓库（P13）。
> 实测：Auto-create Deliveries 之后状态栏报 `ORA-20120: Currency Conversion Rate Not Defined`，
> 看着像失败 —— 查库发现交货已经建好了。**报错也可能是噪音，同样要查库。**

**这条对"成功"和"失败"两个方向都成立**：界面说成功要查，界面说失败也要查。

### 14.7 规矩 6：结论要有观测支撑

每写下一个判断，问自己："**是哪一次运行的哪一行输出让我这么说的？**"

本手册作者用"窗口里 editable 字段数"当可录入判据，写进了文档 —— 然后被实测证伪：

```
Open 打开（受保护）    editable 0 / 43     不能录入
New Order 打开（正确）  editable 0 / 43     也不能录入（缺 OU）
Purchase Orders        editable 30 / 238   能录入
```

**同一个 JAB、同一个 JVM，不同表单行为完全不同。** 那条判据是推测出来的，不是测出来的。
唯一测得出来的判据是 `f.probe()`：写哨兵值 → 读回 → 还原。

> 推测可以用来设计下一次实验，**不能用来下结论**。

### 14.8 反模式清单（实测中真实发生过的）

| 反模式 | 具体表现 | 代价 |
|---|---|---|
| 从报错直接跳到底层机制假设 | `type` 不生效 → 断定 EDT 问题 → 准备改 agent | 方向全错，改了也不会好 |
| 在错误前提下越钻越深 | 认定在通用职责下建单，然后去找不存在的 OU 字段、调页签 API | 卡很久，三个猜想全不相干 |
| 自己写探针程序试探边界 | 写最小 agent 试 `new JarFile()`，得出"沙箱级限制、方案不可行" | 真实原因只是包名重复；而且探针自己被沙箱拦了（P28） |
| 把中间层的成功当最终成功 | 看到 `OK typed 3` 就认为写入完成 | 假阳性，后续全建立在错误状态上 |
| 不读 Forms 状态栏 | 答案就写在屏幕上，从没去读 | 全部三次卡死都能被一次 `f.status()` 化解 |
| 假设控件类型，只在那一类里穷举 | 认定 Ship Confirm 是下拉项，查了 104 项，结论"功能不存在、权限受限" | 结论完全错误，而且会把人引去开权限工单；正确做法是一次 `locate()` |
| 在没核对主键的记录上执行不可逆动作 | 界面显示的是一张早已完结的旧交货，直接按了 Ship Confirm | 差点改坏别人的数据；这次没生效纯属运气 |
| 把可逆实验拿去问人 | 提出"是否需要先玻璃层点击"这个正确假设，却停下来等确认，没自己试 | 一分钟能验证的事拖成一轮往返；而且汇报里没有新信息 |
| 一次改多个变量 | 换 jar 又换目录又换包名 | 结论不可用，得重做 |

### 14.9 卡住时的固定动作

不要即兴发挥，按这个顺序走：

```
1. f.diagnose()                 —— 状态栏、每个窗口能否录入、下一步建议
2. 回看规矩 6：我最近这个判断，有哪次观测支撑？没有就先去测
3. 搜 flow.py / nav.py / bg.py：这件事有没有已经跑通的实现？
4. 查故障字典（§7）：症状对得上哪一条？
5. 设计一个【只变一个变量】的实验，把范围切一半
6. 有假设先自己验：可逆 + 一分钟内 + 能切范围，三条都占就【直接做】（14.3a）
7. 做完还没结论才汇报：当前窗口 / 目标字段 / 试了什么 / 观察到什么 / 你的判断
   —— 汇报里必须有"我试了 X 得到 Y"，不能只有"我猜是 X"
   —— 汇报不是失败，闷头改驱动代码才是；把能自己试的推给别人也是
```

---

### 14.10 为你自己的实例建立一份流程附录

前面九节是**通用方法**，对任何 EBS R12 实例都成立。
但真正让这套东西省时间的，是一份**属于你们实例的流程附录** ——
每条业务流程的步骤、每一道客制化关卡、每个只有你们才有的坑。

这份附录本包不提供（它只能来自你们自己的实例），但做法是固定的。

#### 第一步：先查，再动手

动界面之前，先用 `ebsql.py` 把版图摸清楚。三条查询解决大部分未知：

```sql
-- ① 这个功能是 Forms 还是网页？走错地方会白等（见 P17）
select function_name, user_function_name, type
  from apps.fnd_form_functions_vl
 where user_function_name like '%关键词%';
-- type = 'JSP' 表示它开在浏览器里，Forms 这边不会出现任何窗口

-- ② 这个职责下有哪些功能、各是什么类型
select ff.type, count(*)
  from apps.fnd_compiled_menu_functions cmf
  join apps.fnd_form_functions ff on ff.function_id = cmf.function_id
  join apps.fnd_responsibility r on r.menu_id = cmf.menu_id
 where r.responsibility_name = '<职责名>'
 group by ff.type;

-- ③ 有哪些业务实体，org_id 各是多少
select organization_id, name from apps.hr_operating_units;
```

**上一张成功单据是最好的参考。** 要填的字段不知道填什么，
就去查历史单据那一行实际是什么值 —— 比在界面上猜 LOV 快得多（规矩 4）。

#### 第二步：人工走一遍，记下每一次被拒绝

**客制化校验的提示语是唯一线索，API 层看不到。**
人工走的时候，每被拒绝一次就记一条：

```
在哪一步 / 界面原话 / 当时的记录状态 / 后来怎么过去的
```

这些就是你们实例的"关卡"。别人的文档里不会有。

#### 第三步：固化成"症状 → 原因 → 处置"

照着 §7 故障字典的格式写。**用症状做索引，不是用原因** ——
下次撞上时你看到的是症状，不是原因。

反面例子（写了等于没写）：

> ~~创建交货时要注意 ATP 日期~~

正面例子：

> **症状**：Auto-create Deliveries 报错拒绝，提示里提到日期
> **原因**：（举例）该实例的客制化要求某个日期字段在 N 天以内
> **处置**：先跑 ATP CHECK（Refresh 再 Confirm）刷新该日期，再创建交货

#### 第四步：把流程写到"点哪个控件"的粒度

一条流程写成一张表，每步四列：

| 步 | 做什么 | 具体怎么点 | 怎么验证 |
|---|---|---|---|
| n | 业务动作 | 哪个窗口、哪个页签、哪个控件、什么类型 | 查哪张表的哪个字段变成什么 |

**第三列和第四列是关键。** 只写"做 Ship Confirm"，
助手会在错误的控件类型里穷举（实测有人查了 104 项得出"功能不存在"）；
只写"应该就好了"，助手会把界面提示当成结论。

不知道控件在哪，让助手自己 `f.locate("<功能名>")`（§5.2）——
它会搜遍按钮、菜单项、页签、复选框和所有下拉项，并逐个页签重扫。

#### 第五步：每次踩坑都往回加

**这份附录的价值不在它现在写了什么，在于它是可累加的。**
每摸清一个坑就加一条，下一次（不管是你还是助手）就不必再踩。

一个实测数据供参考：同一个流程，第一次摸索约 1–2 小时，
固化之后重跑约 20–30 分钟，其中大部分是并发程序的等待时间。

> **前几个流程会很慢，之后会快得多。**
> 把这个预期提前讲清楚，比事后解释有用。

---

## 15. 启动配置（人类交付者填写后连同本文件一起交给 AI）

```yaml
环境:
  EBS_URL:        <填写>
  环境:            <你们的测试环境标识>
  Forms_状态:      已由人工登录并运行
  数据库:          只读账号（连接信息另附）
  操作系统:        Windows

agent:
  端口:            8159
  口令文件:        tools/forms-mcp/agent/drive/secret.txt   # 按 §2.2 步骤④ 自行生成

工作约定:
  - 【先读 §14 排查方法】。字典是别人踩过的坑，§14 是你自己踩新坑时怎么走
  - 七条硬规矩：先确认会话活着 / 一次只变一个变量 / 卡住十分钟换方向 / 先读跑通过的代码 /
    界面答不了就查库 / 发出不等于生效 / 结论要有观测支撑
  - 开始录入前先确认职责对不对：窗口标题里应当带 (OU-XX)。没有就先换职责
  - 任何意料之外的结果，第一件事是 f.diagnose()，把它的 next 当作下一步依据
  - 不要凭 "OK typed N" 认为写入成功；一律以读回为准
  - 卡住超过十分钟就停下来汇报，不要闷头改驱动代码

约束确认:
  - 纯界面操作，不写数据库
  - 不抢占鼠标键盘
  - 每步只读 SQL 核对
  - 配置类写入前先报计划等确认
  - 不碰任务范围外的功能

任务:
  <在这里写具体任务。例如：
   在测试环境完成一笔订单到收款全流程测试，
   客户 XXX，物料 XXX，数量 20，
   全程不得抢占输入，每阶段给出耗时与数据库核对结果。>
```

---

