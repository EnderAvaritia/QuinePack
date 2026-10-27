# ZIP 不纳入产物格式

## Status

accepted

## Context

Windows 10 资源管理器原生只认 ZIP，因此曾认真考虑用 store-only ZIP 作为双击兼容变体。核查 ZIP 规范与现有 quine 实现后确认：

- ZIP 的 local file header 没有“数据偏移”字段；entry 数据区从本地头之后开始（至少 30 字节 + 文件名 + extra）。
- 若自含副本 entry 的数据区是文件的某个后缀，它不可能逐字节等于整个文件（有限字符串的真后缀不可能等于自身）；若数据区越过 EOF，读取器无法读满。
- 因此 store-only ZIP 无法做到“解压出的单个文件 = 整个 ZIP”。
- 依赖 DEFLATE 的 ZIP quine 通过 LZ77 自引用实现，受 32 KiB 窗口约束；带额外载荷的递归 ZIP quine 总量被限制在约 32 KiB 量级，无法支撑 1 GiB。

## Decision

V1 不提供 ZIP 产物。兼容变体使用 7z-store（单 coder、store-only）。Windows 10 用户需要安装 7-Zip 才能打开 7z；Windows 11 23H2+ 的资源管理器可通过 libarchive 打开单 coder 的 7z-store，但压缩型 7z 仍不可用。

## Considered Options

- store-only ZIP quine：结构上不可行（数据区无法从文件起点开始）。
- DEFLATE ZIP quine：只适合约 32 KiB 以内的载荷。
- SFX 自解压 EXE：产物是程序而不是归档，且容易被杀软误报。
- 普通 ZIP 加一份 ZIP 副本：不是 quine，副本无法包含自身。

## Consequences

- 双击兼容性只在 Windows 11 23H2+ 的 7z-store 上成立；Windows 10 需要安装 7-Zip。
- 若未来要支持小载荷 ZIP quine，必须新增 ADR，并明确只适用于约 32 KiB 以内的载荷。