<h1 align="center">Pack2Serve</h1>

<p align="center">
  导入一个 Minecraft 整合包文件，一键生成可直接运行的专用服务器文件夹，并在浏览器里启动开服和集中管理。最快路径下，Pack2Serve 可以从“只有整合包文件”到“服务器已启动”约 2 分钟完成。
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="#截图">截图</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#功能">功能</a> ·
  <a href="#状态与限制">状态</a>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-205A4B?style=for-the-badge&logo=python&logoColor=white" />
  <img alt="Minecraft" src="https://img.shields.io/badge/Minecraft-server%20panel-1F7A64?style=for-the-badge" />
  <img alt="Modpacks" src="https://img.shields.io/badge/Modrinth%20%2B%20CurseForge-imports-6B7FD7?style=for-the-badge" />
  <img alt="Status" src="https://img.shields.io/badge/status-active%20prototype-F0B429?style=for-the-badge" />
</p>

<p align="center">
  <img src=".github/assets/readme-hero-unified.svg" alt="Pack2Serve 项目简介图" />
</p>

## 截图

<table>
  <tr>
    <td colspan="2" align="center">
      <img src=".github/assets/screenshots/dashboard-projects.png" alt="Pack2Serve 项目首页、服务器卡片和创建进度" />
      <br />
      <strong>项目首页。</strong> 导入整合包、查看构建与验证进度、复制连接地址，并启动或停止服务器。
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/runtime-overview.png" alt="运行总览，显示连接地址、运行时长、磁盘和内存指标" />
      <br />
      <strong>运行总览。</strong> 查看运行状态、运行时长、世界大小、项目占用、内存占用和连接地址。
    </td>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/live-console.png" alt="实时 Minecraft 服务器日志控制台和命令输入框" />
      <br />
      <strong>实时控制台。</strong> 在浏览器里查看服务器日志，并发送 Minecraft 控制台命令。
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/player-management.png" alt="在线玩家管理面板，显示玩家位置和操作按钮" />
      <br />
      <strong>玩家管理。</strong> 刷新玩家状态、查看坐标，并发送常用管理命令。
    </td>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/server-properties.png" alt="服务器参数编辑器，包含中文字段和原始 server.properties 文本编辑" />
      <br />
      <strong>服务器参数。</strong> 用中文字段编辑常用 server.properties，也可以直接编辑原始配置文本。
    </td>
  </tr>
</table>

## 为什么做它

Minecraft 整合包作为客户端版本很容易启动，但把同一个 `.mrpack` 或 CurseForge 导出的 `.zip` 变成专用服务器文件夹仍然麻烦：远程模组文件要补齐，客户端专用内容要分离，加载器安装器要运行，Java 版本要匹配，EULA 必须明确接受，启动失败还要看得懂日志。

Pack2Serve 面向本地开服者，目标是从一个整合包文件到可进入的服务器项目一条龙完成：

1. 在网页面板里直接选择 `.mrpack` 或 CurseForge 导出的 `.zip`。
2. 点击一次创建，系统解析整合包、下载可解析的远程文件、复制服务器资源、安装 Java 和加载器、在同意后写入 EULA，并执行启动验证。
3. 得到完整的服务器项目文件夹，从项目卡片直接启动服务器，并在浏览器里管理日志、玩家、世界、模组、文件和参数。

对于较小整合包或依赖已经缓存的情况，最快可以约 2 分钟完成从“只有整合包文件”到“服务器已启动”。大型整合包仍会受下载速度、加载器安装和启动验证耗时影响。

## 功能

- 导入 Modrinth `.mrpack` 和 CurseForge 导出的 `.zip` 整合包。
- 解析整合包名称、Minecraft 版本、加载器类型、加载器版本、远程模组条目和 overrides 文件。
- 直接下载 Modrinth 文件，并通过可插拔的无 Key 镜像源解析 CurseForge 文件。
- 将下载结果缓存到 `data/cache`，后续构建复用缓存。
- 生成服务器项目目录，包括 `mods/`、服务器资源、`server.properties`、`eula.txt`、启动脚本和 Pack2Serve 报告。
- 根据 Minecraft 版本安装项目本地 Java 运行时。
- 安装 Fabric、Forge、NeoForge 服务端启动文件，可执行 Forge/NeoForge 安装器。
- 创建完成前执行启动验证，并写入 `pack2serve/validation-report.json` 和 `logs/pack2serve-validation.log`。
- 在网页面板中启动、停止、监控生成的服务器。
- 显示实时日志、命令输入、项目路由、运行状态、磁盘占用、内存占用、世界时间和连接地址。
- 提供中文常用参数表单和原始 `server.properties` 编辑器。
- 从日志和命令探针识别在线玩家，支持 OP、改模式、传送、封禁、杀死、清空背包等常用操作。
- 浏览项目文件、管理世界、备份世界，并查看、启用、禁用、删除模组文件。
- 默认隐藏内部验证/测试项目，避免首页被临时项目刷屏。

## 兼容模型

Pack2Serve 的目标是生成尽量接近“同一个整合包开多人房间”的专用服务器，但 Minecraft 整合包生态并不完全统一。当前生成流程分成几层：

