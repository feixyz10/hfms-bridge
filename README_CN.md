# hfms-bridge

[用户指南（英文）](docs/README.md) | [English](README.md) | **简体中文**

在 HuggingFace（HF）与 ModelScope（MS）之间流式传输模型和数据集，不把文件内容保存到本地。支持 Python 3.9+、Linux、macOS 和 Windows。

## 开发环境与使用

```bash
uv sync --locked
uv run hfms-bridge hf2ms org/source user/destination --ms-token "$MODELSCOPE_API_TOKEN"
uv run hfms-bridge ms2hf org/source user/destination --hf-token "$HF_TOKEN"
uv run hfms-bridge ms2hf --help
```

目标端 token 必须显式传入。源端 token 可选，默认读取 `HF_TOKEN` 或 `MODELSCOPE_API_TOKEN`；私有或受限源仓库需要具备读取权限的 token。

## 参数与仓库行为

| 参数                                   | 默认值 / 行为                                                                           |
| -------------------------------------- | --------------------------------------------------------------------------------------- |
| `SOURCE DESTINATION`                 | 必填，均为 `owner/repository`                                                         |
| `--repo-type auto\|model\|dataset`     | auto 检查源端模型和数据集命名空间；只有一个可访问结果时选用它，两个均存在则要求显式指定 |
| `--hf-token TOKEN`                   | ms2hf 必填；hf2ms 可选，默认读取 `HF_TOKEN`                                           |
| `--ms-token TOKEN`                   | hf2ms 必填；ms2hf 可选，默认读取 `MODELSCOPE_API_TOKEN`                               |
| `--hf-revision REV`                  | `main`；源端版本或已存在的目标分支                                                    |
| `--ms-revision REV`                  | `master`；源端版本或已存在的目标分支                                                  |
| `--visibility auto\|public\|private`   | 仅新仓库；auto 对公开源创建公开目标，其余创建私有目标                                   |
| `--include GLOB [GLOB ...]`          | 可重复，匹配仓库相对路径，区分大小写                                                    |
| `--exclude GLOB [GLOB ...]`          | 可重复，优先于 include                                                                  |
| `--on-conflict overwrite\|error\|skip` | `overwrite`；处理内容不同或无法确认相同的目标文件                                     |
| `--workers N`                        | `4`；文件并行数，设为 1 即串行                                                        |
| `--attempts N`                       | `3`；每项操作尝试次数，包含首次                                                       |
| `--chunk-size SIZE`                  | `1MiB`；每个活动文件的读取缓冲                                                        |
| `--timeout SECONDS`                  | `120`；网络超时                                                                       |
| `--progress auto\|on\|off`             | `auto`；终端 stderr 显示进度                                                          |
| `--cache-dir PATH`                   | `~/.cache/hfms-bridge`                                                                |
| `--cache-max-size SIZE`              | `256MiB`；最小 64KiB，优先清理最久未使用的已完成任务                                  |
| `--restart`                          | 忽略本地进度并重新解析源版本，仍可复用远端相同内容                                      |
| `--dry-run`                          | 只读预览，不下载文件内容、不写远端、不更改缓存                                          |
| `--hf-endpoint URL`                  | `HF_ENDPOINT` 或 `https://huggingface.co`                                           |
| `--ms-endpoint URL`                  | `MODELSCOPE_ENDPOINT` 或 `https://modelscope.cn`                                    |

版本查看使用 `hfms-bridge --version`。大小支持字节及 KiB/MiB/GiB、KB/MB/GB。请给 glob 加引号，并将源和目标位置参数放在多值筛选参数前。自动识别只针对当前 token 可见的仓库。

目标仓库不存在时自动创建，使用该平台默认分支；其他目标分支必须提前创建。已有仓库逐文件提交更新，不删除目标上的其他文件。可见性只在创建时设置。创建 MS 仓库时采用源许可证，缺失时使用 `Other`；HF 不自动生成许可证卡片，许可证文件和 README 正常传输。

通过 SHA-256 或 HF 普通文件的 Git blob SHA-1 比较相同内容并跳过。后者可能需要再次读取源文件。dry-run 不为比较下载内容，因此部分结果为未知。`--on-conflict error` 在远端写入前检查所选路径，但不能对其他客户端的并发修改提供事务保证。

## 实现与恢复语义

`ms2hf` 配置 `https://hf-mirror.com` 时，目标端请求直接使用 `https://huggingface.co`，避免镜像跳转后重放已消耗的提交流，因此仍需能访问 HF 官方站点。检查点身份保留原始 endpoint 配置，已有任务使用原命令即可恢复。`hf2ms` 源端下载仍使用配置的镜像。

`hubs/hf.py`、`hubs/ms.py` 提供平台适配器，调度、重试、流校验、进度和检查点由公共引擎复用。

HF 源固定到 commit；MS 源把每个文件的不可变 revision、大小和摘要写入 SQLite 清单，枚举前后检查根列表标记。未完成任务恢复时复用清单，即使源分支已变化也保持文件版本。它是文件版本快照，不是服务端原子仓库快照。本地只保存元数据，不保存文件内容；元数据空间随文件数增长。缓存预算不会淘汰活动任务，放不下时会报错。

文件内容通过有限缓冲传输，并在完成前校验可用的 SHA-256。缺少摘要可能增加一次读取。HF 由 preupload 接口决定普通/LFS 存储；普通文件使用流式 Base64 NDJSON 提交，LFS 使用单次 PUT 或服务端协商的分片上传。MS 使用现有 LFS PUT/commit 协议，仓库元数据文件有 1MiB 内联限制。

源端断线可在当前上传过程中通过严格校验的 HTTP 206 Range 响应续读。MS 仅对 LFS 文件启用 Range，普通文件的 Range 行为不可靠，需重新读取。是否使用 LFS 取决于服务端元数据，与 `.pt`、`.jsonl` 等扩展名无关。

