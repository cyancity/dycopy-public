# dycopy

从抖音分享链接提取作品文案和元数据，下载视频、图文原图与 Live Photo，并按需生成口播逐字稿和 SRT 字幕。提供 Python 命令行、本机串行队列服务和 Chrome 扩展。

这是非官方工具，与抖音及其运营方没有隶属、合作或背书关系。平台接口变化、登录限制和风控可能导致部分功能不可用。

## 安装

需要 Python 3.10 或更新版本。解压源码包后进入包含 `dycopy.py` 的目录。下载与队列功能使用核心依赖；本地转写还需要 ffmpeg 和可选 ASR 依赖。

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe dycopy.py --help
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python dycopy.py --help
```

本地 Whisper 转写另装 `requirements-asr.txt`，并确认 `ffmpeg -version` 可运行。首次转写会下载模型，默认从 `hf-mirror.com` 获取，缓存于 `~/.cache/dycopy/models/`。

下文的 `python` 均指上述虚拟环境解释器。Windows 使用 `.\.venv\Scripts\python.exe`，macOS / Linux 使用 `.venv/bin/python`。示例中的作品链接、主页链接需要替换为你有权处理的实际输入。

## 提取作品

```bash
python dycopy.py "作品链接"
python dycopy.py "作品链接" --download
python dycopy.py "作品链接" --asr faster-whisper --download --srt
python dycopy.py links.txt --download --media-only --out downloads
python dycopy.py profiles.txt --download --media-only --role-folders --out downloads
```

支持长链、短链、分享口令和裸作品 ID。文本清单每行一个链接，空行和 `#` 开头的注释会跳过。图文作品保存图片；视频支持选择无水印播放流，兜底会将 `playwm` 地址改为 `play`。这不代表平台或作者授权了下载、去标识或再次传播。

CLI 默认输出到当前目录的 `result/`，每个作品一个目录。用 `--out` 指定输出根目录。`--download` 启用媒体下载，`--asr` 启用转写。ASR 也会下载并保留供转写使用的视频；明确需要视频和字幕时可同时传入两项。

CLI 也接受本地 JSON 作品数组、包含 `items` 的对象或分组数组。这类文件可能包含账号和签名媒体地址，不要上传到仓库。

## 批量归档与增量同步

```bash
python sync_role_library.py --profiles profiles.txt
python sync_role_library.py --profiles profiles.txt --works works.txt
```

主页清单每行一个主页 URL 或 `sec_uid`。补录清单每行一个作品链接，需带作者 `secUid=` 或 `/user/<sec_uid>` 以便归到正确作者。同步脚本与服务默认使用源码根目录下的这些位置：

| 数据 | 默认位置 |
| --- | --- |
| 主页清单 | `.local/dycopy/profile_sources.txt` |
| 已处理作品与失败记录 | `.local/dycopy/processed_aweme_ids.json` |
| 可选旧归档导入文件 | `.local/dycopy/legacy_archive.json` |
| 下载媒体 | `downloads/` |

用 `--profiles`、`--state`、`--output` 和同步脚本的 `--legacy-archive` 覆盖路径。首次同步使用你创建的 `profiles.txt`；默认主页清单也会由服务入队操作创建。

按 `aweme_id` 去重，成功下载后登记 ID。即使手动删除媒体，已登记作品仍跳过。失败作品可重试；Live Photo 动态片段失败不会影响已保存的静态图。下载严格串行。

匿名列表可能只返回主页的部分作品。核对页面作品数、取回数和分页结束状态，数量不一致时不能当作全量。浏览器扩展可以使用当前已登录的页面读取列表。

**不要同时运行使用同一状态文件的同步脚本和本机服务。** 多个写入进程会覆盖彼此状态。并行实例需使用独立的状态文件和输出目录。

## Chrome 扩展

1. 在源码目录运行 `python dycopy_service.py`，看到 `http://127.0.0.1:8788` 的启动提示。
2. 在 Chrome 的 `chrome://extensions` 开启开发者模式，加载仓库的 `extension/` 文件夹。
3. 打开抖音主页并按需登录，点击扩展。可加入单个主页、批量导入、抓取全部作品或补录当前作品。

“更新全部已登记主页”按主页清单串行处理。扩展安装后会注册每日检查，需 Chrome 开启、可用登录态和本机服务同时存在；下载队列忙时跳过更新。停止使用时可禁用扩展以停止检查。

服务仅监听 `127.0.0.1`，检查 Host 和 Origin，没有配对密码，并允许 Chrome 扩展来源。只在信任的本机环境使用，不要转发到公网或作为多用户服务器。更改 `--port` 后还需同步修改扩展中的服务地址和权限。

服务停止后内存队列不会保留，作品状态仍在磁盘。重启后重新入队即可按 ID 跳过已完成作品。CLI 不需要启动 8788 服务。

## 转写与数据流

| 模式 | 音频去向 | 补充说明 |
| --- | --- | --- |
| `--asr faster-whisper` | 本机推理 | 另装 `requirements-asr.txt`；用 `--model` 选择模型或本地模型目录 |
| `--asr sensevoice` | 本机推理 | 另需 FunASR、ModelScope、PyTorch、torchaudio；会加载上游模型代码，不含在核心安装中 |
| `--asr siliconflow` | 上传到 SiliconFlow | 使用自己的账号和 Key，费用与数据处理遵循其条款 |

下载访问抖音及其媒体 CDN，模型首次获取也需要联网。“本地转写”指推理发生在本机，不代表整个流程离线。CLI 新建无状态浏览器；扩展在抖音页面内使用现有登录态，将作品对象交给本机服务。

云转写优先用 `SILICONFLOW_API_KEY` 环境变量配置 Key。仍兼容 `--api-key`，但命令行参数可能进入终端历史或被其他本机进程看到。不要提交 Key、Cookie、浏览器资料、状态库和真实下载记录。

## 验证

核心依赖安装后，以下检查不需要登录抖音、真实账号或下载媒体：

```bash
python tests/test_offline.py
python tests/test_service_e2e.py
node tests/test_extension.mjs
```

最后一项需要 Node.js，验证扩展脚本在独立页面环境中的注入与分页逻辑。测试使用合成数据和临时目录，不读取个人素材库。通过这些检查不代表抖音在线接口一定可用。

供 AI 助手使用的工作流在 [skills/dycopy/SKILL.md](skills/dycopy/SKILL.md)，故障定位见 [troubleshooting.md](skills/dycopy/references/troubleshooting.md)。

## 使用边界与许可证

仅处理你有权访问和使用的内容，遵守平台规则、著作权和个人信息保护要求。内容公开可见不代表允许批量采集、去标识、再分发或商用。不要共享登录态或获取无权访问的内容。

代码按 [MIT License](LICENSE) 提供，软件按现状提供。MIT 授权只覆盖代码，不授予平台数据、音视频、肖像、音乐或第三方模型的权利，也不能排除法律强制规定的责任。第三方依赖和模型遵循各自许可证；源码包不包含依赖、模型权重或 ffmpeg 二进制文件。
