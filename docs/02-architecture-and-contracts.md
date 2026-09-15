# 02 · 技术架构、数据模型与接口契约

状态：拟定设计，不是已实现 API。首版采用模块化单体+独立 Worker，不同时引入多个完整平台、多个调度框架或多套数据库。

## 1. 架构决策

### 1.1 业务模型归 MJ 所有，工具按能力复用

故事、角色、镜头、资产版本、审批、预算和时间线由 MJ 管理。`video-use` 可作为后期工具的代码来源/对照实现，但不作为原创剧本、角色生成或生产状态的真相源。上游原始 EDL 仅适配简单剪辑，MJ 使用版本化的 TimelineSpec，能力不足时显式拒绝，不能丢字段后继续运行。

ArcReel、Toonflow、OpenMontage 用于流程和能力对照，不直接混合其业务数据库与调度器。许可未核清的代码不复制；若未来决定整体 fork 某平台，另写决策记录、迁移计划和许可证方案，不能把引用研究默认为已经集成。

### 1.2 首版建议技术栈

| 层 | 建议 | 边界 |
|---|---|---|
| 前端 | React + TypeScript | 项目页、分镜列表、候选审核、基础时间线；不先做无限画布 |
| API/领域层 | Python + FastAPI + Pydantic | 结构化数据校验、状态和权限；不在 HTTP 进程执行长时间渲染 |
| 数据 | PostgreSQL + SQLAlchemy + Alembic | 业务、任务、预算、版本、事件统一落库；不维护 SQLite/PG 双生产路径 |
| 任务 | PG 持久任务表 + 独立 Worker | lease、心跳、恢复、并发限额；首版无需 Redis/Celery/LangGraph 同时上场 |
| 资产 | 本地内容寻址存储，预留 S3 兼容接口 | DB 保存元数据；媒体不放数据库、不放 Git |
| 媒体 | FFmpeg/ffprobe，选择性改造 video-use | 固定工具版本，预检滤镜/编码器，执行受限参数 |
| 模型 | ProviderAdapter | 文本、视觉检查、图片、视频、TTS 分开；无需同一供应商包办 |
| 实时进度 | SSE + 可重放事件 | HTTP 管理操作；必要时才引入 WebSocket |
| 测试 | pytest、前端单测、端到端浏览器测试 | 优先模拟供应商与本地合成素材；付费测试独立开关 |

开发起点建议 Python 3.12 与 Node.js 22 或更高的兼容版本；精确版本、依赖安全和锁文件在技术验证阶段确定，不把建议解释为已经测试。FFmpeg 的许可取决于实际构建和启用组件，见 [05](05-upstream-and-decisions.md)。

### 1.3 进程与职责

```text
Browser
  └─ API / Authentication / Domain Services
       ├─ PostgreSQL: projects, revisions, approvals, tasks, budget ledger, events
       ├─ AssetStore: local / object storage
       └─ Persistent Job Queue
            ├─ Planning Worker → text / vision providers
            ├─ Generation Worker → image / video / TTS providers
            └─ Render Worker → local FFmpeg / validators
```

首轮可在一台服务器上运行这些逻辑进程。模型生成与渲染分别限额，不能因同时请求多个视频而让 FFmpeg 挤占 API 资源。公网访问、GPU 和多机分布式不是首版默认前提。

## 2. 建议代码边界

```text
apps/api/                 # HTTP、认证、事件出口
apps/web/                 # Web 工作台
apps/worker/              # 调度与执行入口
packages/domain/          # 项目、审批、预算、版本、失效规则
packages/contracts/       # 版本化 schema 与类型
packages/providers/       # 供应商适配与能力目录
packages/media/           # 时间线编译、混音、字幕、渲染、技术质检
packages/storage/         # 资产和下载适配
integrations/video_use/   # 经审查的选择性移植、补丁与测试
prompts/                  # 版本化创作模板；不是可执行系统权限
examples/                 # 明确标注的规划/测试样例
 tests/                   # 实施时整理为规范目录；此处仅示意
```

