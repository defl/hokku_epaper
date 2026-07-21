"""Annotated disassembler for the phoenixMC Linux ELF (x86-64).

Disassembles a named function, resolving call targets to symbols and
RIP-relative loads to string/data contents.

Usage:
    python tools/_dis_phoenix.py <symbol-substring> [more...]
    python tools/_dis_phoenix.py ReadSector ReadFlashLength FlashOperate
"""

import sys

from capstone import CS_ARCH_X86, CS_MODE_64, CS_OP_IMM, CS_OP_MEM, Cs
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from elftools.elf.sections import SymbolTableSection

from _private import res

ELF = res("bigme_flash_tool_bin")


def load():
    fh = open(ELF, "rb")
    elf = ELFFile(fh)
    # Map vaddr -> (file offset, size) per section, and gather symbol table
    sections = []
    for sec in elf.iter_sections():
        addr = sec["sh_addr"]
        if addr == 0:
            continue
        sections.append((addr, sec["sh_size"], sec["sh_offset"], sec.name, sec["sh_type"]))
    syms = {}  # addr -> name
    funcs = {}  # name -> (addr, size)
    symtab = elf.get_section_by_name(".symtab")
    assert isinstance(symtab, SymbolTableSection)
    for s in symtab.iter_symbols():
        if not s.name:
            continue
        v = s["st_value"]
        if v:
            syms.setdefault(v, s.name)
        if s["st_info"]["type"] == "STT_FUNC" and v:
            funcs[s.name] = (v, s["st_size"])

    # Resolve PLT stubs -> imported symbol names via .rela.plt + .plt.sec/.plt
    dynsym = elf.get_section_by_name(".dynsym")
    rela = elf.get_section_by_name(".rela.plt")
    plt = None
    for cand in (".plt.sec", ".plt"):
        plt = elf.get_section_by_name(cand)
        if plt:
            break
    if isinstance(rela, RelocationSection) and isinstance(dynsym, SymbolTableSection) and plt:
        plt_base = plt["sh_addr"]
        # First PLT entry is the resolver stub (.plt only); .plt.sec has none.
        start = plt_base + (16 if plt.name == ".plt" else 0)
        for i, r in enumerate(rela.iter_relocations()):
            sym = dynsym.get_symbol(r["r_info_sym"])
            if sym and sym.name:
                syms.setdefault(start + i * 16, sym.name + "@plt")

    data = open(ELF, "rb").read()
    return data, sections, syms, funcs


def vaddr_to_off(sections, vaddr):
    for addr, size, off, _name, stype in sections:
        if addr <= vaddr < addr + size:
            if stype == "SHT_NOBITS":  # .bss — no file content
                return None
            return off + (vaddr - addr)
    return None


def read_cstr(data, sections, vaddr, maxlen=80):
    off = vaddr_to_off(sections, vaddr)
    if off is None:
        return None
    end = data.find(b"\x00", off, off + maxlen)
    if end < 0:
        end = off + maxlen
    raw = data[off:end]
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if all(32 <= ord(c) < 127 or c in "\t\n\r" for c in s) and len(s) >= 2:
        return s
    return None


def nearest_sym(syms, addr):
    best = None
    for a, n in syms.items():
        if a <= addr and (best is None or a > best[0]):
            best = (a, n)
    if best and addr - best[0] < 0x2000:
        delta = addr - best[0]
        return best[1] + (f"+0x{delta:x}" if delta else "")
    return None


def disasm_func(data, sections, syms, funcs, name):
    addr, size = funcs[name]
    off = vaddr_to_off(sections, addr)
    code = data[off : off + size]
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    print(f"\n{'=' * 78}\n{name}\n  @ 0x{addr:x}  size={size}\n{'=' * 78}")
    for ins in md.disasm(code, addr):
        line = f"  0x{ins.address:08x}:  {ins.mnemonic:<7} {ins.op_str}"
        ann = []
        # Resolve call/jmp immediate targets
        if ins.mnemonic.startswith(("call", "jmp")) or ins.mnemonic.startswith("j"):
            for op in ins.operands:
                if op.type == CS_OP_IMM:
                    tgt = op.imm
                    s = nearest_sym(syms, tgt)
                    if s:
                        ann.append(f"-> {s}")
        # Resolve RIP-relative memory loads (strings / data)
        for op in ins.operands:
            if op.type == CS_OP_MEM and op.mem.base == 0 and op.mem.index == 0:
                pass
            if op.type == CS_OP_MEM and ins.mnemonic in ("lea", "mov", "movzx", "cmp"):
                m = op.mem
                # RIP-relative: capstone reg name 'rip'
                if m.base != 0 and ins.reg_name(m.base) == "rip":
                    tgt = ins.address + ins.size + m.disp
                    cstr = read_cstr(data, sections, tgt)
                    if cstr is not None:
                        ann.append(f'"{cstr}"')
                    else:
                        s = nearest_sym(syms, tgt)
                        if s:
                            ann.append(f"&{s}")
        if ann:
            line = f"{line:<60} ; {'  '.join(ann)}"
        print(line)


def main():
    data, sections, syms, funcs = load()
    targets = sys.argv[1:] or ["ReadSector"]
    for t in targets:
        matches = [n for n in funcs if t in n]
        if not matches:
            print(f"(no symbol matching {t!r})")
            continue
        for name in matches:
            disasm_func(data, sections, syms, funcs, name)


if __name__ == "__main__":
    main()
