# codex++ 运行与安全细节

> 本文是完整的部署、任务流程与安全边界说明；快速了解请看 [README](../README.md)。

Codex CLI、独立 ChatGPT Chrome、只读工作区 MCP 和隧道的一体化 Docker。源码采用MIT许可，可克隆、自行构建和使用。当前是实验版本，发布前由Claude审计；适用于可信宿主和可信执行内容。立项与发布门槛见 [CHARTER.md](../CHARTER.md)。独立登录、真实MCP调用、沙箱及浏览器启停已有本机验证；完整任务最终验收、固定域名与可移植安装仍待完成。此前一次锁释放失败未复现，根因尚未确定。

```bash
git clone https://github.com/martinwu0211/codex-plus-plus.git
cd codex-plus-plus
```

构建前请完成下文的宿主UID、AppArmor和共享锁准备。每位使用者分别登录自己的Codex和ChatGPT账号；仓库不包含登录数据。当前发布源码，由使用者在本机构建镜像。

```
bash build_and_test.sh          # ubuntu 执行：构建镜像 + 测试容器 + doctor + 清理
CODEXPP_PROJECT_ROOT=/projects CODEXPP_WORKSPACE=/projects/demo ./codex++ up
./codex++ doctor | url [--full] | logs | status | codex | shell | down
./codex++ codex-login           # 容器里的 Codex 单独登录
./codex++ login                 # 在远程桌面登录专属 ChatGPT Chrome
./codex++ browser-status
./codex++ browser-stop          # 关闭并释放全机锁
```

任务流程支持内容摘要批准、执行互斥、一次性执行标记、独立子进程看守及审阅摘要核对。`chat_task.py`与`connector.mjs`接入ChatGPT页面传输。源码和当前运行镜像存在版本差异，正式发布前须重新构建。一个小任务曾完成规划、批准、执行和审阅，审阅指出证据不足，最终验收仍待完成。

```
./codex++ task new '任务内容'         # 输出任务编号 ID
./codex++ chat start ID plan
./codex++ chat wait ID plan
./codex++ task show-plan ID           # 阅读任务、计划及 sha256
./codex++ task approve ID --hash SHA256
./codex++ task execute ID
./codex++ chat start ID review
./codex++ chat wait ID review
./codex++ task accept ID              # 人工审阅结果后再验收
./codex++ browser-stop               # 结束后关闭专属浏览器并释放共享锁
```

同一时刻只允许一个在途浏览器会话。计划批准后不会被轮询覆盖；执行开始后不能重跑。执行失败时先核实残留改动，再新建任务，不删除一次性执行标记来重试。超时可设置容器环境 `CODEXPP_EXEC_TIMEOUT`（默认240秒，加清理时间）。提交结果不确定时先在浏览器确认确实未发送，再执行 `./codex++ chat reset-submit ID plan --confirmed-unsent`；不能用它重发已经确认发送的消息。历史任务缺少新的摘要字段时，应重新审阅，不能直接验收。

首次安装需要分别完成Codex和ChatGPT登录，登录信息保存在数据卷；已有卷重启后无需重新登录。

Codex 沙箱：容器内的 Codex 沙箱使用专用安全规则，见 [seccomp 配置](../security/codexpp-seccomp.json) 和 [AppArmor 配置](../security/codexpp.apparmor)，用于支持容器内的嵌套沙箱运行。

| 文件 | 作用 |
|---|---|
| `image/Dockerfile` | node:22 + python3 + git + cloudflared + Codex + 官方 Chrome + Xvfb/x11vnc/noVNC，用户 uid 1000 |
| `image/bin/browser.py` | 独立 `/data/chrome-profile`，拿到宿主同一把锁后启动，关闭所有子进程再放锁；默认最多30分钟 |
| `image/bridge/server.py` | 只读文件和Git检查；不执行搜索命令，统一拒绝敏感名称与链接；Git使用无原仓库配置的临时快照 |
| `image/bin/supervisor.sh` | 常驻桥与隧道，挂了自动重启；quick 模式抓取公网地址写入 `public_url` |
| `image/bin/doctor.py` | 检查本地/公网协议、访问日志、登记地址与真实工具验收；非doctor请求不能直接当作ChatGPT证明 |
| `image/bin/connector.mjs` | Node22原生CDP操作插件及会话，校验会话路径与响应摘要；服务日志只能证明发生过工具调用，不能归属到具体任务 |
| `image/bin/execution_guard.py` | 独立看守执行超时和父进程退出，收养并清理脱离原进程组的子进程 |
| `codex++` | 宿主命令，不在 docker 组时自动 sudo |

