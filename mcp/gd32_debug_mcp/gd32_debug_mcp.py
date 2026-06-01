"""
GD32 Debug MCP Server (SVD + ELF-powered)
──────────────────────────────────────────
基于 CMSIS-SVD + ELF 调试信息的 GD32H7xx 调试 MCP server。

修复历史:
        v3 — 新增 ELF/DWARF 支持:符号查找、地址反查、源码行↔地址、
                 set_breakpoint 可配合符号工具使用。可选,通过 load_elf 工具动态加载。
        v2 — 用 register_list() + register_name() 替代 register_index(),
                 兼容新版 pylink-square。

启动前提:
    1. J-Link 探针通过 USB 连到电脑
    2. 目标板正常供电,SWD 接好
    3. Keil 调试会话已关闭(不能占用 J-Link)
    4. pip install pylink-square mcp cmsis-svd pyelftools

环境变量:
    GD32_TARGET           - 目标芯片(默认 GD32H759IM)
    GD32_SWD_SPEED_KHZ    - SWD 速率(默认 4000)
    GD32_JLINK_SERIAL     - J-Link 序列号(多探针时指定)
    GD32_SVD_PATH         - SVD 文件路径(必须设置)
    GD32_ELF_PATH         - ELF/AXF 文件路径(必须设置)
"""

import os
import sys
# Windows 控制台中文 stderr 不乱码
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
import logging
import time
import re
from typing import Optional, Dict, Tuple, List

# ⚠ 重要:所有日志走 stderr,stdout 被 MCP 协议占用
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("gd32-mcp")

try:
    import pylink
except ImportError:
    log.error("pylink-square 未安装。请运行: pip install pylink-square")
    sys.exit(1)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    log.error("mcp 未安装。请运行: pip install mcp")
    sys.exit(1)

try:
    from cmsis_svd.parser import SVDParser
except ImportError:
    log.error("cmsis-svd 未安装。请运行: pip install cmsis-svd")
    sys.exit(1)

# >>> [AI 2026-05-19] 增加可选 pyelftools 依赖,用于 ELF/DWARF 解析
try:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
    _HAS_ELFTOOLS = True
except ImportError:
    log.warning("pyelftools 未安装,ELF 相关工具不可用。如需启用: pip install pyelftools")
    _HAS_ELFTOOLS = False
# <<< [AI]


# ─────────────────────── 配置 ───────────────────────
TARGET_DEVICE = os.environ.get("GD32_TARGET", "GD32H759IM")
SWD_SPEED_KHZ = int(os.environ.get("GD32_SWD_SPEED_KHZ", "4000"))
JLINK_SERIAL = os.environ.get("GD32_JLINK_SERIAL")
SVD_PATH = os.environ.get("GD32_SVD_PATH")
# >>> [AI 2026-05-19] 支持通过环境变量在启动时预加载 ELF
ELF_PATH = os.environ.get("GD32_ELF_PATH")
# <<< [AI]

def _require_existing_file_from_env(env_name: str, path: Optional[str]) -> str:
    if not path:
        log.error(f"{env_name} 未设置")
        log.error(f"请在 Claude Desktop / VS Code 配置的 env 里指定 {env_name}")
        sys.exit(1)

    resolved = os.path.abspath(path)
    if not os.path.isfile(resolved):
        log.error(f"{env_name} 文件不存在: {resolved}")
        log.error(f"请检查 {env_name} 是否指向有效文件")
        sys.exit(1)
    return resolved


SVD_PATH = _require_existing_file_from_env("GD32_SVD_PATH", SVD_PATH)
ELF_PATH = _require_existing_file_from_env("GD32_ELF_PATH", ELF_PATH)


# ─────────────────────── SVD 加载 + 索引 ───────────────────────
log.info(f"加载 SVD: {SVD_PATH}")
_svd_device = SVDParser.for_xml_file(SVD_PATH).get_device()

REG_INDEX: Dict[Tuple[str, str], dict] = {}
PERIPH_INDEX: Dict[str, dict] = {}

for periph in _svd_device.peripherals:
    pname = periph.name.upper()
    base = periph.base_address
    PERIPH_INDEX[pname] = {
        "base": base,
        "description": (periph.description or "").strip(),
        "register_names": [],
    }
    for reg in periph.registers or []:
        rname = reg.name.upper()
        addr = base + reg.address_offset
        fields = []
        for f in reg.fields or []:
            fields.append({
                "name": f.name,
                "bit_offset": f.bit_offset,
                "bit_width": f.bit_width,
                "description": (f.description or "").strip(),
                "access": str(f.access) if f.access else "",
            })
        REG_INDEX[(pname, rname)] = {
            "addr": addr,
            "fields": fields,
            "description": (reg.description or "").strip(),
            "size_bits": reg.size or 32,
            "reset_value": reg.reset_value,
        }
        PERIPH_INDEX[pname]["register_names"].append(rname)

log.info(f"SVD 索引完成: {len(PERIPH_INDEX)} 外设, {len(REG_INDEX)} 寄存器")


# ─────────────────────── MCP 实例 + J-Link ───────────────────────
mcp = FastMCP("gd32-debugger")
jlink = pylink.JLink()

# 寄存器名→索引缓存(连接成功后建一次)
_REG_NAME_TO_IDX: Optional[Dict[str, int]] = None


