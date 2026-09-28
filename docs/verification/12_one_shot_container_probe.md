# 一次性容器方案可行性上限探针

> 记录日期：2026-09-28
> 实现分支：`main`
> 探针脚本：[`demo/one_shot_container_probe.py`](../../demo/one_shot_container_probe.py)
> 当前状态：**探针通过全部安全门槛；生产改造未开始**

前两条路线已被实测排除：`hard_timeout` 不终止执行（见
[`10_hard_timeout_probe.md`](10_hard_timeout_probe.md)），`cleanup_session` 不即时也不完备
（见 [`11_cleanup_session_probe.md`](11_cleanup_session_probe.md)）。本页验证剩下的候选：
每次执行使用一次性容器，超时后由宿主侧强制销毁容器。

本页不修改生产执行链路。

---

## 1. 验收对象

| 项目 | 值 |
| --- | --- |
| 分支 | `main` |
| Docker Client / Server | 29.7.2 / 29.7.2（Docker Desktop 4.87.0） |
| 沙箱镜像 | `...all-in-one-sandbox:1.11.0`（本地已存在，未触发拉取） |
| 镜像 image ID | `sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7` |
| 挂载 | 宿主目录 `.data/one-shot-probe/<run>` → 容器 `/home/gem/workspace` |
| 容器参数 | `--cpus 2 --memory 4g --memory-swap 4g --pids-limit 512 -p 127.0.0.1::8080` |
| 载荷延迟 | `PAYLOAD_DELAY_SECONDS = 10`，销毁后等待 `15s` 再检查挂载目录 |

复现命令：

```powershell
.venv\Scripts\python.exe -m demo.one_shot_container_probe
```

---

## 2. 场景设计

先启动四种进程形态（各自写入独立的标记文件到挂载目录），确认它们都存活，然后
`docker rm -f`，再等待超过载荷延迟后检查挂载目录：

| 形态 | 启动方式 |
| --- | --- |
| 主进程 | `docker exec -d ... python3 -c "<sleep 10 → 写文件 → 存活 60s>"` |
| 普通子进程 | 父进程 `subprocess.run` 等待子进程 |
| 后台进程 | `sh -c "<python> & echo started"`，父 shell 立即退出 |
| 脱离进程组 | `subprocess.Popen(..., start_new_session=True)`，父进程存活 |

另外三个场景：并发容器互不影响、客户端崩溃后的孤儿容器回收、资源限制与 Docker
Socket 暴露检查。

---

## 3. 原始输出

```text
docker            : ...\DockerDesktop\resources\bin\docker.EXE (server 29.7.2)
image             : sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7
run tag / host dir: osprobe-28fa9cd0 / D:\gitcode\document-specialist-agent\.data\one-shot-probe\osprobe-28fa9cd0

--- scenario 1: force-remove containment ---
  start            : {'started_seconds': 0.50, 'returncode': 0, 'stderr': '', 'cold_start_seconds': 0.78, 'ready': True}
  processes alive  : 6 (expected >= 4)
  force destroy    : returncode=0 in 0.50s
  container gone   : True
  waiting 15s for delayed side effects...
  delayed markers  : []
  verdict          : CONTAINED (no delayed side effect after destroy)

--- scenario 2: concurrent container isolation ---
  start a/b        : True / True
  bystander alive  : before=1 after=1 exec_ok=True
  verdict          : ISOLATED

--- scenario 3: orphan container after client crash ---
  start            : True
  orphan present   : True (no supervisor would leave it running)
  label sweep      : 2 containers removed in 1.25s, remaining=0
  verdict          : RECOVERABLE (label-based sweep removes orphans)

--- scenario 4: limits and docker socket exposure ---
  start            : True
  limits           : memory=4294967296 memory_swap=4294967296 nano_cpus=2000000000 pids=512 network=bridge
  published ports  : {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '58517'}]}
  mounts           : ['D:\\gitcode\\document-specialist-agent\\.data\\one-shot-probe\\osprobe-28fa9cd0']
  docker.sock      : mounted=False visible_in_container=no
  docker binary    : none
  outbound network : outbound-reachable
  note             : PID limit is verified by config only; no stress bomb was run
  verdict          : OK (limits in force, no docker socket in container)

=== summary ===
probe containers left running : 0
leaks/blockers                : none
=== decision ===
Force-removing a one-shot container removed every process shape and
left no delayed side effect in the mounted directory.
exit_code=0
```

