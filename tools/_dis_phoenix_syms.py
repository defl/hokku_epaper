"""List flash/BROM-related function symbols in the phoenixMC Linux ELF.

Usage: python tools/_dis_phoenix_syms.py
"""

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from _private import res

ELF = res("bigme_flash_tool_bin")

KEYWORDS = (
    "Flash",
    "ReadSector",
    "WriteSector",
    "ChangeBaud",
    "GetFlashId",
    "FlashId",
    "SysReboot",
    "Erase",
    "DownLoad",
    "Download",
    "Brom",
    "BROM",
    "Read",
    "Dump",
    "Crc",
    "CRC",
    "Sync",
    "Connect",
    "Cmd",
    "Upgrade",
)


def main():
    with open(ELF, "rb") as fh:
        elf = ELFFile(fh)
        for secname in (".symtab", ".dynsym"):
            sec = elf.get_section_by_name(secname)
            if not isinstance(sec, SymbolTableSection):
                print(f"(no {secname})")
                continue
            print(f"\n==== {secname} ({sec.num_symbols()} symbols) ====")
            rows = []
            for sym in sec.iter_symbols():
                n = sym.name
                if not n:
                    continue
                if any(k in n for k in KEYWORDS):
                    info = sym["st_info"]
                    if info["type"] != "STT_FUNC":
                        continue
                    rows.append((sym["st_value"], sym["st_size"], n))
            rows.sort()
            for val, size, n in rows:
                print(f"  0x{val:08x}  size={size:6d}  {n}")


if __name__ == "__main__":
    main()