当前支持 Linux amd64、宿主 UID 1000、支持 user namespaces/Landlock 的内核与 AppArmor ABI 4.0。工作区必须显式指定项目目录，拒绝系统根目录和账户目录。启动前须加载 `security/codexpp.apparmor`：`sudo apparmor_parser -r security/codexpp.apparmor`。共享锁必须预先存在且 UID 1000 可读，默认 `/run/lock/browser-fleet/chrome.lock`；可用 `CODEXPP_HOST_LOCK` 指定已有锁。没有其他浏览器服务的新机器可由管理员创建专用锁目录和文件；已有共享锁时不要重建、替换或删除文件，否则会破坏互斥。目录以只读方式挂载，避免单文件挂载一直指向旧 inode。

默认使用只属于本容器的 Docker bridge 网络和容器内 Xvfb `:100`，noVNC 只向宿主 `127.0.0.1:6082` 发布，CDP/VNC/MCP 仅在容器环回地址监听。请勿让其他不可信容器加入专用网络。如本机 Docker bridge 出网受阻，可显式设置 `CODEXPP_NETWORK=host`；这会允许访问宿主网络服务，不能用作隔离不可信本地程序的安全边界。共享 X99 桌面也需显式设置 `CODEXPP_EXTERNAL_DISPLAY=1`，只挂载 X99 socket，但共享 X11 仍扩大对宿主桌面的访问范围，host 模式还可访问抽象 X socket。本机此前验证使用这两个兼容选项，默认 bridge 模式仍待新机器验收。

运行命令删除全部宿主 capabilities、开启 no-new-privileges 并限制进程数。专用 seccomp/AppArmor 为嵌套沙箱放开 user namespaces/mount 等能力，扩大了 Docker 默认内核攻击面；新用户命名空间内的 capabilities 与宿主不同。Chrome 当前使用 `--no-sandbox`，依赖容器边界。独立 profile 防止登录状态混用，不意味着能抵御同一宿主上的可信账户被攻破。

MCP 名称过滤属于尽力保护，不能识别任意正常命名文件里的密钥。read/list/search拒绝符号链接和多重硬链接。Git快照上限128 MiB、20,000条目、单次输出120 KB，忽略子模块；仅保证普通 SHA-1、独立 `.git` 目录仓库，不支持 worktree 元数据文件及外部对象存储。查询可能因规模或对象缺失被拒绝。

可在启动时设置 `MCP_DENY_NAMES=attachments,generated-media ./codex++ up`，追加部署自己的文件或目录名。配置按逗号分隔、不区分大小写，并匹配任意层级的完整名称；不接受路径，不展开通配符，不覆盖内置凭据保护。读取、列表、搜索和Git查询使用同一名单。修改后须重新创建容器，已有容器不会自动更新。

路径密钥是 bearer 凭据，`url --full` 输出需按密钥管理；不应把它放到群聊、截图或公共日志。应用访问日志不保留路径和密钥；自建反代仍需自行关闭或清理带密钥路径的日志。验收摘要放在数据卷，工作区内证据只能作参考。`./codex++ sandbox-check`验证工作区写入与数据卷写入边界；它不证明任意第三方组件可安全运行。

`CODEXPP_PROJECT_ROOT`与工作区均须显式指定，拒绝系统、隐藏凭据及账户根目录。工作区不能与本仓库的宿主脚本和策略目录重叠，安全策略也不能放在可写工作区内。共享锁只能位于专用 `/run/lock/browser-fleet` 目录，不把任意锁所在的宿主目录挂入容器。宿主重启后须由宿主服务或 tmpfiles 恢复锁；锁缺失时容器启动失败。

无法采集的浏览器阶段可在核实后执行 `./codex++ chat abandon ID plan --confirmed-abandon`，保留记录、标记失败并释放在途会话。执行报告在完成时记录摘要，审阅和验收均复核；验收仍由操作人判断报告结论，摘要不等于正确性证明，也不阻止其他进程在审阅后修改工作区。

