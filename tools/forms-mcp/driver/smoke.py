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
