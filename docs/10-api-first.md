# 10 · API-first 原创漫剧（v0.4）

决策日期：2026-09-16。MJ 管理故事、分镜、任务、版本、预算与合成；文本、图片、视频、旁白都是服务端 API 调用。**本版没有调用真实收费模型，没有部署用户服务器，没有宣称首条漫剧质量通过。**测试范围见 [11](11-api-verification.md)。

## 1. 结构与边界

```text
浏览器 → MJ Web/API → PostgreSQL/审批/预算/冻结任务 → MJ Worker
                                                        ├─ 文本 API（兼容 chat/completions）
                                                        ├─ Qwen 2.0 生图/编辑 API
                                                        ├─ MiniMax H3 V2 视频 API
                                                        ├─ MiniMax Speech 2.8 旁白 API
                                                        └─ 可选独立 ComfyUI API（默认不启用）
结果 → 受限下载/媒体验证 → 待审核素材 → 明确选择 → 独立声音/字幕/FFmpeg → 交付
```

MJ 运行时不需要 CUDA、模型权重或本地推理库。FFmpeg 仍在 MJ 服务器做后期。以后独立部署模型只需实现对应服务协议/适配器，不改变 MJ 业务对象；**不是任何服务都能只换 base_url**。原生云适配器仅接受管理员批准的 HTTPS 公网来源，私网自托管继续使用已有 ComfyUI 受控传输，不删除 SSRF/TLS 边界。

既有 `openai_compatible`、`fal_queue`、`comfyui` 路径保留，不做强制数据迁移。新增参数在任务/工作区 JSON 中；不修改已发布的 0001 迁移。旧请求未使用新选项时保持幂等指纹。保存旧镜头时增加默认原声策略可能使旧审批失效，需要人工确认，不能自动迁移成“批准”。

## 2. 实际适配的协议

| 配置 type | 支持范围 | 路由 |
|---|---|---|
| `qwen_image` | Qwen-Image-2.0 同步统一文生图/多参考编辑 | POST `/api/v1/services/aigc/multimodal-generation/generation` |
| `minimax_h3` | MiniMax-H3 / MiniMax-H3-Max：文字、首帧、尾帧、首尾帧、混合参考 | POST `/v2/video_generation`；GET `/v2/query/video_generation/{task_id}` |
| `minimax_tts` | speech-2.8-hd / speech-2.8-turbo：独立中文旁白 | POST `/v1/t2a_v2`，非流式 hex MP3 → 本地48k WAV |

本次按以下官方接口核对（2026-09-16）；账户权限和模型质量尚未实测：
- https://help.aliyun.com/zh/model-studio/qwen-image-api
- https://help.aliyun.com/zh/model-studio/qwen-image-edit-api
- https://platform.minimax.io/docs/api-reference/video-generation-v2-create
- https://platform.minimax.io/docs/api-reference/video-generation-v2-query
- https://platform.minimax.io/docs/api-reference/speech-t2a-http

未接：旧版 Qwen 异步 image-synthesis 协议、H3 Context-IR/2K再生成专用接口、webhook回调、流式 TTS、声音克隆。H3 创建参数中的2K与专用再生成接口是不同事。

## 3. 云服务配置（默认不消费）

先按 README 完成 CPU 服务器 PostgreSQL/API/Worker 部署。备份现有 `.env`、`providers.json`；合并 [providers.cloud.example.json](../providers.cloud.example.json) 中需要的条目到管理员配置，不覆盖已有 ComfyUI 或其他供应商。

