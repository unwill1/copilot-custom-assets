"""
test_jlink.py — 跑通 MCP 之前,先用这个脚本验证 J-Link 链路。
能跑通这个,MCP server 就一定能跑通(SVD 加载部分已经离线测试过没问题)。

用法:
    set GD32_TARGET=GD32H759IM
    python test_jlink.py

修复历史:
    v2 — 用 register_list() + register_name() 替代 register_index(),
         兼容新版 pylink-square。
"""

import os
import sys
import pylink


TARGET = os.environ.get("GD32_TARGET", "GD32H759IM")
SPEED = int(os.environ.get("GD32_SWD_SPEED_KHZ", "4000"))


def main():
    print(f"== J-Link 自检 ==")
    print(f"目标芯片: {TARGET}")
    print(f"SWD 速率: {SPEED} kHz")
    print()

    jlink = pylink.JLink()

    try:
        print("[1/5] 打开 J-Link...", end=" ", flush=True)
        jlink.open()
        print("OK")

        print("[2/5] 设置 SWD 接口...", end=" ", flush=True)
        jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
        print("OK")

        print(f"[3/5] 连接目标 {TARGET}...", end=" ", flush=True)
        jlink.connect(TARGET, speed=SPEED)
        print("OK")

        print(f"[4/5] 读 Core ID...", end=" ", flush=True)
        cid = jlink.core_id()
        print(f"0x{cid:08X}")

        print("[5/5] 读核心寄存器...")
        jlink.halt()

        # 建寄存器名→索引映射(跨 pylink/J-Link 版本最稳的做法)
        reg_indices = jlink.register_list()
        name_to_idx = {}
        for i in reg_indices:
            try:
                name_to_idx[jlink.register_name(i).upper()] = i
            except Exception:
                pass

        # ARM 别名映射(R15↔PC, R14↔LR, R13↔SP)
        alias_pairs = {
            "PC": "R15", "R15": "PC",
            "LR": "R14", "R14": "LR",
            "SP": "R13", "R13": "SP",
        }

        def find_idx(name):
            name_u = name.upper()
            if name_u in name_to_idx:
                return name_to_idx[name_u]
            alt = alias_pairs.get(name_u)
            if alt and alt in name_to_idx:
                return name_to_idx[alt]
            return None

        for name in ["R0", "R13", "R14", "R15"]:
            idx = find_idx(name)
            if idx is None:
                print(f"        {name} = (未找到)")
                continue
            val = jlink.register_read(idx)
            print(f"        {name} = 0x{val:08X}")

        # GD32H7 SRAM: D1 AXI SRAM 在 0x24000000
        print("\n[额外] 读 D1 AXI SRAM 起始 16 字节 (0x24000000):")
        try:
            data = jlink.memory_read8(0x24000000, 16)
            print("        " + " ".join(f"{b:02X}" for b in data))
        except Exception as e:
            print(f"        读取失败: {e}")
            print(f"        (如果是 GD32F4 而不是 H7, SRAM 在 0x20000000)")

        # GD32H7 RCU.APB2EN 地址(从 SVD 解析出的真实地址)
        print("\n[额外] 读 GD32H7 RCU.APB2EN (0x58024444):")
        try:
            val = jlink.memory_read32(0x58024444, 1)[0]
            print(f"        0x{val:08X}")
        except Exception as e:
            print(f"        读取失败: {e}")

        print("\n✓ 全部通过!可以接 MCP server 了")

    except pylink.errors.JLinkException as e:
        print(f"\n✗ 失败: {e}")
        print("\n常见原因:")
        print("  - Keil 还在 Debug 模式,占用了 J-Link")
        print("  - J-Link 软件版本太老不认识 GD32H7(去 segger.com 装最新版)")
        print("  - 目标芯片名 TARGET 写错了(GD32H759IM / GD32H759II 等)")
        print("  - SWD 线没接好或目标没供电")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ 异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            jlink.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