目标端上传失败仍按文件重试。HF 分片顺序流式发送，不缓存整片，也不持久化分片会话；不单独续传失败分片。**进程退出或 Ctrl-C 后，只保证文件级恢复，未完成文件不能从中间字节继续。** 若目标端已保存完整 blob，则可协商复用。保持原命令、不加 `--restart` 即可恢复；修改 workers 或 progress 不影响任务身份。

并行采用线程和独立 HTTP 会话，仅提交和短暂的 SQLite 操作串行。总进度统计完成或跳过文件，活动文件各有临时进度行。字节进度代表上传器读取量，不是服务端确认量；源端 Range 恢复不清零，整文件重试会清零。重试耗尽后停止派发新文件，等待活动任务完成。Ctrl-C 清理需等待正在执行的网络调用返回或超时。

token 不写入检查点，跨域下载跳转会移除认证信息。元数据跳转限于受信任来源，不自动重放已消耗的上传或提交流。

## Python API

```python
import os
from hfms_bridge import Transfer, TransferOptions

result = Transfer(
    TransferOptions("org/source", "user/destination", direction="ms2hf", workers=4),
    hf_token=os.environ["HF_TOKEN"],
    ms_token=os.getenv("MODELSCOPE_API_TOKEN"),
).run()
```

## 验证与构建

```bash
uv sync --locked
uv run ruff check .
uv run pytest
uv build
```

本地 HTTP 服务测试覆盖双向流式请求、分片完成、Range 恢复、固定清单恢复、冲突处理、缓存限制、取消和进度。CI 配置 Python 3.9–3.14 与 Linux/macOS/Windows。公开 MS 模型/数据集下载和 HF 元数据接口另行只读验证；本地测试不能替代真实鉴权上传的端到端验证。

协议参考：[HF LFS](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/lfs.py)、[HF commit](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/_commit_api.py)、[MS REST/LFS](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/_legacy_api.py)。

## 下载到本地

同一包新增 `hfms-download`，复用 HF/MS 源适配器及网络重试逻辑，直接写入本地文件，不使用全局下载缓存。

```bash
uv run hfms-download hf Qwen/Qwen3-0.6B --local-dir ./model
uv run hfms-download ms owner/repo --local-dir ./models --layout repo
uv run hfms-download hf owner/repo config.json --local-dir ./model
uv run hfms-download ms owner/repo --preset author
uv run hfms-download ms --help
```

必须选择 `--local-dir PATH` 或 `--preset author`，两者互斥。`--local-dir` 默认采用 `direct` 布局，直接写入指定目录；`--layout repo` 追加 `owner/repository`。仓库内部目录结构始终保留。

作者预设不默认启用：模型使用 `HF_MODELS`，数据集使用 `HF_DATASETS`，并追加仓库 ID。只校验识别出的类型对应的变量；变量必须非空并指向已存在的目录，否则报错，不回退到其他位置。预设不能与 `--layout` 同用。HF/MS 都使用这两个变量，它们是本工具约定的本地目录配置。

下载支持 `--repo-type auto|model|dataset`、`--revision`、`--token`、`--endpoint`、`--include`、`--exclude`。默认 revision 分别为 HF 的 main 和 MS 的 master；token 默认读取 HF_TOKEN 或 MODELSCOPE_API_TOKEN。位置参数中的文件名为精确路径，不存在时报错；include/exclude 继续筛选这些文件。

`--max-workers` 默认 4，按文件并行，单文件顺序下载；`--attempts`、`--timeout`、`--chunk-size` 默认分别为 3、120 秒、1MiB。`--progress` 控制总进度和活动文件进度；`--quiet` 隐藏普通日志和进度但保留警告、错误。成功时 stdout 输出目标路径。

`--dry-run` 只输出 JSON 计划，不读取文件内容、不创建目录、不修改状态。`--force-download` 重新解析源版本、丢弃旧部分文件，并允许重新下载替换已有本地文件。

状态目录位于最终目录旁：`./model` 对应 `./.model.hfms-download`；`./models/org/repo` 对应 `./models/org/.repo.hfms-download`。后续上传最终仓库目录时不会带入这些内部文件。状态目录与目标目录必须位于同一文件系统。删除状态目录会失去部分文件恢复信息，不会删除已完成文件。

未完成任务用 JSON 清单固定源版本；每个文件的小型 metadata 绑定内容身份，`.incomplete` 的实际长度就是恢复偏移量，不保存百分比、不逐块写数据库。重启后先重新计算已有前缀的哈希，验证 Range 响应后追加下载。MS 普通文件从头重下；MS LFS 和返回正确 Range 的 HF 文件可在进程退出后续传。不正确的 Range 会在重试预算内退回整文件下载。

完成后验证长度及源端提供的 SHA-256/Git blob SHA-1，再原子移动到正式路径；源端没有可信摘要时明确警告只完成长度校验。文件读取使用有限缓冲，清单内存随仓库文件数增长。

已有相同文件校验后跳过；未知或本地修改过的文件需加 `--force-download`。本工具管理且未被修改的文件允许按新源版本更新，旧文件保留至新文件验证成功，因此更新期间可能需要两份文件的空间。完整任务下次执行会刷新源版本，未完成任务保留原快照。目录绑定平台、仓库、revision 参数和 endpoint；切换身份请使用其他目录，或先移走/清理其状态。不会删除无关本地文件。

同一目标目录只允许一个下载进程。Ctrl-C 等待活动网络调用返回或超时，停止工作线程后释放锁，保留可恢复部分文件。禁止越界路径、目标符号链接、Windows 保留名称及冲突路径；状态中不保存 token。