上述是未来组织建议，不意味着这些目录现在存在。正式实现不应照搬目录数量而过度拆包；可以先在单个 Python 项目内以模块实现边界。

## 3. 核心对象

| 对象 | 需要保存的关键信息 |
|---|---|
| Project / Episode | 所有者、模式、目标规格、当前修订、预算授权引用、当前交付版本 |
| Brief / Concept / Script / Beat | 不可变版本、作者来源、内容、结构化节拍与选择记录 |
| Character / Scene / Prop / VoiceProfile | 稳定实体 ID、设定版本、可变状态、参考资产与授权 |
| Shot | 稳定 shot_id、顺序、节拍/场景、进入退出状态、动作合同、声音与参考版本 |
| Asset / AssetVersion / Blob | 逻辑身份、不可变版本、内容哈希、媒体属性、权利记录、有效性、物理存储键 |
| GenerationSpec | 一次生成的冻结输入、能力要求、供应商配置快照、预算引用 |
| Task / Submission / Attempt | 生命周期、输入哈希、远端 ID、操作幂等键、租约、错误、实际费用 |
| TimelineVersion | 整数帧视频时间线、采样级音轨、字幕、效果、选中资产版本 |
| Approval | 人工主体、批准范围、具体版本/哈希、预算范围、时间及失效原因 |
| ReviewFinding | 镜头/帧定位、严重级别、来源、证据、处理结果 |
| BudgetAuthorization / Ledger | 币种、上限、预占、结算、未知费用、释放和调整 |
| ReleaseManifest | 固定时间线、资产清单、工具版本、审核报告、输出哈希 |

实体 ID 不等于显示名称；人物改名不应丢失所有镜头关联。资产版本不等于某个临时下载 URL；签名到期不得使长期项目失去媒体。无关项目不得仅凭相同文件名共享资产。

## 4. 版本、依赖与局部重做

### 4.1 不可变产物与可变选中指针

原始输入和生成产物不可原地覆盖。用户激活候选时，只更新 `active_version_id`，并检查 `expected_revision`。生成任务结束时先保存候选，只有输入版本仍符合当前计划且满足选择条件时才允许激活；旧任务晚到不覆盖新方案。

审批和任务都引用具体版本，不引用“最新图片”这种动态名称。每次调用冻结输入；更换供应商或参考资产后创建新任务，不能让排队任务执行时偷偷读取另一套最新配置。

### 4.2 依赖图

```text
Brief → Concept → Script / Beats
                     ├─ Bible / Reference Assets → ShotSpec → Images → Video Candidates
                     └─ Voice Plan → Speech Segments ───────┐
                     Storyboard + Audio → Animatic          │
                     Selected Assets + Audio → Timeline → Render → QA → Release
```

每条依赖指明类型：内容、身份、时间、视觉样式或导出格式。失效传播沿实际引用走，不把所有下游无差别删除。

| 修改 | 应受影响 | 不应无故重做 |
|---|---|---|
| 字幕字体/位置 | 字幕渲染、合成、相关质检 | 生图、生视频、TTS |
| 同长度的旁白音色 | 该音频、混音、时间对齐复核、合成 | 无口型关联的画面 |
| 台词内容或实际时长 | 配音、字幕、时间线及依赖动作的镜头 | 无依赖的角色设定 |
| 角色服装参考 | 引用该服装的镜头和一致性检查 | 其他角色/其他服装版本 |
| 替换某镜头候选 | 该镜头、相邻衔接、相关时间线/合成 | 其余无依赖生成任务 |
| 输出横竖比例 | 构图适配、字幕、镜头可用性及必要再生成 | 故事文本原则上保留 |

失效产物保留历史，标记 `stale`，与“文件损坏”分开。允许用户显式导出已批准的旧快照，但不能把它标为符合当前新要求。

## 5. 状态模型

### 5.1 阶段状态