# >>> [AI 2026-05-18] 规范化 J-Link 核心寄存器名和别名
def _register_name_aliases(name: str) -> List[str]:
    """把 J-Link 返回的寄存器名展开成可查询的别名集合。"""
    name_u = name.strip().upper()
    if not name_u:
        return []

    aliases: List[str] = []

    def add_alias(alias: str):
        if alias and alias not in aliases:
            aliases.append(alias)

    add_alias(name_u)

    for token in re.split(r"[^A-Z0-9]+", name_u):
        if not token:
            continue
        add_alias(token)

        if token == "PC":
            add_alias("R15")
        elif token == "R15":
            add_alias("PC")
        elif token == "LR":
            add_alias("R14")
        elif token == "R14":
            add_alias("LR")
        elif token == "SP":
            add_alias("R13")
        elif token == "R13":
            add_alias("SP")
        elif token == "XPSR":
            add_alias("CPSR")
        elif token == "CPSR":
            add_alias("XPSR")

    return aliases
# <<< [AI]


def _build_reg_name_map():
    """连接成功后建一次名字索引表,跨 pylink/J-Link 版本最稳。"""
    global _REG_NAME_TO_IDX
    _REG_NAME_TO_IDX = {}
    try:
        indices = jlink.register_list()
        for i in indices:
            try:
                raw_name = jlink.register_name(i)
                for alias in _register_name_aliases(raw_name):
                    _REG_NAME_TO_IDX.setdefault(alias, i)
            except Exception:
                pass
        log.info(f"核心寄存器索引完成: {len(_REG_NAME_TO_IDX)} 个")
    except Exception as e:
        log.error(f"构建寄存器索引失败: {e}")


def _open_and_connect():
    if jlink.opened():
        try:
            jlink.close()
        except Exception:
            pass

    log.info(f"打开 J-Link (serial={JLINK_SERIAL or 'auto'})")
    if JLINK_SERIAL:
        jlink.open(serial_no=int(JLINK_SERIAL))
    else:
        jlink.open()
    jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)

    log.info(f"连接目标 {TARGET_DEVICE} @ {SWD_SPEED_KHZ}kHz")
    jlink.connect(TARGET_DEVICE, speed=SWD_SPEED_KHZ)
    _build_reg_name_map()


def _ensure_connected():
    """惰性连接 + 健康检查。"""
    # >>> [AI 2026-05-18] 丢失 J-Link 会话时自动重连,修复 opened() 仍为真但底层连接已失效的场景
    try:
        if not jlink.opened():
            _open_and_connect()
            return

        if not jlink.target_connected():
            _open_and_connect()
            return

        # 某些版本在连接丢失后 opened()/target_connected() 仍可能返回真,
        # 用一次轻量读 Core ID 作为健康检查,失败则强制重连。
        jlink.core_id()
    except Exception as e:
        log.warning(f"检测到 J-Link 会话失效,准备重连: {e}")
        _open_and_connect()
    # <<< [AI]


def _reg_index(name: str) -> int:
    """
    把寄存器名映射成 pylink 索引。支持 R0-R15 / PC / SP / LR / XPSR 等别名。
    跨 pylink/J-Link 版本兼容。
    """
    global _REG_NAME_TO_IDX
    if _REG_NAME_TO_IDX is None:
        _build_reg_name_map()
    if not _REG_NAME_TO_IDX:
        raise RuntimeError("核心寄存器索引未建立,可能 J-Link 未连接")

    for alias in _register_name_aliases(name):
        if alias in _REG_NAME_TO_IDX:
            return _REG_NAME_TO_IDX[alias]

    available = sorted(_REG_NAME_TO_IDX.keys())[:20]
    raise ValueError(f"未知寄存器 '{name}'。可用前 20 个: {available}")


def _decode_register(raw_value: int, fields: List[dict]) -> dict:
    decoded = {}
    for f in fields:
        mask = (1 << f["bit_width"]) - 1
        val = (raw_value >> f["bit_offset"]) & mask
        bit_range = (f"[{f['bit_offset']+f['bit_width']-1}:{f['bit_offset']}]"
                     if f["bit_width"] > 1 else f"[{f['bit_offset']}]")
        decoded[f["name"]] = {
            "bits": bit_range,
            "value": val,
            "hex": f"0x{val:X}" if f["bit_width"] > 4 else str(val),
            "description": f["description"][:100] if f["description"] else "",
        }
    return decoded


# >>> [AI 2026-05-19] 新增 ELF/DWARF 解析能力,支持符号/源码行与地址互查
# ════════════════════════════════════════════════════════════════════
#  ELF/DWARF 模块
# ════════════════════════════════════════════════════════════════════
# 设计要点:
#  - ELF 加载是可选的,任何时候可以通过 load_elf 工具切换
#  - 解析后建多个索引,后续查询是 O(1) 字典查找
#  - Cortex-M 函数符号地址最低位可能是 1(Thumb 标记),实际指令地址要清掉

_ELF_LOADED: bool = False
_ELF_PATH: Optional[str] = None
_SYMBOLS: Dict[str, dict] = {}
_SYMBOLS_BY_ADDR: List[Tuple[int, str]] = []
_LINE_BY_ADDR: List[Tuple[int, str, int]] = []
_FILE_LINE_TO_ADDR: Dict[Tuple[str, int], int] = {}


def _strip_thumb_bit(addr: int) -> int:
    return addr & ~1