测试不改正式镜像标签，失败保留非零退出码并清理测试容器、卷及临时目录。本机兼容模式运行测试时设置 `CODEXPP_NETWORK=host CODEXPP_BUILD_NETWORK=host`；共享桌面可按需单独启用。基础镜像固定digest，Codex固定版本，两个deb核对固定SHA256；Debian apt包未固定快照，不能承诺逐字节可复现。第三方来源见 [THIRD_PARTY.md](../THIRD_PARTY.md)。

bridge 网络仍允许访问宿主网关、局域网和云元数据地址，不是出站访问控制。host 模式下，本机进程可访问无认证的 CDP、VNC/noVNC 和 Xvfb；仅在可信宿主使用。容器中的 Codex 与浏览器共享数据卷，当前沙箱检查验证写入边界，没有证明登录凭据不可读取。MCP 工具只读，而 Codex 执行阶段可写工作区。全局执行锁避免本容器两个任务同时执行，但验收摘要不锁定整个工作区，也不排除外部进程改动。

Google 可能移除固定版本安装包，导致构建返回404。更新时修改 `scripts/lock_dependencies.py` 的版本、重新生成依赖清单，并重新构建和验证；不能绕过 SHA256 检查。授权卡片自动点击依赖页面结构，目前仍须实测核对；识别失败时在专属浏览器中人工批准只读工具。浏览器控制进程异常退出由看守回收子进程；看守本身被 SIGKILL 或整个容器故障属于单独恢复场景，不能据此保证任意脱离进程组的后代自动退出。

`connect` 会关闭已有专属窗口后打开新版插件目录。创建入口为 Add → Create custom MCP server，Authentication选择No authentication；完整地址带路径密钥，不写入文档。插件绑定需用真实工具调用验收；quick隧道重启换域名后须更新插件地址并重新验收。登录数据在codexpp-data卷保留。

所有模式的 noVNC 都没有口令，宿主本地用户可访问已登录桌面；需可信宿主和 Docker 28 以上版本。named 模式仅挂载 CODEXPP_TUNNEL_CONFIG（yml）和 CODEXPP_TUNNEL_CREDENTIAL（json），配置的 credentials-file 须指向 /run/secrets/tunnel/credentials.json，不挂载账户级 cert.pem 或整个凭据目录。

审计保留事项：沙箱探针要求成功退出及预期权限拒绝标记，普通命令失败不计为通过；启动脚本要求Docker 28以上及启用AppArmor，ABI与策略加载仍需部署者核对。当前实验版本要求可信操作者和执行内容，不能承诺执行方与登录态、隧道凭据之间的读取隔离。工作区审阅期间也不保证文件树冻结。

复核命令：`CODEXPP_IMAGE=镜像标签 python3 smoke_phase2.py` 用sudo创建独立卷、host网络和空白Chrome页面，不测试默认bridge；需要已有共享锁与已加载AppArmor。`sudo docker cp scripts/verify_live_mcp.py 容器名:/tmp/verify_live_mcp.py` 后运行 `sudo docker exec 容器名 python3 /tmp/verify_live_mcp.py`，只输出假数据检查结果。验证脚本会保留空的 `.codexpp` 目录，目录白名单若被改动，拒绝测试需结合公开文件读取与搜索成功结果判定。`build_and_test.sh` 的doctor包含公网quick隧道检查，离线或公网限流会失败；用无网络临时容器运行unittest可单独复核纯本地测试。项目源码采用MIT（见LICENSE）；公开镜像分发与跨机部署尚未验收。

任务提交前的浏览器排队/启动失败会取消队列并释放在途记录；如果已经生成提交标记则保留记录，继续按不确定提交流程人工核实。启动命令透传 EXEC_TIMEOUT、MODEL、BROWSER_LIFETIME 和 LOCK_WAIT 对应的 CODEXPP_ 环境变量。

执行崩溃后运行 task status 会在全局执行租约已释放时标记 execution_failed；一次性执行标记保留，须检查残留工作区改动后新建任务。绑定源采用 --mount，宿主重启源路径缺失时拒绝自动建目录。

容器停止时由tini进程组转发信号，supervisor请求浏览器正常退出；宿主启动脚本设置75秒停止宽限。提交失败仅清理本次启动的浏览器，清理失败保留原始错误并提示人工关闭，不终止已存在的人工会话。Docker挂载路径拒绝CSV分隔字符。源码采用MIT，第三方许可单独保留。
