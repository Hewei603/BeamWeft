# BeamWeft 工作台 · Windows 试用

把软件 ZIP 完整解压到自己有写入权限的本地文件夹。安装 Windows 版 Python 3.11 或更高版本，双击同一目录中的 **Launch-BeamWeft.bat**。首次启动会联网安装 Python 依赖，请等到显示工作台地址。界面已随包构建，在接收电脑上可以直接运行。需要安装 Python 时可从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/) 获取。

## 做第一个实验

1. 浏览器打开的是本机工程库。新建工程，只填写名称就可以进入。在光路工作台手工放置元件或打开自带示例，选中元件修改位置、曲率等参数，运行相应计算。未配置模型时也可完成这些步骤。
2. 保存想要保留的光路版本。在工作台将工程保存为 **.lclaw** 文件，选一个方便找回的位置。这份文件可以交给另一位安装了工作台的实验者。要重开，可将它拖到 **Launch-BeamWeft.bat** 上。打开后可以核对元件、光路版本和该工程已经保存的工具结果。
3. 要让 Agent 接手，可从首页 **Agent 配置** 或工程对话的 **模型设置** 选择自己的服务、模型及可用推理档位，测试连接。在工具记录中可查看实际调用、输入、结果和未满足的条件。

## 实验资料存在哪里

启动窗口会显示实际使用的资料库位置。默认位置是当前用户目录下的 **LaserClaw-Data**，沿用这一目录可以找回旧版本的实验。工程库包含数据库、上传原件和实验结果。个人备份请在关闭工作台服务后复制整个资料库。单个项目的交换请用工作台保存的 **.lclaw** 文件。

用另一目录开始独立试验时，可以在 PowerShell 设置 `$env:LASERCLAW_DATA_DIR` 后从同一个窗口运行启动器。例如已有库正在使用默认端口时，可独立启动另一套。

```powershell
$env:LASERCLAW_DATA_DIR = Join-Path $env:LOCALAPPDATA 'BeamWeft-Trial'
.\Launch-BeamWeft.bat --api-port 8010 --ui-port 5180
```

若首次安装中断，请查看启动窗口的具体错误，检查网络和目录写入权限后重新启动。如果报告端口正在被其他资料库占用，请先保存正在进行的实验，再关闭原服务或另选空闲端口。遇到界面文件损坏，可将完整软件包解压到另一个新目录并重新启动。工程资料库单独保存。

工作台默认使用本机 SQLite 和内置检索。需要 PostgreSQL 或 Chroma 的部署可另行安装 `backend/requirements.txt` 并配置相应服务。软件本身的许可在根目录的 LICENSE，预编译界面中依赖的许可在 frontend/dist/THIRD-PARTY-LICENSES.md。
