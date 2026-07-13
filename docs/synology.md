# 群晖 Container Manager 部署

## 前提

- Intel/AMD x86-64 群晖；本镜像不构建 ARM。
- Container Manager（旧版系统称 Docker）。
- 媒体共享目录有足够空间，且容器用户拥有读写和目录创建权限。
- 先在测试媒体目录验证。不要直接把服务暴露到公网。

## 查询权限身份

通过群晖 SSH 运行 `id 用户名`，把得到的 uid/gid 写入 Compose 的 `PUID` 与 `PGID`。当前默认值按 CCAW 用户设置为 `1026:100`。容器入口会修正 `/config` 内数据库文件的所有权，但不会修改媒体目录权限。

## 项目部署

1. 在群晖建立 `/volume2/docker/nas-video-converter/config`，并确保其所有者与 Compose 中的 `PUID:PGID` 一致。
2. 下载仓库的 `docker-compose.yml`，按实际共享目录修改 volumes 左侧路径。
3. 保持媒体卷为 `rw`。每个 `/media` 下一级目录会显示为独立根目录。
4. 在 Container Manager → 项目中从 Compose 创建项目，或 SSH 执行：

   ```bash
   cd /volume2/docker/nas-video-converter
   docker compose pull
   docker compose up -d
   ```

5. 打开 `http://群晖IP:12012`。

## Intel N95 Quick Sync（可选）

Intel N95 带 Intel UHD Graphics，可使用 FFmpeg `h264_qsv` 进行 H.264 硬件编码。硬件加速依赖核显已启用、群晖提供渲染设备，并且容器能够访问设备。先检查：

```bash
ls -l /dev/dri/renderD128
```

如果设备存在，使用附加 Compose 文件启动：

```bash
cd /volume2/docker/nas-video-converter
docker compose -f docker-compose.yml -f docker-compose.intel-qsv.yml up -d
```

Container Manager 的项目界面如果只接受一个 Compose 文件，可把下面内容合并到服务中：

```yaml
devices:
  - /dev/dri/renderD128:/dev/dri/renderD128
```

入口脚本会读取设备的实际组 ID，并把容器用户加入对应组。网页“转码硬件加速”默认是“自动”：QSV 可用时使用 `h264_qsv`，否则回退到 `libx264`；也可以强制软件或强制 QSV。强制 QSV 不可用时任务会明确失败，原文件不会移动。

软件编码的 16/18/20 是 CRF；QSV 的 16/18/20 是 `global_quality`（ICQ），数值越小质量越高，但两种编码器的数值不能视为完全相同的画质。

## 初次验证

保持所有自动开关关闭。先映射一个测试目录，扫描几段可恢复的视频，核对分类和备份目录，再确认计划。服务永不永久删除媒体，但写入权限、存储故障和外部程序并发修改仍可能造成风险，应保留 NAS 快照或独立备份。

## 更新与诊断

```bash
docker compose pull
docker compose -f docker-compose.yml -f docker-compose.intel-qsv.yml up -d
docker compose logs --tail=200 nas-video-converter
```

SQLite 位于宿主机 `/volume2/docker/nas-video-converter/config`。更新前停止容器并备份该目录。运行中断后，网页会显示中断任务；`/api/recovery` 只列出遗留临时文件，不会自动处置。
