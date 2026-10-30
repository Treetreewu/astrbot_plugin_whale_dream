# Whale Dream · 做梦与长期成长

用于 AstrBot 的空闲记忆整理插件。按会话和人格整理有原文证据的成员事实、共同经历与暂时想法；通过多次真实互动反馈，逐步形成可修正的行为习惯。

**当前为固定版本兼容的实验版本，尚未完成独立安装后的完整容器验收。** 请求与发送事件使用 AstrBot 公开插件 API；LivingMemory 存储、消息采集、调度和关闭仍使用固定版本兼容桥，不能宣称支持任意版本的 LivingMemory。

## 功能

- 默认空闲 20 分钟后入选，每 5 分钟检查；按北京时间每天最多 2 轮、轮次间隔至少 6 小时，每轮最多 5 个会话。
- 引用真实成员原文，排除引用、转发、机器人输出；隔离 bot、会话、人格和成员。
- 成长需要独立互动和实际已发送回复后的反馈；单次模型生成不会直接晋升长期习惯。
- 使用原有模型与 embedding、原生记忆存储及私有 SQLite journal；不提供 QQ sender，不新增机器人账号或公开 HTTP 服务。
- 幂等投影、删除失效、版本回退和中断重放；线程安全 journal 支持宿主的提示词线程池。
- 热卸载时停止任务并恢复实例方法，重载时检查停用期间的原生记忆修改。

## 兼容版本

| 组件 | 核对基线 |
| --- | --- |
| Python | 3.11+；实际宿主仍须满足 AstrBot 自身要求 |
| AstrBot | 4.29.3 / `f61a7f42696da6df25dc7a249ec9b8d54ed9da18` |
| LivingMemory | **gongzhudeng 分支** 2.7.0-gd.12 / `c486ed65c7d072344c698f83359098c1d4293c8d` |
| MaiBot（可选兼容材料） | 1.3.18 / `2a9af62cec37035d29237acbe2cf3adceb700b27` |

这里的 LivingMemory 是特定分支，不是另一个仓库的同名最新版。绑定会核对插件版本、目录和激活/就绪状态。安装前还应使用下面的源码预检，拒绝带旧做梦 `main.py` hook 或其他未核对修改的安装。

## 安装准备

1. 备份并停止 AstrBot；保留原有 LivingMemory 数据、配置、人格、会话和 Provider ID。
2. 安装上述固定版 LivingMemory。若原部署通过只读挂载覆盖 LivingMemory `main.py`，先移除旧覆盖，恢复该提交原版 `main.py`，不能同时启用旧接入与本插件。
3. 准备原生整合的人格隔离补丁。即使做梦插件没有加载，这个补丁也保护 LivingMemory 的普通整合路径：

   ```bash
   # 在本插件仓库中，仅检查/应用固定版 LivingMemory 的补丁，不启动服务。
   git -C /path/to/astrbot_plugin_livingmemory apply --check /absolute/path/to/astrbot_plugin_whale_dream/compatibility/patches/dream-livingmemory.patch
   git -C /path/to/astrbot_plugin_livingmemory apply /absolute/path/to/astrbot_plugin_whale_dream/compatibility/patches/dream-livingmemory.patch
   python scripts/check-livingmemory.py --plugin-dir /path/to/astrbot_plugin_livingmemory
   ```

4. 将本仓库放在 AstrBot 的 `data/plugins/astrbot_plugin_whale_dream`，或在插件管理中通过本仓库 URL 安装。入口与共享核心在同一仓库，使用相对导入，不需要 `/opt/whale`、外部 `PYTHONPATH` 或另一个仓库。
5. 在 LivingMemory 中明确设置 `provider_settings.llm_provider_id`，使用已配置的模型 Provider。做梦不会创建、重命名或切换 Provider。若会话使用显式插件允许列表，同时包含 LivingMemory 和 `astrbot_plugin_whale_dream`。
6. 在隔离环境完成加载、重载、消息采集、投影/删除和实际发送反馈验收，再按自己的部署流程启用。插件加载成功或单元测试通过，不等于完整联调已经完成。

插件不新增 pip 依赖；所需模型、数据库与 embedding 能力由上述宿主和 LivingMemory 提供。

## 配置与数据

默认配置是 [`whale_dream/default_config.json`](whale_dream/default_config.json)。可用环境变量 `WHALE_DREAM_CONFIG` 指向私有 JSON 文件；显式指定的文件不存在或格式错误时会报错，不会静默回退。共享配置中的 `bots.maibot` 仅用于可选 MaiBot 接入，加载 AstrBot 插件不会启动 MaiBot。

AstrBot journal 仍使用 `StarTools.get_data_dir("astrbot_plugin_livingmemory")/whale-dream/state.sqlite3`，因此从原接入迁移时可保留证据、额度、投影 ID 和成长版本。这个兼容路径是有意保留的，卸载做梦插件不会删除数据。原生模型和 embedding 调用可能产生费用。

正常卸载保存仅含 ID 和哈希的原生记录检查点。重新加载时，停用期间的编辑/删除会失效相关证据；若上次异常退出，无法证明修改没有遗漏，会保守地使旧证据失效并建立历史截止点。原数据与版本保留用于私密管理。

从本仓库目录运行本地管理命令（不调用模型、不发送消息）：

```bash
python -m whale_dream --bot astrbot --state /path/to/state.sqlite3 status
python -m whale_dream --bot astrbot --state /path/to/state.sqlite3 dry-run
python -m whale_dream --bot astrbot --state /path/to/state.sqlite3 freeze
```

其他命令：`unfreeze`、`disable`、`enable`、`versions`、`rollback --record ID --version N`。不存在的 journal 会被拒绝，不会创建假运行状态。`versions` 会显示私人内容，不要把输出或数据库上传到仓库。

## 验证

核心与生命周期回归不需要安装 AstrBot，也不需要账号或网络：

```bash
python -m unittest discover -s tests -v
```

如已准备固定上游 Git 克隆（目录分别为 `AstrBot`、`livingmemory`、`MaiBot`），还可核对真实装饰器与兼容方法，并重建补丁文件：

```bash
python scripts/check-dream-plugin-api.py --source-root /path/to/upstream
python scripts/prepare-compatibility.py --source-root /path/to/upstream
```

重建输出在忽略的 `.local/dream-build/`，不会写入已安装的宿主或私人数据。源码契约检查也不会启动机器人。CI 只跑离线测试。

## MaiBot 边界

共享包包含 `whale_dream.maibot`，并保留 `compatibility/patches/dream-maibot.patch` 供固定版本集成。MaiBot 的公开 SDK 当前缺少等价的幂等记忆写入、独立投影 ID、归档和删除通知接口，因此它**不是可直接安装的 MaiBot SDK 插件**。需要宿主补丁及让宿主可导入本仓库中的 `whale_dream` 包；不要把 AstrBot 的 `main.py` 放入 MaiBot 插件目录。

## 许可与来源

AGPL-3.0-or-later，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。兼容补丁的上游上下文保留其原许可；MaiBot 的 GPL-3.0 文本在 [LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt)。发布修改后的上游构建产物时，应保留完整对应源码、补丁及许可证。

本项目从 `whale-qqbot` 的做梦实现独立提取，使用全新的 Git 历史；不包含 QQ 凭据、服务器配置、素材、聊天、记忆数据库或其他运行数据。
