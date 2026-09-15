# 05 · 上游项目、复用边界与决策记录

核查日期：2026-09-15。依据本轮对话中读取的仓库 README、源码和许可文本，以及公开官方文档。本次未安装并端到端运行这些生成平台，也没有付费生成样片。

本仓库此次只写规划，没有复制任何上游业务源码或模型权重。下列“复用”均指待实现计划，不能当作已经接入。

## 1. 候选定位

| 项目 | 对 MJ 的用途 | 决策与边界 |
|---|---|---|
| browser-use/video-use | 剪辑清单、FFmpeg 后期、时间线可视化的代码参考 | 优先选择性移植并封装，保留 MIT 声明；不整体当作漫剧生成器 |
| ArcReel/ArcReel | 资产/分镜/生成任务/费用/导出的工作台设计对照 | 原创生产流程参考；AGPL+NOTICE 需要另行评估，不直接复制进入未定许可的主项目 |
| calesthio/OpenMontage | 分阶段制作、样片、审核和动画路线参考 | 流程研究，不直接复制整套 Skill；具体导入前重新核查相应版本许可证 |
| HBAI-Ltd/Toonflow-app 与 Toonflow-web | 中文画布、剧本与制作 Agent、供应商接入设计参考 | LICENSE 含补充商业授权条款；不按“纯 Apache”处理，不作为当前直接整合底座 |
| NarratoAI / MoneyPrinterTurbo | 未来解说路线、语音/字幕/素材服务的候选 | 本期不引入完整项目，防止范围扩张；未来复用时逐文件核验来源与许可 |
| FFmpeg / ffprobe | 本地媒体处理与验证 | 固定实际构建并核查滤镜/编码器；分发义务按实际启用组件评估 |

主项目优先使用清楚可替换的工具模块，而不是把多个完整应用的 UI、任务表和审批流程拼成一个系统。

## 2. video-use：值得复用与必须改造

本次核对的提交：`9575612f066aa517354790a645fd90f9f95a743b`。这只是核查快照，不要求永远固定此版本；后续升级要走回归与来源登记。

### 可优先评估

- `helpers/timeline_view.py`：把局部画面和波形等整理成便于检查的图；需要评估无音轨和中文字体。
- `helpers/render.py`：片段处理、时间偏移、字幕最后合成、帧率与方向处理等思路/实现。
- `helpers/grade.py`：可控调色工具；不能把某预设强套到所有动漫风格。
- `helpers/transcribe.py`：需要转写已有声音时参考；不是原创漫剧必经步骤。

### 已看到的边界

1. 上游是面向编码 Agent 的 Skill+脚本，不是拥有完整角色/分镜/预算数据模型的 Web 产品。
2. `build_master_srt` 以两词分组、英文标点和大写为默认，需要中文重构。
3. `build_final_composite` 映射基础视频音轨，不能直接承担 MJ 的无声视频+独立旁白/对白/音乐总线。
4. `transcribe_one` 看到现有缓存路径就返回；仅按素材名和音轨保存时，源文件内容变更需要额外指纹失效策略。
5. SKILL 要求 Agent 自检，不代表单独调用 render.py 已执行全套审核。
6. 流式拷贝拼接是有条件优化，不是所有时间线通用方案；MJ 还要处理统一画布、转场、声音连续性与不支持特性报错。

以上可在 S1 的具体文件中核对。本次没有声称已通过这些边界场景的实际运行测试。

### 复用登记要求

每个导入文件记录上游仓库、commit、原路径、许可证、导入日期、内容哈希、修改摘要和本地测试。原作者声明保留；补丁与上游隔离，升级先比较差异再回归。只引用架构思路时记录来源，不假装为原作者的验证结果。

## 3. 许可与产品边界

`video-use` 核对版本为 MIT。ArcReel 的 NOTICE 声明 AGPL-3.0 并要求修改 UI 保留适当署名和原仓库链接。Toonflow 的 LICENSE 在 Apache 文本后含补充授权条件，不能只看首页徽章。

这些是源文件内容记录，不是法律意见或对条款效力的判断。准备分发、闭源部署、去品牌或向第三方提供服务前，核对具体代码、版本、使用方式并按需取得授权。拆成服务不自动消除许可义务；也不能把公开仓库误认为没有版权条件。

MJ 自有代码许可暂未选择；本次不添加 MIT/Apache/AGPL 声明代替用户决定。模型服务、权重、角色图、字体、音乐和声音授权分别管理；仓库代码许可不等于生成素材拥有任意使用权。

FFmpeg 官方说明其主要代码适用 LGPL，启用部分 GPL 组件会影响构建许可；不能因为“只是调用命令”就忽略再分发二进制的义务。实际构建、x264 等组件和安装包分发方式应在发布前核验。

Remotion、HyperFrames、Manim、剪映草稿库等均不是首版必需依赖。需要时独立评估对应版本、许可、运行需求和兼容性，不继承上游 README 中的笼统“免费”结论。