`not_started → working → awaiting_review → approved`，另有 `blocked`、`stale`、`rejected`。阶段批准不是执行任务成功的别名。

### 5.2 任务状态

`queued → dispatching → submitted → running → succeeded`。

异常分支：`reconciling`（是否提交/计费未知）、`cancel_requested`、`cancelled`、`failed`。本地任务可以没有 `submitted`；供应商完成但下载未完成不能直接记为产物成功。

合法转换、超时和恢复路径由代码维护。`succeeded` 只表示有效产物已落地，候选审核和当前版本激活另行记录。禁止前端通过传入 status 任意跳过阶段。

### 5.3 远端请求身份

一个可能收费的远端生成请求对应一个稳定 `submission_id` 和幂等键。网络重发、轮询和恢复不能悄悄换这个身份。创作重生成是新 submission，重新预占预算。传输重试次数与生成尝试次数分别统计。

没有远端幂等/查单能力时，不承诺 exactly-once。租约只能限制本地执行，不能证明供应商未收到超时请求；细则见 [03](03-reliability-cost-security.md)。

## 6. 供应商契约

### 6.1 能力目录

至少声明：支持的任务类型、画幅/分辨率/长度档位、参考图数量及语义、首尾帧/主体参考能力、原生声音、可指定音色、seed、状态查询、远端幂等、取消、回调、上传方式、结果有效期、价格来源和最近验证日期。

`declared_capabilities` 与 `verified_capabilities` 分开。文档写支持不等于实际账户有权限；供应商名相同不等于每个模型配置能力相同。模型原生接口与 OpenAI 兼容接口不假设完全等价。

### 6.2 概念性方法

`capabilities()`、`validate_spec()`、`estimate_cost()`、`submit()`、`query()`、`cancel()`、`download_result()`、`normalize_usage()`。

文本/TTS 可以同步返回，视频可以异步返回；统一结果封装，但不强迫伪造远端 job_id。输出至少有状态、供应商请求标识、媒体清单、用量、错误分类与是否可安全重试。

### 6.3 GenerationSpec 必需信息

`schema_version`、project/episode/shot、操作类型、不可变脚本与参考版本、动作合同、时长目标、输出规格、声音模式、模型配置版本、成本报价引用、预算授权、输入指纹、生成意图标识。

临时签名 URL 在发送时生成；语义指纹使用资产哈希/版本，不使用会到期的 URL。实际发送载荷另存脱敏摘要供诊断。模型响应视为不可信输入，先结构化校验，不能执行其中的指令或代码。

## 7. TimelineSpec 与渲染

### 7.1 唯一时间基准

视频时间采用整数帧与有理数帧率 `{num, den}`；音频采用采样位置与采样率。P0 默认 30/1 fps、3600 帧、48 kHz，预编码音频长度对应 5,760,000 个采样。展示用秒从时间基准推导，不再维护会漂移的另一套浮点时间真相。

视频片段包含轨道、目标 start/end、来源 asset_version、source_in/out、速度映射、构图策略和转场；音频事件包含采样起点、源区间、增益/淡化/ducking；字幕包含目标时段、文字、speaker 和样式引用。

主画面在 `[0, total_frames)` 每帧有明确覆盖；有意黑场必须是显式片段。叠加轨允许重叠，不能以“有重叠”一律拒绝。转场的覆盖长度与剪辑边界以编译后的目标时间线为准。

### 7.2 编译步骤

1. 校验资产版本、权限、媒体存在、长度、输入规格和获批快照。
2. 统一方向、画布、像素格式、SAR、帧率与色彩；需要裁切时校验主体/字幕区域。
3. 将主视频、静帧动画和叠加效果编译为受限渲染计划；不支持的效果报错，不忽略。
4. 独立构建声音总线，处理无声镜头、连续旁白、对白、音乐与音效，按计划混音。
5. 最后合成可选烧录字幕；分别导出干净版和字幕版。
6. 完整解码检查、帧数检查、音频有效时长/可听首尾检查、采样质检、写 manifest；通过后原子发布。