def _load_elf_file(path: str) -> dict:
    global _ELF_LOADED, _ELF_PATH
    global _SYMBOLS, _SYMBOLS_BY_ADDR, _LINE_BY_ADDR, _FILE_LINE_TO_ADDR

    if not _HAS_ELFTOOLS:
        return {"error": "pyelftools 未安装,无法加载 ELF。pip install pyelftools"}
    if not os.path.isfile(path):
        return {"error": f"文件不存在: {path}"}

    _ELF_LOADED = False
    _ELF_PATH = None
    _SYMBOLS = {}
    _SYMBOLS_BY_ADDR = []
    _LINE_BY_ADDR = []
    _FILE_LINE_TO_ADDR = {}

    try:
        with open(path, "rb") as file_obj:
            elf = ELFFile(file_obj)

            symtab = elf.get_section_by_name(".symtab")
            if symtab is None or not isinstance(symtab, SymbolTableSection):
                return {"error": "ELF 没有 .symtab 段。可能是 strip 过的 release 版本"}

            for sym in symtab.iter_symbols():
                name = sym.name
                if not name:
                    continue

                info = sym["st_info"]
                symbol_type = info["type"]
                if symbol_type not in ("STT_FUNC", "STT_OBJECT", "STT_NOTYPE"):
                    continue

                raw_addr = sym["st_value"]
                size = sym["st_size"]
                addr = _strip_thumb_bit(raw_addr) if symbol_type == "STT_FUNC" else raw_addr
                if addr == 0 and size == 0:
                    continue

                _SYMBOLS[name] = {
                    "addr": addr,
                    "size": size,
                    "type": symbol_type,
                    "raw_addr_with_thumb_bit": raw_addr,
                }

            _SYMBOLS_BY_ADDR = sorted(
                [(symbol["addr"], name) for name, symbol in _SYMBOLS.items()],
                key=lambda item: item[0]
            )

            line_count = 0
            if elf.has_dwarf_info():
                dwarf = elf.get_dwarf_info()
                for cu in dwarf.iter_CUs():
                    try:
                        line_program = dwarf.line_program_for_CU(cu)
                        if line_program is None:
                            continue

                        file_entries = line_program["file_entry"]
                        for entry in line_program.get_entries():
                            state = entry.state
                            if state is None or state.end_sequence:
                                continue

                            file_idx = state.file
                            if 1 <= file_idx <= len(file_entries):
                                fname_bytes = file_entries[file_idx - 1].name
                                fname = (
                                    fname_bytes.decode("utf-8", errors="replace")
                                    if isinstance(fname_bytes, bytes)
                                    else str(fname_bytes)
                                )
                            else:
                                fname = "?"

                            addr = state.address
                            line = state.line
                            _LINE_BY_ADDR.append((addr, fname, line))
                            key = (fname, line)
                            if key not in _FILE_LINE_TO_ADDR:
                                _FILE_LINE_TO_ADDR[key] = addr
                            line_count += 1
                    except Exception as e:
                        log.warning(f"DWARF CU 解析跳过: {e}")
                        continue

                _LINE_BY_ADDR.sort(key=lambda item: item[0])
            else:
                log.info("ELF 没有 DWARF 调试信息(可能没用 -g 编译)")

        _ELF_LOADED = True
        _ELF_PATH = path
        return {
            "loaded": True,
            "path": path,
            "num_symbols": len(_SYMBOLS),
            "num_functions": sum(1 for symbol in _SYMBOLS.values() if symbol["type"] == "STT_FUNC"),
            "num_objects": sum(1 for symbol in _SYMBOLS.values() if symbol["type"] == "STT_OBJECT"),
            "num_line_entries": line_count,
            "has_dwarf": line_count > 0,
        }

    except Exception as e:
        return {"error": f"加载失败: {type(e).__name__}: {e}"}


def _find_nearest_symbol(addr: int) -> Optional[Tuple[str, int]]:
    if not _SYMBOLS_BY_ADDR:
        return None

    import bisect

    addrs = [item[0] for item in _SYMBOLS_BY_ADDR]
    idx = bisect.bisect_right(addrs, addr) - 1
    if idx < 0:
        return None

    sym_addr, sym_name = _SYMBOLS_BY_ADDR[idx]
    offset = addr - sym_addr
    sym_size = _SYMBOLS[sym_name]["size"]
    if sym_size > 0 and offset >= sym_size:
        return None

    return (sym_name, offset)


def _find_nearest_line(addr: int) -> Optional[Tuple[str, int]]:
    if not _LINE_BY_ADDR:
        return None

    import bisect

    addrs = [item[0] for item in _LINE_BY_ADDR]
    idx = bisect.bisect_right(addrs, addr) - 1
    if idx < 0:
        return None

    _, fname, line = _LINE_BY_ADDR[idx]
    return (fname, line)


if not _HAS_ELFTOOLS:
    log.error("pyelftools 未安装,无法加载 GD32_ELF_PATH。请运行: pip install pyelftools")
    sys.exit(1)

log.info(f"启动时加载 ELF: {ELF_PATH}")
result = _load_elf_file(ELF_PATH)
if "error" in result:
    log.error(f"ELF 加载失败: {result['error']}")
    sys.exit(1)

log.info(f"ELF 索引完成: {result['num_symbols']} 符号, {result['num_line_entries']} 行号条目")
# <<< [AI]


