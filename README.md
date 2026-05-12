# Codex History Repair

`Codex History Repair` 是一个给 Windows 用户用的本地工具，解决两件事：

1. 把本机里的 Codex 对话历史导出成可读备份。
2. 修复 Codex 左侧历史列表看不到旧对话的问题。

它适合这些情况：

- 你切换了账号、登录方式或 provider 以后，Codex 左侧历史列表变空了
- 你知道旧对话文件还在电脑里，但 UI 不显示
- 你想先把本地历史备份成 Markdown / JSON，再慢慢排查

## 先记住两件事

1. **先备份，再修复。**
   备份不会改你的 Codex，本地修复会改本地 UI 索引。

2. **普通用户最推荐直接双击这个文件：**
   [`open_codex_history_repair_gui.cmd`](./open_codex_history_repair_gui.cmd)

## 这个工具不会做什么

它不会：

- 迁移云端账号历史
- 修改 `auth.json`
- 复制 token、cookie、登录凭据
- 承诺不同账号之间自动共享官方历史

它做的是：

- 从你电脑里的 `~/.codex` 读取本地历史
- 导出可读备份
- 必要时重建本地历史列表索引

## 最简单的使用方法

### 第 1 步：打开工具

双击：

[`open_codex_history_repair_gui.cmd`](./open_codex_history_repair_gui.cmd)

如果你的电脑还没有合适的 Python，或者没有 `tkinter`，启动器会自动检测，并尽量提示你自动安装。

### 第 2 步：先备份

打开 GUI 后，先点击：

`备份对话`

备份完成后，再点击：

`查看备份`

确认能看到这些文件：

- `index.md`
- `index.json`
- `searchable_messages.jsonl`
- `conversations/*.md`
- `conversations/*.json`

默认备份位置：

`%USERPROFILE%\Documents\CodexHistoryVault`

### 第 3 步：看诊断结果

点击：

`刷新诊断`

现在界面会给出一个很明显的状态提示：

- 绿色：`看起来正常`
- 红色：`需要修复`
- 蓝色：正在诊断 / 正在修复
- 红色或橙色：失败 / 警告

### 第 4 步：如果需要修复，就按提示处理

如果诊断显示 `需要修复`，有两种常用方式。

#### 方式 A：Codex 已经关掉了

直接点击：

`立即修复`

修复完成后，再打开 Codex 看左侧历史是否恢复。

#### 方式 B：Codex 还开着

你有两种选择：

1. 先关掉 Codex，再点 `立即修复`
2. 先点 `关闭后自动修复`，再去关闭 Codex

第二种适合不想自己卡时间的人。工具会等 Codex 完全退出后自动修复，然后再重新打开 Codex。

## GUI 里每个按钮是做什么的

- `刷新诊断`
  检查本地 UI 索引、provider、SQLite 状态

- `立即修复`
  立刻重建本地 UI 索引

- `关闭后自动修复`
  先进入等待状态，等你关闭 Codex 后再自动修复

- `备份对话`
  导出本地历史为可读备份

- `查看备份`
  打开最近一次备份目录

- `创建桌面图标`
  在桌面创建 GUI 快捷方式

## 什么时候必须关闭 Codex

下面这些操作前，**必须先让 Codex 完全退出**：

- `立即修复`
- 双击 [`repair_codex_ui_index.cmd`](./repair_codex_ui_index.cmd)
- 运行 `--repair-ui-index --apply-repair`

因为这些操作会替换本地 `state_5.sqlite`。

下面这个操作不要求你先手动关闭：

- `关闭后自动修复`

因为它本来就是专门等 Codex 关闭后再执行。

下面这个操作通常不需要关闭 Codex：

- `备份对话`

## 修复时到底会改什么

修复主要会重建这些本地文件：

- `state_5.sqlite`
- `session_index.jsonl`
- `.codex-global-state.json`（如果存在）

默认修复模式下，工具还会把旧会话里的 `model_provider` 调整到**当前正在使用的 provider**，这样旧会话更容易重新显示在当前 UI 里。

这是这个项目的设计行为，不是意外副作用。

修复前会先自动备份原始状态文件。

默认备份位置：

`%USERPROFILE%\.codex\history_sync_backups`

## 如果没有 Python 或没有 tkinter

最推荐还是直接双击：

[`open_codex_history_repair_gui.cmd`](./open_codex_history_repair_gui.cmd)

它会按这个顺序处理：

1. 查找 Python 3.11+
2. 检查该 Python 是否带 `tkinter`
3. 如果缺失，询问你是否自动安装官方 Python 3.13
4. 安装完成后再次尝试启动 GUI

安装时会优先尝试 `winget`。

如果系统没有 `winget`，或者 `winget` 安装失败，启动器会继续从 Python 官网自动下载官方 Windows 安装器并静默安装。

只有在自动下载本身失败时，你才需要检查网络或稍后重试。

## 推荐使用顺序

### 情况 1：你只想先把历史保住

1. 双击打开 GUI
2. 点击 `备份对话`
3. 点击 `查看备份`
4. 确认 `index.md` 和 `conversations` 已生成

### 情况 2：左侧历史列表空了

1. 先 `备份对话`
2. 再 `刷新诊断`
3. 如果提示 `需要修复`
4. 关闭 Codex
5. 点击 `立即修复`
6. 重新打开 Codex 检查结果

### 情况 3：你不在乎左侧列表，只想继续看旧内容

直接打开备份目录里的：

- `index.md`
- `conversations/*.md`
- `searchable_messages.jsonl`

就可以继续搜索、阅读和引用旧对话。

## 高级用法（命令行）

先进入项目目录：

```powershell
Set-Location "<项目目录>"
```

查看帮助：

```powershell
py -3 .\codex_history_keeper.py --help
```

只导出备份：

```powershell
py -3 .\codex_history_keeper.py --sync
```

关闭 Codex 后直接修复 UI 索引：

```powershell
py -3 .\codex_history_keeper.py --repair-ui-index --apply-repair
```

如果你明确想保留原始 provider，不统一到当前 provider：

```powershell
py -3 .\codex_history_keeper.py --repair-ui-index --apply-repair --provider-mode preserve
```

## 常见问题

### 1. 修复后左侧还是看不到旧对话

先检查这几件事：

1. `~/.codex/sessions` 或 `~/.codex/archived_sessions` 里是否真的还有旧 JSONL
2. 修复前是否已经做过一次备份
3. 修复时 Codex 是否完全关闭
4. 当前 provider 是否和你想显示的历史一致

### 2. PowerShell 里看到中文乱码

这通常是终端编码问题，不一定是文件坏了。

优先直接看 GUI，或者直接打开导出的 `index.md` / `conversations/*.md`。

### 3. 只想恢复内容，不想动本地索引

那就不要修复，直接备份，然后查看导出的 Markdown 文件即可。

## 项目文件说明

- [`open_codex_history_repair_gui.cmd`](./open_codex_history_repair_gui.cmd)
  普通用户首选入口

- [`repair_codex_ui_index.cmd`](./repair_codex_ui_index.cmd)
  关闭 Codex 后，一键执行修复

- [`repair_after_codex_closes.ps1`](./repair_after_codex_closes.ps1)
  等 Codex 关闭后自动修复并重新打开

- [`codex_history_repair_gui.py`](./codex_history_repair_gui.py)
  图形界面

- [`codex_history_keeper.py`](./codex_history_keeper.py)
  核心逻辑
