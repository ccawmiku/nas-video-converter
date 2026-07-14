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

Intel N95 带 Intel UHD Graphics，可使用 FFmpeg `h264_qsv` 或 `h264_vaapi` 进行 H.264 硬件编码。硬件加速依赖核显已启用、群晖提供渲染设备，并且容器能够访问设备。先检查：

```bash
ls -l /dev/dri/renderD128
```

主 `docker-compose.yml` 已直接映射整个核显设备目录，确认渲染设备存在后正常启动即可：

```bash
cd /volume2/docker/nas-video-converter
docker compose up -d
```

Compose 中的核显映射为：

```yaml
devices:
  - /dev/dri:/dev/dri
```

入口脚本会读取渲染设备的实际组 ID，并把容器用户加入对应组。网页“转码硬件加速”默认是“自动”：它会指定 `/dev/dri/renderD128` 依次执行一帧真实 `h264_qsv` 与 `h264_vaapi` 编码测试，按 QSV → VAAPI → `libx264` 选择；也可以强制软件、QSV 或 VAAPI。强制的硬件后端不可用时任务会明确失败，原文件不会移动。

N95 属于 Alder Lake-N。如果 QSV 检查显示设备可读写，但在 `MFX session` 初始化阶段返回 `-3`，说明失败发生在 QSV/MFX 运行时兼容层，不是 Compose 映射或 UID/GID 权限。此时自动模式会改用同一块 Intel 核显的 VAAPI 编码路径；只要页面显示“当前转码：Intel VAAPI”，实际仍是核显硬件编码。

网页顶部会直接显示“当前转码：Intel QSV”“当前转码：Intel VAAPI”或“当前转码：libx264 软件”，实时任务中也会显示该文件实际使用的后端。如果仍提示设备无权限，可在 NAS 上查看设备的数值权限：

```bash
stat -c '%A %a %U %G %u:%g' /dev/dri/renderD128
```

映射设备只会把设备节点放进容器，不会改变宿主机的 UID、GID 和权限位；容器入口必须保留对应设备组作为附加组。

软件编码的 16/18/20 是 CRF；QSV 的 16/18/20 是 `global_quality`（ICQ）；VAAPI 的 16/18/20 是 QP。数值越小质量越高，但三种编码器的数值不能视为完全相同的画质。

## 初次验证

保持所有自动开关关闭。先映射一个测试目录，扫描几段可恢复的视频，核对分类和备份目录，再确认计划。服务永不永久删除媒体，但写入权限、存储故障和外部程序并发修改仍可能造成风险，应保留 NAS 快照或独立备份。

FFprobe 或严格完整解码确认损坏且文件未发生变化时，文件会安全移动到映射根目录下的 `损坏文件`，保留原相对目录并拒绝覆盖同名文件。该目录不会被后续扫描重复处理。

## 更新与诊断

```bash
docker compose pull
docker compose up -d
docker compose logs --tail=200 nas-video-converter
```

SQLite 位于宿主机 `/volume2/docker/nas-video-converter/config`。更新前停止容器并备份该目录。运行中断后，网页会显示中断任务；`/api/recovery` 只列出遗留临时文件，不会自动处置。
