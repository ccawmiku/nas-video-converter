# NAS Video Converter

面向群晖 NAS 的“先扫描、后确认、再转换”视频管理服务。它在 Docker 中提供中文网页，使用 FFprobe/FFmpeg 检测媒体、生成处理计划、无损换封装或将普通 SDR 8-bit 视频编码为 H.264。

> [!WARNING]
> 本项目没有登录系统，只能部署在可信局域网。不要把 12012 端口直接暴露到公网。

## 安全边界

- 不提供删除 API，媒体处理代码不使用永久删除操作。
- 失败、取消和中断输出不会清理；可识别的临时输出会通过同文件系统改名保存到 `转换失败输出`。
- 源文件仅在输出通过 FFprobe、首/中/尾抽样解码和结束时长检查后，通过同文件系统 rename 移到 `转换前原文件`。
- 不用“复制后删除”模拟跨文件系统移动，检测到跨文件系统会拒绝。
- Linux 使用 `renameat2(RENAME_NOREPLACE)`；目标存在时拒绝覆盖。已有同名 MP4 时使用 `文件名 (2).mp4`、`文件名 (3).mp4`。
- 媒体根目录、每级路径、真实路径和符号链接都经过校验；排除 `@eaDir`、`#recycle`、快照、备份与失败目录。
- 服务重启会把运行中任务标为“中断”，只报告遗留临时文件，不擅自移动、覆盖或删除。

即使如此，任何会写入重要媒体库的工具都应先在测试目录验证，并保持独立备份。

## 快速启动

```bash
sudo mkdir -p /volume2/docker/nas-video-converter/config
docker compose up -d
```

打开 `http://NAS-IP:12012`。默认自动换封装、自动转码、定时任务和监控全部关闭。手动流程为：

1. 选择映射根目录，完整扫描。
2. 查看分类、轨道、完整性与原因。
3. 多选可处理文件，确认计划。
4. 选择质量档位后，正式运行。

处理成功后的目录示例：

```text
/media/视频库/电影/示例.mp4
/media/视频库/转换前原文件/电影/示例.mkv
```

## 分类规则

| 分类 | 行为 |
|---|---|
| 无需转换 | 已是 MP4，只做完整性检测 |
| 可无损换封装 | 非 MP4，所有轨道可原样写入 MP4，使用 `-map 0 -c copy` |
| 需要重新编码 | 普通 SDR 8-bit 视频转 H.264；兼容的其他轨道原样复制 |
| 无法处理 | DTS/TrueHD、PGS、附件或其他不兼容轨道需要改变，因安全规则不处理 |
| 跳过 | HDR、杜比视界、10-bit、损坏、变化中或未稳定文件 |

转码保留分辨率和帧率，使用 preset medium，不设置 maxrate 和输出大小上限。软件 `libx264` 使用 `yuv420p` 和 CRF 16 / 18（默认）/ 20；可选 Intel QSV 使用 `h264_qsv`、`nv12` 和 `global_quality` 16 / 18 / 20。输出变大仍完成并显示黄色警告，不自动再转第二次。

## 完整性与进度

扫描执行 FFprobe 结构分析以及开头、中间、结尾抽样解码。异常文件可在网页发起完整严格解码。缓存键包含真实路径、大小和纳秒修改时间。任务通过 SQLite 持久化；SSE 支持浏览器断线重连。

FFmpeg 操作使用 `-progress pipe:1` 的真实输出展示阶段、文件/整体百分比、数量、体积、已用时间、ETA、speed、输出时间和总时长；后端每 0.2 秒检查并通过 SSE 转发新进度。网页顶部和实时任务区域会显示当前实际使用的编码后端（Intel QSV、libx264 或流复制）。暂停/继续在 Linux 上使用进程信号；取消会终止 FFmpeg 并保存已产生的临时输出。

## 群晖部署

参见 [docs/synology.md](docs/synology.md)。容器只发布 `linux/amd64`，不支持 ARM 群晖。默认进程身份为 `1026:100`，可通过 `PUID`/`PGID` 覆盖。数据库和设置位于 `/config/nas-video-converter.db`。主 Compose 已为 N95 映射 `/dev/dri/renderD128`；启动脚本会把容器用户加入该设备的宿主机数值 GID，网页默认自动检测 QSV，不可用时回退到软件编码。

## 本地开发与测试

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
MEDIA_ROOT=/path/to/test-media CONFIG_DIR=/path/to/config uvicorn app.main:app --port 12012
pytest
```

测试覆盖路径安全、符号链接、同名输出、分类、SQLite 恢复、真实 FFmpeg 扫描/换封装/转码和源文件保留。CI 还构建并启动真实容器、调用 API、FFprobe 输出、解码输出帧，并验证没有覆盖已有文件。

## API 摘要

- `GET /api/roots`、`GET /api/stats`、`GET /api/files`
- `POST /api/scans`：完整扫描与计划统计
- `POST /api/plans`：确认选中文件及动作
- `POST /api/conversions`：仅接受已确认计划
- `POST /api/verifications`：抽样或完整严格检测
- `POST /api/recovery/verifications`：只读验证中断遗留的临时输出
- `GET /api/events`：SSE 进度
- `POST /api/jobs/{id}/pause|resume|cancel`
- `GET/PUT /api/settings`、`GET /api/hardware`、`GET /api/automation`
- `GET /api/jobs`、`GET /api/conversions`、`GET /api/logs`、`GET /api/recovery`

OpenAPI 文档位于 `/docs`（同样只应在可信局域网访问）。