# ════════════════════════════════════════════════════════════════════
#  工具组 1: 目标连接 & 控制
# ════════════════════════════════════════════════════════════════════
@mcp.tool()
def get_target_info() -> dict:
    """
    获取目标连接状态:芯片型号、Core ID、是否停止、PC 位置。
    任何调试 session 开始时建议先调这个工具确认连上了。
    """
    _ensure_connected()
    halted = jlink.halted()
    info = {
        "target": TARGET_DEVICE,
        "swd_speed_khz": SWD_SPEED_KHZ,
        "core_id": f"0x{jlink.core_id():08X}",
        "halted": halted,
        "svd_loaded": _svd_device.name,
        "num_peripherals": len(PERIPH_INDEX),
        "num_registers": len(REG_INDEX),
    }
    if halted:
        info["pc"] = f"0x{jlink.register_read(_reg_index('PC')):08X}"
    return info


@mcp.tool()
def halt() -> dict:
    """停止 CPU 运行,返回停止位置 PC。"""
    _ensure_connected()
    jlink.halt()
    pc = jlink.register_read(_reg_index("PC"))
    return {"halted": True, "pc": f"0x{pc:08X}"}


@mcp.tool()
def resume() -> dict:
    """让 CPU 从当前 PC 继续运行(不复位)。"""
    _ensure_connected()
    jlink.restart()
    return {"resumed": True}


@mcp.tool()
def step() -> dict:
    """单步一条汇编指令。遇到 BL/BLX 会跳进函数。要跨过函数用 step_over。"""
    _ensure_connected()
    if not jlink.halted():
        return {"error": "CPU 正在运行,先 halt"}
    jlink.step()
    pc = jlink.register_read(_reg_index("PC"))
    return {"stepped": True, "pc": f"0x{pc:08X}"}


@mcp.tool()
def step_over() -> dict:
    """步过当前指令。遇到 BL/BLX 不进入函数,在调用之后停下。"""
    _ensure_connected()
    if not jlink.halted():
        return {"error": "CPU 正在运行,先 halt"}

    pc = jlink.register_read(_reg_index("PC"))
    instr_lo = jlink.memory_read16(pc, 1)[0]
    is_blx_reg = (instr_lo & 0xFF80) == 0x4780
    next_pc = None

    try:
        mnemonic = jlink.disassemble_instruction(pc).strip().split(maxsplit=1)[0].upper()
    except Exception:
        mnemonic = ""

    if is_blx_reg:
        next_pc = pc + 2
    elif mnemonic in ("BL", "BLX"):
        next_pc = pc + 4
    else:
        jlink.step()
        return {"stepped_over": False,
                "pc": f"0x{jlink.register_read(_reg_index('PC')):08X}",
                "note": "非调用指令,执行普通 step"}

    bp_id = None
    try:
        bp_id = jlink.breakpoint_set(next_pc, thumb=True)
        jlink.restart()
        for _ in range(200):
            if jlink.halted():
                break
            time.sleep(0.01)
    except Exception as e:
        return {"error": f"step_over 失败: {e}"}
    finally:
        if bp_id is not None:
            try:
                jlink.breakpoint_clear(bp_id)
            except Exception:
                pass

    if not jlink.halted():
        return {
            "error": "step_over 超时: CPU 未在预期位置停止",
            "from_pc": f"0x{pc:08X}",
            "expected_next_pc": f"0x{next_pc:08X}",
        }

    return {
        "stepped_over": True,
        "from_pc": f"0x{pc:08X}",
        "to_pc": f"0x{jlink.register_read(_reg_index('PC')):08X}",
        "halted": jlink.halted(),
    }


@mcp.tool()
def reset(halt_after_reset: bool = True) -> dict:
    """
    复位目标芯片。

    参数:
        halt_after_reset: 复位后是否停在第一条指令(默认 True)
    """
    _ensure_connected()
    jlink.reset(halt=halt_after_reset)
    info = {"reset": True, "halted": halt_after_reset}
    if halt_after_reset:
        info["pc"] = f"0x{jlink.register_read(_reg_index('PC')):08X}"
    return info


# ════════════════════════════════════════════════════════════════════
#  工具组 2: 内存读写
# ════════════════════════════════════════════════════════════════════
@mcp.tool()
def read_memory(addr: int, num_words: int = 1) -> dict:
    """
    读取 32-bit 内存。

    参数:
        addr: 起始地址
        num_words: 读多少个 32-bit 字,默认 1,最大 256

    注意:某些外设寄存器读取有副作用(如 USART RDR 读了清 RFNE)。
    """
    _ensure_connected()
    num_words = max(1, min(num_words, 256))
    try:
        values = jlink.memory_read32(addr, num_words)
    except Exception as e:
        return {"error": f"读取失败: {e}", "addr": f"0x{addr:08X}"}

    return {
        "addr": f"0x{addr:08X}",
        "num_words": num_words,
        "values": [
            {
                "addr": f"0x{addr + i*4:08X}",
                "hex": f"0x{v:08X}",
                "decimal": v,
                "binary": f"0b{v:032b}",
            }
            for i, v in enumerate(values)
        ],
    }


@mcp.tool()
def read_memory_bytes(addr: int, num_bytes: int = 16) -> dict:
    """按字节读内存。适合查看字符串、缓冲区。最大 1024 字节。"""
    _ensure_connected()
    num_bytes = max(1, min(num_bytes, 1024))
    try:
        data = jlink.memory_read8(addr, num_bytes)
    except Exception as e:
        return {"error": f"读取失败: {e}", "addr": f"0x{addr:08X}"}
    return {
        "addr": f"0x{addr:08X}",
        "num_bytes": num_bytes,
        "hex": " ".join(f"{b:02X}" for b in data),
        "ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in data),
        "raw": list(data),
    }


