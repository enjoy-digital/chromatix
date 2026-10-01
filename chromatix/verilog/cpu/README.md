# VexRiscv variants (generated)

`VexRiscv_Doom.v`: VexRiscv `standard` configuration (LiteX `GenCoreDefault`) with 8KB instruction
and data caches, used by `--with-doom`. Generated from LiteX's pythondata-cpu-vexriscv (VexRiscv
b6118e5c, SpinalHDL 1.9.4):

```bash
cd pythondata_cpu_vexriscv/verilog   # with the ext/VexRiscv submodule initialized
sbt "runMain vexriscv.GenCoreDefault --iCacheSize 8192 --dCacheSize 8192 --outputFile VexRiscv_Doom"
```
