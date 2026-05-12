# Codex History Keeper

这是一个安全版的 Codex 本地历史恢复工具。

它不会修改 `auth.json`、cookie、账号令牌或正在运行的 SQLite 数据库。它做的事情是把本机 `~/.codex/sessions`、`~/.codex/archived_sessions`、`session_index.jsonl` 里的可读对话导出到：

`C:\Users\honghua\Documents\CodexHistoryVault`

## 打开界面

```powershell
python .\tools\codex_history_keeper.py
```

更推荐使用新的专用修复界面：

```powershell
python .\tools\codex_history_repair_gui.py
```

也可以双击：

`C:\Users\honghua\Documents\New project 4\tools\open_codex_history_repair_gui.cmd`

我已经创建了桌面图标：

`C:\Users\honghua\Desktop\Codex History Repair.lnk`

界面里可以：

- 备份本地历史对话
- 搜索旧对话
- 打开最新导出的 `index.md`
- 创建桌面启动器：先刷新历史，再启动 Codex
- 安装或移除 Windows 登录后的自动同步
- 检查或修复 Codex 左侧列表使用的本地 UI 索引

## 每次打开 Codex 前自动刷新

运行一次：

```powershell
python .\tools\codex_history_keeper.py --install-launcher
```

之后从桌面的 `Codex History Launcher` 打开 Codex。它会先把本机历史备份到可读目录，再启动 Codex。

新版启动器还会在启动前检查 UI 索引：如果 `state_5.sqlite` 损坏、缺少本地会话，或旧会话 provider 与当前登录方式不一致，会先自动重建索引再启动。

## 切换登录方式后怎么用

如果新登录方式的左侧历史列表看不到旧会话，打开对话备份目录里的：

`_reuse_this_history_in_codex.md`

把里面的路径告诉新会话，或直接让 Codex 搜索这个备份目录。旧会话的 Markdown 文件在 `conversations` 目录里。

## 左侧列表仍然没有旧对话

如果 Codex 左侧项目里仍显示“暂无对话”，先在界面里点 `检查 UI 索引`。如果检查通过，完全退出 Codex，然后在外部 PowerShell 里运行：

```powershell
python "C:\Users\honghua\Documents\New project 4\tools\codex_history_keeper.py" --repair-ui-index --apply-repair
```

也可以直接双击：

`C:\Users\honghua\Documents\New project 4\tools\repair_codex_ui_index.cmd`

默认会自动读取当前 Codex 配置里的 provider。官方账号登录通常是 `openai`；API Key 模式会读取 `model_provider`，例如 `my_codex`、`sub2api` 等：

```powershell
python "C:\Users\honghua\Documents\New project 4\tools\codex_history_keeper.py" --repair-ui-index --apply-repair --provider-mode current
```

如果需要手动指定某个 API provider，可以把最后的 `current` 换成实际名称，例如 `my_codex`。

这个命令会：

- 从 `~\.codex\sessions` 和 `~\.codex\archived_sessions` 重建 `state_5.sqlite`
- 将会话 JSONL 里的 `session_meta.model_provider` 统一到当前可见 provider，避免 Codex 重启后再次回填成旧 provider
- 重建 `session_index.jsonl`
- 在 `~\.codex\history_sync_backups` 里保留修复前备份
- 默认把旧会话的 `model_provider` 统一成当前配置正在使用的 provider，避免切换账户/API 后被过滤

如果想保留原来的 provider 元数据，运行：

```powershell
python "C:\Users\honghua\Documents\New project 4\tools\codex_history_keeper.py" --repair-ui-index --apply-repair --provider-mode preserve
```