- 整合包解析：识别 Modrinth 和 CurseForge 的文件结构。
- 文件补齐：下载远程文件，无法解析时明确记录人工处理项。
- 服务端过滤：复制服务端资源，并隔离已知客户端专用文件。
- 加载器安装：生成 Fabric、Forge 或 NeoForge 对应的启动链路。
- 启动验证：真实启动服务器，监听日志，记录是否到达 Minecraft 启动完成标志。

`docs/analysis/` 和 `docs/development/` 里包含真实整合包分析和验证记录。

## 快速开始

前置要求：

- Windows、PowerShell、Python 3.10 或更新版本。
- 如果需要下载 Java、加载器或远程模组文件，需要可用网络。
- 一个本地 Minecraft 整合包文件。

启动网页面板：

```powershell
git clone https://github.com/Ha22yX/Pack2Serve.git
cd Pack2Serve
python -m pack2serve.cli serve-panel --host 127.0.0.1 --port 8766 --workspace data
```

也可以使用内置 PowerShell 启动脚本：

```powershell
.\scripts\start-panel.ps1 -HostName 127.0.0.1 -Port 8766 -Workspace data
```

然后打开：

```text
http://127.0.0.1:8766/
```

## 面板流程

1. 点击 `创建项目`。
2. 直接选择 `.mrpack` 或 CurseForge `.zip` 整合包文件。
3. 输入项目名称。
4. 勾选已阅读并同意 Minecraft EULA，并允许系统下载可解析的远程模组文件。
5. 等待 Pack2Serve 生成服务器文件夹：解析、下载、Java 安装、加载器安装、EULA 写入、启动验证和摘要生成都会显示在创建进度里。
6. 打开项目卡片，点击启动，复制连接地址，然后继续在同一个面板里管理日志、玩家、世界、模组、文件和参数。

## CLI 用法

查看整合包信息：

```powershell
python -m pack2serve.cli inspect "C:\path\to\modpack.mrpack"
```

准备完整服务器项目：

```powershell
python -m pack2serve.cli prepare "C:\path\to\modpack.mrpack" --target "data\servers\example" --download --install-java --validate
```

使用内置无 Key 解析源构建 CurseForge ZIP：

```powershell
python -m pack2serve.cli build "C:\path\to\modpack.zip" --target "data\servers\example" --download
```

验证已有服务器项目：

```powershell
python -m pack2serve.cli validate-server "data\servers\example" --timeout 120
```

明确接受 Minecraft EULA：

```powershell
python -m pack2serve.cli accept-eula "data\servers\example" --i-agree
```

## 技术栈

| 层级 | 技术 | 用途 |
| --- | --- | --- |
| 后端 | Python 标准库 | 整合包解析、文件生成、进程控制、HTTP API 和面板服务 |
| 前端 | 服务端输出 HTML、CSS、原生 JavaScript | 无需前端构建步骤的本地浏览器面板 |
| Minecraft 运行时 | Fabric、Forge、NeoForge、Java Runtime | 专用服务器安装与启动 |
| 存储 | `data/` 下的本地文件系统 | 上传整合包、生成服务器、缓存、日志和报告 |
| 测试 | `unittest` | 覆盖解析、构建、面板、验证和进程管理逻辑 |

## 项目结构

```text
pack2serve/                 Python 包
  builder.py                服务器项目生成
  parser.py                 Modrinth 和 CurseForge 整合包解析
  downloader.py             文件缓存和下载源
  installer.py              Fabric、Forge、NeoForge 安装计划和执行
  java.py                   Java 版本检测与便携运行时安装
  validator.py              启动验证和客户端专用模组修复辅助
  panel.py                  面板服务层和服务器进程管理
  web.py                    本地 HTTP 面板和浏览器 UI
docs/                       架构记录、整合包分析和验证报告
scripts/start-panel.ps1     本地面板启动脚本
tests/                      标准库单元测试
data/                       本地运行工作区，已被 git 忽略
```

## 状态与限制

Pack2Serve 仍是活跃原型，已经基于真实 Modrinth 和 CurseForge 整合包持续开发和验证，但仍应把它当成本地工具：每个生成出来的服务器都需要验证，而不是假设所有整合包天然 100% 等价。

已知限制：

- CurseForge 无 Key 模式依赖第三方镜像覆盖率，部分文件仍可能需要人工处理。
- 部分客户端专用模组只有在专用服务器崩溃报告中才能暴露，Pack2Serve 可以隔离一部分已知 invalid dist 情况并重试验证。
- 玩家坐标和朝向可以通过命令探针获取，但稳定读取背包、复活点和更深层玩家状态通常需要 RCON 支持或服务端伴生模组。
- Forge/NeoForge 安装器行为会随 Minecraft 世代和加载器版本变化。
- 当前重点是本地管理面板，不是公网多用户托管平台；鉴权、权限隔离和公网安全不是现阶段核心。

## 开发

运行测试：

```powershell
python -m unittest tests.test_pack2serve_core
```

相关文档：

- `docs/architecture/0001-curseforge-no-key-mirror-strategy.md`
- `docs/development/backend-mvp-status.md`
- `docs/development/startup-verification-2026-07-21.md`

## License

仓库暂未添加许可证文件。
