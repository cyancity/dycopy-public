# 故障定位

恢复前先检查已有媒体、逐字稿和 SRT，保留已完成阶段，只重试失败部分。

## 浏览器启动失败

`Executable doesn't exist` 通常表示当前 Python 环境缺少 Playwright 对应的 Chromium。先核对解释器，再运行该解释器的 `-m playwright install chromium`。

Windows 的 `PermissionError: [WinError 5]` 或 Node 的 `EPERM` 可能来自宿主沙箱对命名管道的限制。先检查调用栈和当前宿主权限，不要因此导出 Cookie、修改整个用户目录的 ACL、关闭系统防护或重复重装依赖。确需更高权限时遵守当前宿主的审批机制。

用现有虚拟环境运行以下离线烟测，不访问抖音、不下载媒体：

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content("<title>dycopy browser ready</title>")
    print(page.title())
    browser.close()
```

## 详情为空或主页数量不足

HTTP 分享页没有 SSR 数据时，CLI 会尝试浏览器通道。先看浏览器最终结果，不把 HTTP 通道失败当作作品已删除。

浏览器也可能受到登录、限流或页面变更影响。核对该作品在用户有权使用的页面中是否可见。取回数量少于页面显示数量时保留差额，不把部分列表标为全量，也不提高并发反复请求。

## ASR 没有输出

首次运行包含模型下载和推理两个阶段。检查 Python 进程是否仍在计算、模型缓存是否齐备，以及当前作品是否产生输出。faster-whisper 默认使用 CPU，静默数分钟本身不能证明卡死。

用 Python `-u` 或 `PYTHONUNBUFFERED=1` 保留实时输出。不要在长任务末尾接 `Select-Object -Last`，它会等进程结束才输出；保存日志时可用 `Tee-Object`。

进程退出后核对本次文件才能确认完成。文件可能是旧产物，应检查作品 ID、修改时间和本次日志。不要为引用几句话重跑整片转写或全片 OCR。

## 服务与状态库

8788 只供扩展使用，CLI 无需启动服务。连接失败时检查服务是否运行和端口是否一致，不停止来源不明的占用进程。

同步脚本和服务共用状态库时不能同时运行。扩展重启不会清空已登记作品，删除媒体也不会清除 ID。人工调整前先备份状态库，不删除整个状态库来重试一条失败作品。