@mcp.tool()
def write_memory(addr: int, value: int) -> dict:
    """
    写入一个 32-bit 值,自动读回验证。

    ⚠ 谨慎使用。建议先 halt 目标。
    """
    _ensure_connected()
    try:
        jlink.memory_write32(addr, [value])
        readback = jlink.memory_read32(addr, 1)[0]
    except Exception as e:
        return {"error": f"写入失败: {e}"}
    return {
        "addr": f"0x{addr:08X}",
        "written": f"0x{value:08X}",
        "readback": f"0x{readback:08X}",
        "match": readback == value,
        "note": "" if readback == value else
                "回读不一致: 可能写到只读位/外设无时钟/写保护未解锁",
    }


# ════════════════════════════════════════════════════════════════════
#  工具组 3: 核心寄存器
# ════════════════════════════════════════════════════════════════════
@mcp.tool()
def read_core_register(name: str) -> dict:
    """
    读取 Cortex-M 核心寄存器。

    参数:
        name: R0-R15 / PC / SP / LR / XPSR / MSP / PSP / PRIMASK / CONTROL / FAULTMASK / BASEPRI
    """
    _ensure_connected()
    try:
        val = jlink.register_read(_reg_index(name))
    except Exception as e:
        return {"error": f"读取失败: {e}", "name": name}
    return {"name": name.upper(), "hex": f"0x{val:08X}", "decimal": val}


@mcp.tool()
def read_all_core_registers() -> dict:
    """一次性返回所有核心寄存器快照。抓 hardfault 现场特别有用。"""
    _ensure_connected()
    result = {}
    for name in ["R0","R1","R2","R3","R4","R5","R6","R7",
                 "R8","R9","R10","R11","R12","R13","R14","R15","XPSR"]:
        try:
            val = jlink.register_read(_reg_index(name))
            result[name] = f"0x{val:08X}"
        except Exception:
            result[name] = "N/A"
    result["SP (R13)"] = result.get("R13", "N/A")
    result["LR (R14)"] = result.get("R14", "N/A")
    result["PC (R15)"] = result.get("R15", "N/A")
    return result


# ════════════════════════════════════════════════════════════════════
#  工具组 4: 断点
# ════════════════════════════════════════════════════════════════════
@mcp.tool()
def set_breakpoint(addr: int, thumb: bool = True) -> dict:
    """
    在指定地址设置硬件断点。Cortex-M7 通常有 8 个硬件断点。

    参数:
        addr: 断点地址
        thumb: 是否 Thumb 模式(Cortex-M 一般是 True)
    """
    _ensure_connected()
    try:
        bp_id = jlink.breakpoint_set(addr, thumb=thumb)
    except Exception as e:
        return {"error": f"下断点失败(可能断点资源耗尽): {e}"}
    return {
        "bp_id": bp_id,
        "addr": f"0x{addr:08X}",
        "total_breakpoints": jlink.num_active_breakpoints(),
    }


@mcp.tool()
def clear_breakpoint(bp_id: int) -> dict:
    """删除指定 ID 的断点。"""
    _ensure_connected()
    try:
        jlink.breakpoint_clear(bp_id)
    except Exception as e:
        return {"error": f"清除断点失败: {e}"}
    return {"cleared": True, "bp_id": bp_id,
            "remaining_breakpoints": jlink.num_active_breakpoints()}


@mcp.tool()
def clear_all_breakpoints() -> dict:
    """删除所有断点。"""
    _ensure_connected()
    count = jlink.num_active_breakpoints()
    jlink.breakpoint_clear_all()
    return {"cleared_count": count}


@mcp.tool()
def wait_for_halt(timeout_seconds: float = 10.0) -> dict:
    """
    等待 CPU 停止(命中断点或异常)。配合 resume + 断点使用。
    """
    _ensure_connected()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if jlink.halted():
            return {
                "halted": True,
                "pc": f"0x{jlink.register_read(_reg_index('PC')):08X}",
                "wait_seconds": round(timeout_seconds - (deadline - time.time()), 2),
            }
        time.sleep(0.02)
    return {
        "halted": False,
        "timeout": True,
        "note": f"等待 {timeout_seconds}s 后 CPU 未停止",
    }