## 4. 决策记录（设计基线，不等于用户已确认全部偏好）

| 编号 | 决策 | 理由 | 重新评估的触发条件 |
|---|---|---|---|
| ADR-001 | 本期只做原创漫剧 | 不混入原片理解与检索工程 | 原创路线 M4 通过并明确启动第二路线 |
| ADR-002 | 独立 MJ 领域模型+工具模块复用 | 掌握版本、预算与生产事实，避免多个平台冲突 | 整体 fork 经许可和迁移评估明显更合适 |
| ADR-003 | 先旁白驱动+必要真实动作 | 先验证故事与一致性，同时避免 PPT 冒充 | 对白/口型/复杂动作样片通过 |
| ADR-004 | PG 持久任务+模块化单体 | 首版保持单一事实来源，控制运维复杂度 | 实测吞吐或多机需求要求专业队列/编排 |
| ADR-005 | 15 秒代表样片后才批量 | 尽早暴露模型、动作和成本问题 | 不取消此质量关卡，只调整样片范围 |
| ADR-006 | 默认 30 fps/3600 帧，音频采样计时 | 可测量的 120 秒与低漂移时间线 | 项目开始前调整格式并重新定义验收 |
| ADR-007 | 先成片+开放项目包，剪映后置 | 不被外部草稿格式或桌面导出兼容性卡住 | 用户明确需要且实际版本兼容性验证通过 |
| ADR-008 | 付费默认关闭，未知提交先查单 | 降低重复扣费与无授权消费风险 | 不允许取消，仅改进供应商核对能力 |
| ADR-009 | Agent 产出建议，服务端执行权限与预算 | 文本不能替代系统约束 | 不允许直接赋予模型无边界权限 |

## 5. 来源索引

下列链接指向官方仓库或官方文档。使用 commit 链接的条目可复查同一快照；官方在线文档可能更新，正式导入时再次记录版本。

### S1 · video-use（已读 README、SKILL、完整 render、transcribe、LICENSE）

- [仓库](https://github.com/browser-use/video-use)
- [MIT LICENSE](https://github.com/browser-use/video-use/blob/9575612f066aa517354790a645fd90f9f95a743b/LICENSE)
- [render.py](https://github.com/browser-use/video-use/blob/9575612f066aa517354790a645fd90f9f95a743b/helpers/render.py)
- [transcribe.py](https://github.com/browser-use/video-use/blob/9575612f066aa517354790a645fd90f9f95a743b/helpers/transcribe.py)
- [SKILL.md](https://github.com/browser-use/video-use/blob/9575612f066aa517354790a645fd90f9f95a743b/SKILL.md)

### S2 · ArcReel（已读 README、NOTICE、部分任务队列源码）

- [README](https://github.com/ArcReel/ArcReel/blob/81c3a0e1526b2fd06ca2fae1de7452fc04cfcb9c/README.md)
- [NOTICE](https://github.com/ArcReel/ArcReel/blob/81c3a0e1526b2fd06ca2fae1de7452fc04cfcb9c/NOTICE)
- [generation_queue.py](https://github.com/ArcReel/ArcReel/blob/81c3a0e1526b2fd06ca2fae1de7452fc04cfcb9c/lib/generation_queue.py)

### S3 · Toonflow（已读 README 与实际 LICENSE）

- [README](https://github.com/HBAI-Ltd/Toonflow-app/blob/e03cf590eb0cab63534a4040db9acb4ec95b42a6/README.md)
- [含补充协议的 LICENSE](https://github.com/HBAI-Ltd/Toonflow-app/blob/e03cf590eb0cab63534a4040db9acb4ec95b42a6/LICENSE)
- [前端仓库](https://github.com/HBAI-Ltd/Toonflow-web)

### S4 · OpenMontage（已读相关流程文件；链接为可变 main）

- [解说流程](https://github.com/calesthio/OpenMontage/blob/main/pipeline_defs/animated-explainer.yaml)
- [本地角色动画流程](https://github.com/calesthio/OpenMontage/blob/main/pipeline_defs/character-animation.yaml)
- [动画素材制作说明](https://github.com/calesthio/OpenMontage/blob/main/skills/pipelines/animation/asset-director.md)

### S5 · FFmpeg（本轮在线核对）

- [滤镜文档](https://ffmpeg.org/ffmpeg-filters.html)：concat、xfade、amix、loudnorm 等需根据实际构建和参数测试。
- [许可与法律事项](https://ffmpeg.org/legal.html)：核对实际分发构建的许可条件。

## 6. 尚未验证的事实，不得写成结论

尚未测量任一供应商在 MJ 场景下的中文声音表现、角色稳定性、复杂动作成功率、120 秒有效成本、生成耗时、部署资源和剪映兼容性。本次没有选定实际供应商或报价。

这些事项通过 M1–M4 的合同测试、授权样片与实际账单完成验证。某项目 star 多、有 Demo、README 写 production，都不能代替 MJ 自己的验收。
