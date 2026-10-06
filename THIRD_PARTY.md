# 第三方来源

`image/bridge/server.py` 从用户维护的既有 MCP 桥迁入，沿用 `codex-with-chatgpt` 的工作区工具设计，并在本项目修复搜索、路径、Git执行与协议问题。它不是独立重写的实现。精确迁入版本未记录，设计参考上游的MIT许可已核对并保留；用户维护的迁入实现与本项目新增源码按本仓库MIT许可证发布。

`security/apparmor-upstream.go` 和 `security/seccomp-upstream.json` 来源于 [Moby profiles](https://github.com/moby/profiles)，版权属于 The Moby Authors，遵循 Apache-2.0；许可证全文见 [security/LICENSE.moby](security/LICENSE.moby)。保留原始源码用于核对及生成配置。

`security/generate.py` 派生两份 `codexpp` 配置：新增允许嵌套沙箱所需的命名空间及挂载规则，并修改 AppArmor ABI 和 Unix socket 规则。这些修改扩大了默认容器内核攻击面，详见 README。生成文件中的修改声明及来源归属必须保留。

镜像构建时从各上游下载 Node/Debian、Codex CLI、cloudflared、Google Chrome、Xvfb、x11vnc 和 noVNC；它们各自的许可证或使用条款继续适用。仓库不包含浏览器安装包、登录 profile 或账号凭据。发布镜像时须另行核对镜像内各组件的分发条款。

用户已选择本项目采用MIT，全文见根目录LICENSE。Moby派生规则及原始文件继续按Apache-2.0保留归属与许可；镜像依赖保留各自许可证或使用条款。

上游原始文件的精确下载提交未记录；以下 SHA256 标识本次保留的输入，生成结果不依赖在线 main 分支：

- security/apparmor-upstream.go：7da15d81aa5c507f5002b844fdc49a969904681447a2cc29222bd6cbe797163b
- security/seccomp-upstream.json：6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6

## 已核对的设计参考上游

[XiaoDuoYa/codex-with-chatgpt](https://github.com/XiaoDuoYa/codex-with-chatgpt) 当前采用 [MIT许可证](https://github.com/XiaoDuoYa/codex-with-chatgpt/blob/main/LICENSE)，原文和版权声明保存在 `security/LICENSE.codex-with-chatgpt`。本地Python桥是既有部署代码的迁入及整改，其精确迁入版本未记录；不能把当前上游版本说成逐文件相同的来源。保留上游许可及归属说明，不删除其版权声明。