# ════════════════════════════════════════════════════════════════════
#  工具组 5: SVD 智能访问 (本版本核心)
# ════════════════════════════════════════════════════════════════════
@mcp.tool()
def read_register_by_name(peripheral: str, register: str) -> dict:
    """
    按名字读外设寄存器,自动从 SVD 解析所有位域。这是诊断外设状态最高效的工具。

    参数:
        peripheral: 外设名,例如 "USART0", "RCU", "GPIOA", "TIMER0"
        register: 寄存器名,例如 "CTL0", "APB2EN", "BAUD", "STAT"

    返回 raw 值 + 每个位域的值和说明。

    典型用法:
        - 排查 USART 不通: read_register_by_name("USART0", "CTL0")
        - 检查时钟使能: read_register_by_name("RCU", "APB2EN")
        - 看定时器配置: read_register_by_name("TIMER0", "CTL0")

    不知道精确名字时,用 search_register 模糊查找。
    """
    _ensure_connected()
    pname = peripheral.upper()
    rname = register.upper()
    key = (pname, rname)

    if key not in REG_INDEX:
        if pname not in PERIPH_INDEX:
            similar_p = [p for p in PERIPH_INDEX if pname in p or p in pname][:5]
            return {
                "error": f"外设 {peripheral} 不存在",
                "similar_peripherals": similar_p,
                "hint": "用 list_peripherals 看所有外设,或 search_register 模糊查找",
            }
        else:
            similar_r = [r for r in PERIPH_INDEX[pname]["register_names"]
                         if rname in r or r in rname][:10]
            return {
                "error": f"{peripheral} 没有寄存器 {register}",
                "available_in_peripheral": PERIPH_INDEX[pname]["register_names"][:30],
                "similar": similar_r,
            }

    info = REG_INDEX[key]
    try:
        raw = jlink.memory_read32(info["addr"], 1)[0]
    except Exception as e:
        return {"error": f"读取失败: {e}", "addr": f"0x{info['addr']:08X}"}

    return {
        "peripheral": pname,
        "register": rname,
        "addr": f"0x{info['addr']:08X}",
        "description": info["description"][:120],
        "raw_hex": f"0x{raw:08X}",
        "raw_binary": f"0b{raw:032b}",
        "raw_decimal": raw,
        "fields": _decode_register(raw, info["fields"]),
    }


@mcp.tool()
def write_register_by_name(peripheral: str, register: str, value: int) -> dict:
    """
    按名字写外设寄存器,并自动回读 + 位域解析。

    ⚠ 谨慎使用。

    参数:
        peripheral: 外设名
        register: 寄存器名
        value: 32-bit 值
    """
    _ensure_connected()
    pname = peripheral.upper()
    rname = register.upper()
    key = (pname, rname)

    if key not in REG_INDEX:
        return {"error": f"未找到 {peripheral}.{register}",
                "hint": "用 list_registers_of_peripheral 查可用寄存器"}

    info = REG_INDEX[key]
    try:
        jlink.memory_write32(info["addr"], [value])
        readback = jlink.memory_read32(info["addr"], 1)[0]
    except Exception as e:
        return {"error": f"写入失败: {e}"}

    return {
        "peripheral": pname,
        "register": rname,
        "addr": f"0x{info['addr']:08X}",
        "written_hex": f"0x{value:08X}",
        "readback_hex": f"0x{readback:08X}",
        "match": readback == value,
        "readback_fields": _decode_register(readback, info["fields"]),
        "note": "" if readback == value else
                "回读不一致: 可能写到只读位/外设无时钟/有保护机制",
    }


@mcp.tool()
def describe_register(peripheral: str, register: str) -> dict:
    """
    显示寄存器的完整 SVD 定义(不读硬件,只看文档)。
    AI 在决定要不要修改某位之前应该先看清楚位域含义和访问权限。
    """
    pname = peripheral.upper()
    rname = register.upper()
    key = (pname, rname)
    if key not in REG_INDEX:
        return {"error": f"未找到 {peripheral}.{register}"}
    info = REG_INDEX[key]
    return {
        "peripheral": pname,
        "register": rname,
        "addr": f"0x{info['addr']:08X}",
        "description": info["description"],
        "size_bits": info["size_bits"],
        "reset_value": f"0x{(info['reset_value'] or 0):08X}",
        "fields": [
            {
                "name": f["name"],
                "bits": f"[{f['bit_offset']+f['bit_width']-1}:{f['bit_offset']}]"
                        if f["bit_width"] > 1 else f"[{f['bit_offset']}]",
                "access": f["access"],
                "description": f["description"],
            }
            for f in info["fields"]
        ],
    }


@mcp.tool()
def list_peripherals(name_contains: Optional[str] = None) -> dict:
    """
    列出所有外设(可选关键词过滤)。GD32H7xx 有 125 个外设,建议传 name_contains 过滤。

    参数:
        name_contains: 可选过滤字符串(大小写不敏感),如 "USART"/"TIMER"/"GPIO"/"ENET"
    """
    if name_contains:
        kw = name_contains.upper()
        names = [p for p in PERIPH_INDEX if kw in p]
    else:
        names = list(PERIPH_INDEX.keys())
    names.sort()
    return {
        "count": len(names),
        "filter": name_contains,
        "peripherals": [
            {
                "name": n,
                "base": f"0x{PERIPH_INDEX[n]['base']:08X}",
                "num_registers": len(PERIPH_INDEX[n]["register_names"]),
                "description": PERIPH_INDEX[n]["description"][:80],
            }
            for n in names[:60]
        ],
        "truncated": len(names) > 60,
    }


@mcp.tool()
def list_registers_of_peripheral(peripheral: str) -> dict:
    """
    列出某外设的所有寄存器名 + 描述。

    参数:
        peripheral: 外设名,如 "USART0" / "RCU" / "TIMER0"
    """
    pname = peripheral.upper()
    if pname not in PERIPH_INDEX:
        return {"error": f"外设 {peripheral} 不存在"}
    regs = []
    for rname in PERIPH_INDEX[pname]["register_names"]:
        info = REG_INDEX[(pname, rname)]
        regs.append({
            "name": rname,
            "offset": f"0x{info['addr'] - PERIPH_INDEX[pname]['base']:03X}",
            "addr": f"0x{info['addr']:08X}",
            "description": info["description"][:80],
        })
    return {
        "peripheral": pname,
        "base": f"0x{PERIPH_INDEX[pname]['base']:08X}",
        "description": PERIPH_INDEX[pname]["description"],
        "num_registers": len(regs),
        "registers": regs,
    }


