"""
Disassemble EPD init code from Bigme F7 boot partition.

Boot payload is loaded at SRAM 0x20201000. Entry point 0x20201100 (Thumb).
Vector table occupies the first 0x100 bytes; code starts at entry point.

Strategy:
1. Find EPD string SRAM addresses.
2. Search the entire payload for those 32-bit addresses stored in literal pools.
3. For each literal pool hit, the referencing LDR instruction is nearby (within
   ~4 KB by ARM PC-relative encoding limits). Scan that window for the LDR.
4. Walk backward from the LDR to find the function prologue (PUSH {... lr}).
5. Disassemble the full function.
"""

import struct

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from capstone.arm import ARM_REG_PC

PAYLOAD_PATH = (
    r"c:\Users\defl\workspace\hokku_epaper\.private\screens\bigme_f7\partitions\01_boot_payload.bin"
)
LOAD_ADDR = 0x00201000  # XR872AT code RAM base (not ARM SRAM 0x20000000!)
ENTRY_OFF = 0x100  # entry point offset within payload

with open(PAYLOAD_PATH, "rb") as f:
    data = f.read()

size = len(data)
print(f"Payload: {size} bytes, SRAM base 0x{LOAD_ADDR:08X}, entry offset 0x{ENTRY_OFF:X}")

# ── 1. Build string map ──────────────────────────────────────────────────────
strings = {}
buf = []
start = None
for i, b in enumerate(data):
    if 0x20 <= b < 0x7F:
        if start is None:
            start = i
        buf.append(chr(b))
    else:
        if b == 0 and buf and len(buf) >= 4 and start is not None:
            strings[LOAD_ADDR + start] = "".join(buf)
        buf = []
        start = None

EPD_KEYS = ["check_busy_high", "epd_test", "initial end"]
epd_strings = {addr: txt for addr, txt in strings.items() if any(k in txt for k in EPD_KEYS)}

print(f"\nEPD strings ({len(epd_strings)}):")
for addr, txt in sorted(epd_strings.items()):
    print(f"  0x{addr:08X} (payload+0x{addr - LOAD_ADDR:04X}): [{txt}]")

# ── 2. Find literal pool entries containing EPD string addresses ─────────────
pool_hits = []  # (pool_offset_in_payload, sram_string_addr)

for saddr in epd_strings:
    needle = struct.pack("<I", saddr)
    off = 0
    while True:
        pos = data.find(needle, off)
        if pos < 0:
            break
        pool_hits.append((pos, saddr))
        off = pos + 4

print(f"\nLiteral pool hits ({len(pool_hits)}):")
for off, saddr in pool_hits:
    print(f"  payload+0x{off:05X}  SRAM 0x{off + LOAD_ADDR:08X}  -> string [{epd_strings[saddr]}]")

# ── 3. For each pool hit, find the LDR instruction that loads it ─────────────
# LDR Rn, [PC, #off]: PC = (insn_addr + 4) & ~3
# pool_addr = PC + off  => off = pool_addr - PC
# So: insn_addr ~ pool_addr - 4096 .. pool_addr-4 (off can be 0..4095 for T1)
# For T2 (32-bit LDR.W): off can be 0..4095

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
md.detail = True


def disasm_range(payload, load_addr, start_off, length):
    chunk = payload[start_off : start_off + length]
    return list(md.disasm(chunk, load_addr + start_off))


def find_ldr_for_pool(payload, load_addr, pool_off, window_before=4096):
    """Find LDR instructions in [pool_off - window, pool_off] that reference pool_off."""
    pool_sram = load_addr + pool_off
    search_start = max(ENTRY_OFF, pool_off - window_before)
    insns = disasm_range(payload, load_addr, search_start, pool_off - search_start + 4)
    hits = []
    for ins in insns:
        if ins.mnemonic in ("ldr", "ldr.w"):
            for op in ins.operands:
                if op.type == 3:  # MEM operand
                    if op.mem.base == ARM_REG_PC:
                        pc = (ins.address + 4) & ~3
                        target = pc + op.mem.disp
                        if target == pool_sram:
                            hits.append(ins)
    return hits


ldr_hits = []  # list of (ldr_insn_address, sram_string_addr)