---

## 4. 结果汇总

| # | 检查项 | 结果 |
| --- | --- | --- |
| 1 | 强制销毁后四种进程形态是否全部消失 | 通过：销毁前 6 个进程，`docker rm -f` 后容器消失 |
| 2 | 挂载目录是否仍收到延迟副作用 | 通过：等待 15s 后标记文件为 `[]`，**零副作用** |
| 3 | 冷启动耗时（`docker run` → 可执行代码） | **0.78s**（本地已存在镜像） |
| 4 | 强制销毁耗时 | **0.50s** |
| 5 | 并发容器是否互不影响 | 通过：销毁 A 后 B 的进程仍在、`exec` 正常 |
| 6 | 客户端崩溃后的孤儿容器 | 会残留（无监督则一直存在）；标签扫描清理 2 个容器耗时 1.25s，剩余 0 |
| 7 | 内存 / CPU / PID 限制 | `Memory=4294967296`、`MemorySwap=4294967296`、`NanoCpus=2000000000`、`PidsLimit=512` |
| 8 | 网络与端口 | `network=bridge`，仅发布 `127.0.0.1:58517 -> 8080/tcp`，**出网可达** |
| 9 | 目录挂载 | 仅挂载本次探针目录，无其他宿主路径 |
| 10 | Docker Socket 暴露 | 未挂载；容器内 `/var/run/docker.sock` 不存在，`docker` 可执行文件不存在 |

`htprobe-` / `htclean-` / `osprobe-` 残留容器与文件均为 0。

---

## 5. 时间边界

一次性容器方案给出的硬边界是：

```text
timeout + 监督组件发现超时 + docker rm -f
```

实测 `docker rm -f` 为 **0.50s**，且销毁后**没有任何延迟副作用**。与前面两条路线对比：

| 方案 | 超出超时值的窗口 | 是否挡住脱离进程组 | 延迟副作用 |
| --- | --- | --- | --- |
| `hard_timeout`（shell） | 0.2s 返回，但进程继续运行 | 否 | 发生 |
| `cleanup_session` | +0.71s ~ +2.58s | 否 | 脱离场景发生 |
| **一次性容器 + `docker rm -f`** | **约 +0.5s（销毁耗时）** | **是** | **无** |

这是三条路线中唯一同时满足"进程必被终止"和"副作用不发生"的方案。

---

## 6. 必须记录的注意事项

1. **控制面必须在宿主侧。** 生命周期由可信的宿主侧监督组件控制；容器内不挂载
   Docker Socket，也没有 `docker` 可执行文件。Agent 与不可信代码不得获得容器控制能力。
2. **必须有孤儿清理。** 客户端崩溃时容器会残留；需要基于标签的宿主侧扫除器
   （本次实测 1.25s 内清理 2 个容器）。没有扫除器就会有孤儿容器堆积。
3. **冷启动数据是缓存命中值。** 0.78s 是在本地已存在镜像、且容器为小型镜像的前提下测得；
   首次拉取镜像、或镜像变大后的冷启动需要重新测量。
4. **网络默认是开放的。** `bridge` 网络下出网可达。若生产设计需要网络隔离，应改为
   `--network none`（此时无法用发布端口访问容器 API，需要改用宿主侧 `docker exec`
   或其他受控通道），这是一个需要显式决策的安全取舍。
5. **PID 限制仅验证了配置。** 未做压力测试（遵循项目"无压力炸弹"约定），
   实际强制行为需要单独、受控的验证。
6. **`docker rm -f` 是强制销毁**：会丢失容器内未持久化的状态，任务级工作目录必须通过
   挂载目录持久化。

---

## 7. 判定与后续

按既定判定顺序：

```text
一次性容器安全探针通过  ✅
→ 再审计 document_tool / hooks / Jupyter 富输出依赖   ← 下一步（本轮未做）
→ 计算迁移成本
→ 设计生产执行链路
```

一次性容器在本次实测中通过了全部安全门槛，因此继续保留该路线；但**迁移审计与生产设计
尚未开始**，本轮不修改任何生产代码，`SandboxClient.execute_python` 保持原样，
P0-5 继续保持未完成。