@mcp.tool()
def search_register(keyword: str, max_results: int = 25) -> dict:
    """
    模糊搜索寄存器:在 3316 个寄存器名 + 位域名 + 描述里找包含关键词的。
    AI 不知道精确名字时用这个。

    参数:
        keyword: 关键词(大小写不敏感),如 "BAUD"/"USART0EN"/"watchdog"
        max_results: 最大返回数,默认 25
    """
    kw = keyword.upper()
    matches = []
    for (pname, rname), info in REG_INDEX.items():
        if kw in rname:
            matches.append({
                "peripheral": pname, "register": rname,
                "addr": f"0x{info['addr']:08X}",
                "match_by": "register_name",
                "description": info["description"][:80],
            })
            continue
        if kw in info["description"].upper():
            matches.append({
                "peripheral": pname, "register": rname,
                "addr": f"0x{info['addr']:08X}",
                "match_by": "register_description",
                "description": info["description"][:80],
            })
            continue
        for f in info["fields"]:
            if kw in f["name"].upper():
                matches.append({
                    "peripheral": pname, "register": rname,
                    "addr": f"0x{info['addr']:08X}",
                    "match_by": f"field_name: {f['name']}",
                    "description": f["description"][:80],
                })
                break
        if len(matches) >= max_results:
            break
    return {
        "keyword": keyword,
        "num_matches_shown": len(matches),
        "truncated": len(matches) >= max_results,
        "matches": matches,
    }


@mcp.tool()
def read_field(peripheral: str, register: str, field: str) -> dict:
    """
    只读寄存器里某一个位域的值(比读整个寄存器更简洁)。

    参数:
        peripheral: 外设名,如 "RCU"
        register: 寄存器名,如 "APB2EN"
        field: 位域名,如 "USART0EN"
    """
    _ensure_connected()
    pname = peripheral.upper()
    rname = register.upper()
    fname = field.upper()
    key = (pname, rname)
    if key not in REG_INDEX:
        return {"error": f"未找到 {peripheral}.{register}"}
    info = REG_INDEX[key]
    field_info = next((f for f in info["fields"] if f["name"].upper() == fname), None)
    if not field_info:
        return {
            "error": f"位域 {field} 不存在",
            "available_fields": [f["name"] for f in info["fields"]],
        }
    try:
        raw = jlink.memory_read32(info["addr"], 1)[0]
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    mask = (1 << field_info["bit_width"]) - 1
    val = (raw >> field_info["bit_offset"]) & mask
    return {
        "peripheral": pname,
        "register": rname,
        "field": field_info["name"],
        "bits": (f"[{field_info['bit_offset']+field_info['bit_width']-1}"
                 f":{field_info['bit_offset']}]"
                 if field_info["bit_width"] > 1 else f"[{field_info['bit_offset']}]"),
        "value": val,
        "hex": f"0x{val:X}",
        "register_raw_hex": f"0x{raw:08X}",
        "description": field_info["description"],
        "access": field_info["access"],
    }


# >>> [AI 2026-05-19] 暴露 ELF 相关 MCP 工具,用于符号断点和源码行反查
# ════════════════════════════════════════════════════════════════════
#  工具组 6: ELF 符号表 + DWARF 调试信息
# ════════════════════════════════════════════════════════════════════
@mcp.tool()
def load_elf(path: str) -> dict:
    """
    加载 ELF/AXF 文件,解析符号表和 DWARF 调试信息。
    每次重新编译固件后都应该重新调用以获取最新符号。

    参数:
        path: ELF 或 AXF 文件的完整路径
              Keil 编译输出通常在 工程目录/Objects/xxx.axf
              GCC/IAR 编译输出通常是 .elf
    """
    if not _HAS_ELFTOOLS:
        return {"error": "pyelftools 未安装。pip install pyelftools"}
    return _load_elf_file(path)


@mcp.tool()
def get_elf_status() -> dict:
    """查看当前 ELF 加载状态。"""
    return {
        "loaded": _ELF_LOADED,
        "path": _ELF_PATH,
        "num_symbols": len(_SYMBOLS),
        "num_line_entries": len(_LINE_BY_ADDR),
        "has_elftools": _HAS_ELFTOOLS,
    }


@mcp.tool()
def find_symbol(name: str) -> dict:
    """
    按精确名字查符号(函数或全局变量)的地址。
    返回的 addr 已清掉 Thumb bit,可直接用于下断点。
    """
    if not _ELF_LOADED:
        return {"error": "ELF 未加载,先调 load_elf"}
    if name not in _SYMBOLS:
        similar = [symbol_name for symbol_name in _SYMBOLS if name.lower() in symbol_name.lower()][:10]
        return {
            "error": f"未找到符号 '{name}'",
            "similar": similar,
            "hint": "试 search_symbols 模糊匹配",
        }

    symbol = _SYMBOLS[name]
    return {
        "name": name,
        "addr": f"0x{symbol['addr']:08X}",
        "addr_decimal": symbol["addr"],
        "size": symbol["size"],
        "type": symbol["type"],
        "is_function": symbol["type"] == "STT_FUNC",
    }