for pool_off, saddr in pool_hits:
    ldrs = find_ldr_for_pool(data, LOAD_ADDR, pool_off)
    for lins in ldrs:
        ldr_hits.append((lins.address, saddr))
        print(
            f"  LDR at 0x{lins.address:08X}  "
            f"(payload+0x{lins.address - LOAD_ADDR:04X})  "
            f"-> [{epd_strings[saddr]}]"
        )

if not ldr_hits:
    print("No LDR hits found. Dumping all LDR candidates in code range:")
    # Fallback: disassemble code section and show all LDRs that hit our address range
    insns = disasm_range(data, LOAD_ADDR, ENTRY_OFF, size - ENTRY_OFF)
    target_addrs = set(epd_strings.keys())
    for ins in insns:
        if ins.mnemonic in ("ldr", "ldr.w"):
            for op in ins.operands:
                if op.type == 3 and op.mem.base == ARM_REG_PC:
                    pc = (ins.address + 4) & ~3
                    val_off = pc + op.mem.disp - LOAD_ADDR
                    if 0 <= val_off <= len(data) - 4:
                        val = struct.unpack_from("<I", data, val_off)[0]
                        if val in target_addrs:
                            ldr_hits.append((ins.address, val))
                            print(
                                f"  LDR at 0x{ins.address:08X}: loads 0x{val:08X} [{epd_strings[val]}]"
                            )

print(f"\n{len(ldr_hits)} LDR reference(s) to EPD strings found")

# ── 4. Walk backward to function prologue ────────────────────────────────────


def find_func_boundary(payload, load_addr, ref_sram, scan_back_bytes=2048):
    """Scan backward from ref_sram in Thumb mode to find PUSH {... lr}."""
    ref_off = ref_sram - load_addr
    start_off = max(ENTRY_OFF, ref_off - scan_back_bytes)
    insns = disasm_range(payload, load_addr, start_off, ref_off - start_off + 2)
    # Walk backward through instructions
    for ins in reversed(insns):
        if ins.address >= ref_sram:
            continue
        if ins.mnemonic == "push" and "lr" in ins.op_str:
            return ins.address
        if ins.mnemonic in ("stmdb", "stmfd") and "lr" in ins.op_str:
            return ins.address
    # Fallback: return 64 bytes before ref
    return max(load_addr + ENTRY_OFF, ref_sram - 256)


def disasm_func(payload, load_addr, func_start_sram, max_bytes=3072):
    """Disassemble from func_start until BX LR or POP {... PC}."""
    off = func_start_sram - load_addr
    if off < 0 or off >= len(payload):
        return []
    insns = disasm_range(payload, load_addr, off, min(max_bytes, len(payload) - off))
    lines = []
    for ins in insns:
        b = ins.bytes.hex()
        lines.append(
            (ins.address, f"  {ins.address:08X}  {b:16s}  {ins.mnemonic:<10s} {ins.op_str}")
        )
        if ins.mnemonic == "bx" and "lr" in ins.op_str:
            break
        if ins.mnemonic == "pop" and "pc" in ins.op_str:
            break
    return lines


# Collect unique function starts
seen = set()
func_info = []
for ldr_addr, saddr in sorted(ldr_hits):
    fs = find_func_boundary(data, LOAD_ADDR, ldr_addr)
    if fs not in seen:
        seen.add(fs)
        func_info.append((fs, ldr_addr, saddr))

print(f"\n{'=' * 72}")
print(f"EPD-related functions ({len(func_info)} unique)")
print(f"{'=' * 72}\n")

for fs, _ldr_addr, saddr in sorted(func_info):
    print(f"┌─ FUNCTION  0x{fs:08X}  (payload+0x{fs - LOAD_ADDR:04X})  ref [{epd_strings[saddr]}]")
    lines = disasm_func(data, LOAD_ADDR, fs)
    for _, text in lines:
        # Annotate lines where we have string refs
        ann = ""
        addr_hex = int(text.split()[0], 16)
        for la, sa in ldr_hits:
            if la == addr_hex:
                ann = f"   <- loads [{epd_strings[sa]}]"
        print(text + ann)
    print()