示例中：
- `text-api` 使用兼容文本接口；`qwen-images` 使用 Qwen 原生统一图片协议，不是 OpenAI images 路由。
- 百炼示例使用仍受支持的北京地域 `dashscope.aliyuncs.com`；新账号/其他地域可按官方文档改成账户工作空间域名，**同时修改 hosts 并匹配该地域的 Key**。
- H3/旁白使用 `api.minimax.io`。不同平台、地区、托管商的协议/币种不能混同。
- 所有 `reserve_micros` 与 `pricing` 故意为 **0**，旁白 `default_voice` 故意为空。必须按实际账户填写正预占和可用 voice_id；否则真实任务拒绝。Key存在/本地检查通过不代表这些准备已完成。
- `download_hosts` 只包含示例中的精确 CDN 域名。实际返回其他域名时，任务停住等待管理员核实域名，不自动跟随重定向/放开通配符，也不重新生成。

服务器环境（这里不放任何真实 Key）：

```dotenv
MJ_MODE=production
MJ_EXTERNAL_ENABLED=true
MJ_COMFYUI_ENABLED=false
MJ_PROVIDERS_FILE=/app/providers.json
MJ_QWEN_API_KEY=<仅在服务器密钥环境设置>
MJ_MINIMAX_API_KEY=<仅在服务器密钥环境设置>
```

API 和 Worker 要读取同一配置；变更配置后同时重启。Compose 的 `.env` 通过 `env_file` 注入，两者共享只读 providers.json。浏览器不提供输入 Key 的窗口，不显示凭据或临时下载 URL。

```bash
python -m mj.cli api-check
# 也可只检查一种
python -m mj.cli api-check --provider h3-video
```

`api-check` 和网页“检查本地配置”**不联网**，只验证配置与环境变量是否存在。不要为了“连通性测试”偷偷发一次收费生成。所有真实调用必须经过项目预算和阶段审批。

## 4. 预算：人民币/美元分别授权

一个项目可有多种币种的预算，但每条授权只能包含同币种供应商。任务预占/结算归属自己的预算；页面和导出保留币种，不给出没有汇率依据的跨币种总数。

数值使用整数微单位：`1 元/美元 = 1,000,000 micros`。例如 `500000` 只表示该币种0.5单位，**不是任何模型价格承诺**。

最小预占取固定 `reserve_micros[kind]` 与管理员输入费率计算值中的较大者：
- 图片：`per_image_micros`；每任务1张。Qwen提示词另设1300 UTF-8字节的保守本地上限（不是将官方Token当字符换算）；超出请显式精简，不静默截断。
- H3：实际请求输出秒数 × `per_output_second_micros`，加输入参考视频秒数 × `per_input_second_micros`。不能按剪后5秒给实际生成8秒报价。
- TTS：台词字符数 × `per_1000_characters_micros` / 1000，向上取整。实际供应商计数可能不同，所以仍需保守预占。

请求 `cap_micros` 必须≥最小预占、≤授权单次限额，且总预算有余额。`usage` 只记供应商返回的有效数字，不自动当作实付。生成成功、失败、取消都不能据此清零账单；未知费用保持风险额度，待人工登记真实账单证据。

## 5. 用户在 MJ 的步骤

1. 保存原创需求 → 生成/编辑提案和剧本 → 人工 G1。
2. 角色页生成图片：选 `qwen-images`。没有参考为文生图；最多3张已审核、非 MOCK、有使用权的项目图片为编辑，显式传递顺序。新图片先审核，再关联角色/场景。
3. 按项目画幅做分镜图。图片表单默认跟随项目比例（竖屏720×1280、横屏1280×720或相应整数尺寸）；需要时调整。首帧视频模式的输出比例由首帧决定，所以不要拿2:3竖图冒充9:16分镜。
4. 保存镜头台词与动作 → G2 分镜确认。
5. 选连续2–3个镜头构成约15秒样片。视频任务勾“代表性样片”，填写同一组镜头 ID；后端要求连续、包含当前镜头、组长10–20秒。单个视频任务仍只生成本镜头，不把整集一次发给 H3。
6. 选 `h3-video` 的输入模式与参考、预算、生成秒数。先“仅预检”看冻结计划；确认后才真实提交。
7. 选 `minimax-narration`，填写经账户确认的声音 ID/语速/音调/发音词典。它只朗读**该分镜已经保存的 narration**，不朗读“补充方向”。每个角色可选择不同已批准声音，不自动克隆。
8. 审核和选用视频、旁白，合成样片 → 人工 G3 → 再制作其他镜头 → 全片终审 G4/导出。样片通过不自动消耗全片预算。