@mcp.tool()
def search_symbols(keyword: str, max_results: int = 20,
                   only_functions: bool = False) -> dict:
    """模糊搜索符号名。"""
    if not _ELF_LOADED:
        return {"error": "ELF 未加载,先调 load_elf"}

    kw = keyword.lower()
    matches = []
    for name, symbol in _SYMBOLS.items():
        if kw not in name.lower():
            continue
        if only_functions and symbol["type"] != "STT_FUNC":
            continue

        matches.append({
            "name": name,
            "addr": f"0x{symbol['addr']:08X}",
            "size": symbol["size"],
            "type": "function" if symbol["type"] == "STT_FUNC" else "object",
        })
        if len(matches) >= max_results:
            break

    return {
        "keyword": keyword,
        "num_matches": len(matches),
        "truncated": len(matches) >= max_results,
        "matches": matches,
    }


@mcp.tool()
def addr_to_symbol(addr: int) -> dict:
    """给定地址,反查它在哪个函数/变量里。"""
    if not _ELF_LOADED:
        return {"error": "ELF 未加载,先调 load_elf"}

    result = _find_nearest_symbol(addr)
    if result is None:
        return {
            "addr": f"0x{addr:08X}",
            "found": False,
            "note": "该地址不在任何已知符号范围内",
        }

    name, offset = result
    symbol = _SYMBOLS[name]
    return {
        "addr": f"0x{addr:08X}",
        "found": True,
        "symbol": name,
        "type": "function" if symbol["type"] == "STT_FUNC" else "object",
        "symbol_addr": f"0x{symbol['addr']:08X}",
        "offset_in_symbol": offset,
        "size": symbol["size"],
    }


@mcp.tool()
def addr_to_source(addr: int) -> dict:
    """给定地址,反查 C 源码文件名和行号。"""
    if not _ELF_LOADED:
        return {"error": "ELF 未加载,先调 load_elf"}
    if not _LINE_BY_ADDR:
        return {"error": "ELF 没有 DWARF 行号信息(编译时未带 -g?)"}

    result = _find_nearest_line(addr)
    if result is None:
        return {"addr": f"0x{addr:08X}", "found": False}

    fname, line = result
    return {
        "addr": f"0x{addr:08X}",
        "found": True,
        "file": fname,
        "line": line,
    }


@mcp.tool()
def source_to_addr(file: str, line: int) -> dict:
    """
    给定源码文件名和行号,反查对应的指令地址。
    file 只按 basename 匹配,如 main.c。
    """
    if not _ELF_LOADED:
        return {"error": "ELF 未加载,先调 load_elf"}
    if not _FILE_LINE_TO_ADDR:
        return {"error": "ELF 没有 DWARF 行号信息"}

    if (file, line) in _FILE_LINE_TO_ADDR:
        addr = _FILE_LINE_TO_ADDR[(file, line)]
        return {
            "file": file,
            "line": line,
            "addr": f"0x{addr:08X}",
            "addr_decimal": addr,
        }

    basename = os.path.basename(file).lower()
    candidates = []
    for (fname, file_line), addr in _FILE_LINE_TO_ADDR.items():
        if os.path.basename(fname).lower() == basename and file_line == line:
            candidates.append((fname, file_line, addr))

    if not candidates:
        nearby = []
        for (fname, file_line), _ in _FILE_LINE_TO_ADDR.items():
            if os.path.basename(fname).lower() == basename:
                nearby.append(file_line)
        nearby = sorted(set(nearby))
        nearby_suggestion = [candidate_line for candidate_line in nearby if abs(candidate_line - line) <= 5][:10]
        return {
            "error": f"未找到 {file}:{line}",
            "nearby_lines_in_file": nearby_suggestion,
            "hint": "DWARF 只记录语句首的行号,空行/注释行/部分声明行没有对应地址",
        }

    fname, matched_line, addr = candidates[0]
    return {
        "file": fname,
        "line": matched_line,
        "addr": f"0x{addr:08X}",
        "addr_decimal": addr,
        "note": f"{len(candidates)} 个匹配" if len(candidates) > 1 else "",
    }


@mcp.tool()
def set_breakpoint_at_symbol(symbol: str) -> dict:
    """在指定函数符号上设置硬件断点。"""
    _ensure_connected()
    if not _ELF_LOADED:
        return {"error": "ELF 未加载,先调 load_elf"}
    if symbol not in _SYMBOLS:
        similar = [name for name in _SYMBOLS if symbol.lower() in name.lower()][:5]
        return {"error": f"未找到符号 '{symbol}'", "similar": similar}

    symbol_info = _SYMBOLS[symbol]
    if symbol_info["type"] != "STT_FUNC":
        return {"error": f"'{symbol}' 不是函数 (type={symbol_info['type']}),不能下断点"}

    addr = symbol_info["addr"]
    try:
        bp_id = jlink.breakpoint_set(addr, thumb=True)
    except Exception as e:
        return {"error": f"下断点失败: {e}"}

    return {
        "bp_id": bp_id,
        "symbol": symbol,
        "addr": f"0x{addr:08X}",
        "total_breakpoints": jlink.num_active_breakpoints(),
    }
# <<< [AI]


# ════════════════════════════════════════════════════════════════════
#  启动
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info(f"GD32 Debug MCP server starting (target={TARGET_DEVICE}, SVD={_svd_device.name})")
    try:
        mcp.run()
    except KeyboardInterrupt:
        log.info("收到中断,关闭 J-Link")
    finally:
        if jlink.opened():
            try:
                jlink.close()
            except Exception:
                pass
        log.info("退出")