MP4/AAC 可能有封装时基与编码填充差异，因此同时检查视频解码帧数与音频内容，不能只读取一个容器 duration 数字。技术方案应验证实际构建对采样裁剪和延迟信息的处理，不以末尾 `-t 120` 掩盖剧情或对白超长。

### 7.3 video-use 改造边界

上游 `render.py` 有分段抽取、拼接、叠加和字幕路径，但最终合成使用基础视频音轨；字幕默认英语短词分组，转写面向源视频。[来源见 05 的 S1。]

MJ 应适配/重构无音轨路径、独立多轨混音、中文断句、统一画布、整数帧、输入 schema、资产版本与缓存指纹。保留可验证的工具实现，不原封不动导入上游 SKILL 的“音频优先”作为所有漫剧的硬规则。

不为追求“无损拼接”强制所有镜头使用不兼容的编码参数；只有流参数与时间基一致且经过验证才允许 copy concat。使用转场/叠加时接受必要重编码，并记录中间格式与最终编码参数。

## 8. HTTP 接口大纲

统一前缀 `/api/v1`。以下是待实现的契约清单，不是目前可以访问的地址。

| 方法与路径 | 意图 |
|---|---|
| POST /projects | 创建需求与输出配置 |
| GET /projects/{id} | 获取项目、当前版本、阶段与阻塞摘要 |
| POST /projects/{id}/plans | 生成/保存候选策划；paid 模式需预算授权 |
| PATCH /episodes/{id}/script | 使用 expected_revision 提交新剧本版本 |
| POST /episodes/{id}/storyboards | 创建结构化分镜版本 |
| POST /assets/uploads | 建立受限上传，完成后探测与哈希 |
| POST /assets/{id}/versions | 新增参考/生成版本，不覆盖旧对象 |
| POST /shots/{id}/estimates | 估算该冻结输入，不触发生成 |
| POST /shots/{id}/generations | 校验审批/能力/预算后创建生成任务 |
| POST /shots/{id}/activate | 原子选择已审核候选，校验修订号 |
| POST /episodes/{id}/impact | 返回修改影响预览，不调用付费服务 |
| POST /episodes/{id}/animatics | 创建动态分镜预演任务 |
| POST /episodes/{id}/renders | 使用冻结时间线创建渲染任务 |
| POST /approvals | 人工批准指定版本、范围；身份来自会话，不能由请求体冒充 |
| POST /budget-authorizations | 人工批准预算与允许的外发范围 |
| GET /tasks/{id} | 实际任务状态、远端状态、费用状态、错误 |
| POST /tasks/{id}/cancel | 请求取消；不宣称供应商立即停止或退款 |
| POST /tasks/{id}/reconcile | 核对已有请求，不等价于重新生成 |
| GET /projects/{id}/events | SSE 事件流；支持 Last-Event-ID 与补拉 |
| POST /episodes/{id}/releases | 对批准快照生成交付版本，不自动对外发布 |

异步创建返回 202+task_id；版本冲突返回 409；schema/能力不满足返回 422；权限不足返回 403。预算不足、审批过期、缺资产等使用稳定业务 error_code。错误包含 request_id、是否可安全重试、建议动作，不回显密钥。

所有可能创建工作/费用的 POST 接受幂等键。同一键同一正文返回同一操作；同一键不同正文返回冲突。预算预占、任务创建和事件写入在一个数据库事务中完成。

## 9. 模拟与可替换性

MockProvider 必须模拟成功、拒绝、长任务、超时后成功、未知提交、回调重复、结果过期、取消后晚到及计费未定。模拟结果带 `mock=true`，不得显示为真实模型效果。

领域层测试不需要外网或 API Key。更换供应商后仍使用同一 MJ schema、状态机和验收；供应商不支持的能力在预检中阻塞，不能用空字符串/假文件填充通过。
