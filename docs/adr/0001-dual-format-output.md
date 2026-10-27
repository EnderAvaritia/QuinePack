# 同时产出 7z 与 tar.xz 两种自含压缩包

## Status

accepted

## Context

自含压缩包的接收方分成两个世界：Windows/macOS 上习惯用官方 7-Zip 打开 `.7z`；Linux 服务器与 BusyBox 环境原生能解 `.tar.xz`，但往往不装 7-Zip。文章已验证：双层 LZMA2 的 7z Quine 官方 7-Zip 可解，但 Windows 资源管理器（libarchive）不可解；tar.xz 路线则要自写 LZMA1 区间编码器。

## Decision

生成器同时支持 `.7z` 与 `.tar.xz` 两种产物。二者共享同一套载荷读取与自含布局，但各自拥有独立的编码器与校验定点实现。

## Considered Options

- 只做 7z：Windows 生态最好，Linux/BusyBox 需要额外安装 7-Zip。
- 只做 tar.xz：Linux 原生，但 Windows 用户多数没有 xz 工具，且要自写 LZMA1 编码器。
- 自定义容器：实现自由，但产物失去全部现成解压生态。

## Consequences

- 需要维护两套位级编码器、两套校验定点逻辑、两套兼容性验证矩阵。
- 兼容性测试必须包含真实外部解压器（7z、xz/tar、BusyBox），不能只用自己的解码器自证。
- 任一格式的规模上限、元数据保真能力会不一致，CLI 必须如实暴露这些差异。