## 6. H3 细节，避免静默错误

- 类型：`text` / `first_frame` / `last_frame` / `first_last` / `reference`。首/尾帧与参考角色互斥，后端校验；输入不是所有图片塞到一个字段。
- 参考只来自本项目已审核非MOCK素材，禁止用户直接输入URL。最多9图/3视频/3音频；视频/音频各累计≤15秒且单段2–15秒。图像、视频分辨率/容器/编码/大小在外发前检查。当前不支持HEIC/HEIF。
- 请求内嵌 data URI 不公开整个素材库。不启用公网对象上传；64MB请求上限内生成。超限直接拒绝，不能偷偷缩图/剪声音改变语义。
- H3生成4–15整数秒、768P/2K；H3-Max生成5–15秒、480P/768P。模型变体不是可任意替换。
- 默认生成长度向上取整并覆盖镜头使用区间与 source_in_frames；不够/超过模型档位就阻塞，要求拆镜头或改计划。
- 不发送文档未声明的 `seed` 或“关闭原声”字段。后者在MJ合成处理，不假装模型支持无声生成。

## 7. 独立旁白与原生音轨

`Shot.original_audio` 默认 `mute`。H3即使返回有声MP4，MJ也剥离原声并混入选定旁白；不会把两套话同时播放。

要保留H3原声，手动把该镜头设成 `keep`、清除 `audio_asset`，可调 `original_audio_gain`。两者同时存在时拒绝合成。保留的是完整原始音轨，不声称自动分离对白/音乐/环境声；纯无音轨素材不能选keep。

MiniMax返回hex MP3先暂存、记哈希再转48k WAV，之后复用本地素材。选中TTS带台词哈希，修改文字后旧配音不再可用，需要重配/重新选用；改变字幕字体不再生视频。

字幕当前依照已测音频长度按文字比例排时，**不等于词级强制对齐**，仍须试听与字幕审核。实际配音太长会阻塞而不是加速/截断台词。完整3600帧时间线、中文字体、多轨混音沿用原有模块。

## 8. 长任务与恢复

- 一个生成尝试只提交一次POST；重新创作是新任务/新预占，不能当网络重试。
- H3先保存task_id再轮询；未知status、任务ID或model不匹配、超时进入 `reconciling`。
- Qwen先保存已返回图片URL再下载。网络失败只查/下载同一结果；未知CDN经管理员审核后可以“查原任务”续取，不重新生图。临时URL过期时不能保证恢复。
- 旁白先保存MP3路径+哈希再转码；文件丢失/变化时停止，不再次请求TTS。
- 发出POST后连接断开、没有收到ID时，服务商可能已扣费。当前H3 API没有已验证客户端幂等查回能力，因此暂停人工核对；**不承诺exactly-once**。
- 每个原生接口origin/type默认1个在途任务；未确定提交仍占位，不靠疯狂重试积累费用。超时重试GET有界，旧Worker的fence不能覆盖新结果。
- 取消后远端仍可能完成；保存未激活候选，不自动用于成片，不声称已退款。无原始ID的未知任务暂不提供“强行清零”按钮。
- `hosts/key_env/base_url/type`访问策略变更会阻塞原任务；管理员可单独调整已验证`download_hosts`以恢复下载。密钥可以轮换但不得切到不拥有原任务的账户。

## 9. 当前明确不做

不自动开启付费、不为试样片大额充值；不训练/安装GPU模型；不把H3支持原生声音等同于稳定多角色音色或精确口型；不自动通过视觉动作审核；不保证下游服务的免费重试；不将API协议桩的小色块/正弦音称为正式漫剧。
