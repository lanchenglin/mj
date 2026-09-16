# 08 · ComfyUI 独立服务器接入（MJ v0.3）

本页是实际代码对应的配置指南。适配器已做离线、HTTP 协议桩和界面组件测试；**没有在真实 GPU、模型权重、PostgreSQL 或公网 HTTPS 环境验收**。不要将其描述为已经部署到用户服务器。

## 1. 可以分开部署吗？可以

```
浏览器 → 服务器 A：MJ API / Web / PostgreSQL / Worker / FFmpeg
                       │
                       └─ 私网/VPN，或有认证的 HTTPS
                                      │
                            服务器 B：ComfyUI / CUDA / GPU / 模型
```

A 不必有 GPU；B 可以是另一台物理机、租用的 GPU 云主机或通过 VPN 可达的工作站。它们不需要共享磁盘：MJ 上传本次获准使用的参考图，ComfyUI 执行后，MJ 下载结果到自己的内容寻址存储。浏览器只访问 MJ，不持有 ComfyUI 凭据，不需要为此开放跨域 CORS。

ComfyUI 原生就是 HTTP 服务，可不打开界面运行。官方服务说明与路由参考：
- https://docs.comfy.org/development/comfyui-server/comms_overview
- https://docs.comfy.org/development/comfyui-server/comms_routes
- https://docs.comfy.org/development/comfyui-server/api-examples
- https://docs.comfy.org/interface/features/api-nodes
- https://docs.comfy.org/development/comfyui-server/startup-flags

本适配器使用 `/object_info`、`/system_stats`、`/upload/image`、`/prompt`、`/history`、`/history/{prompt_id}`、`/queue`、`/view`。首版用 HTTP 轮询展示排队/运行阶段，**未实现 WebSocket 的逐步百分比或实时预览**。它接的是自托管 ComfyUI，不是 Comfy Cloud 的账户 SDK。

## 2. 本版交付什么

- 图片任务的三个固定模板：`sdxl-text` 文生图、`sdxl-reference` 单参考图改图、`sdxl-inpaint` 单参考图+蒙版重绘。
- 工作流及配置仅由服务器管理员维护，项目请求只能选择模板 ID、描述、种子、尺寸、改图强度和已审核图片，不接受任意节点图或服务器 URL。
- 上传前验证参考图属于该项目、已审核、不是 MOCK；蒙版原始尺寸必须与第一张参考图相同。白色重绘、黑色保留；请上传不透明黑白图。
- 任务冻结模板及哈希、参考图顺序、种子和参数，结果仅进入待审核候选；不自动改变角色图或分镜选中项。
- 每个任务生成一个候选，最多 2,097,152 像素，宽高 256–2048 且为 8 的倍数。默认 768×1024；本地参考上传/归一化限 16 MiB，返回限 32 MiB。
- GPU 通道单独开关；默认每个 ComfyUI origin 最多 1 个在途任务。这里只限制 MJ 的提交，不限制有人绕开 MJ 直接给 ComfyUI 派任务。

**模板是基于核心节点的 SDXL 协议起点，不包含权重，也不是已验证的角色一致性算法。**普通 img2img 不保证锁脸；VAE 重绘不保证蒙版外像素逐位不变。Qwen/FLUX/IP-Adapter/ControlNet 需要另备适配模型的 API 工作流、节点与样例验收，不是把 checkpoint 名改成另一个架构就能跑。

当前仍只接 ComfyUI 生图，未把 ComfyUI 视频/音频接入 MJ。原有 fal 视频、云端文本/TTS 路线保持独立。

## 3. 先配置服务器 B（GPU）

按 ComfyUI 官方安装说明安装匹配 GPU 驱动的 PyTorch；固定 ComfyUI commit、PyTorch 版本、节点和模型哈希。不能根据 CPU 服务器的 Python 环境推断 GPU 环境已兼容。

基础模板期望 `models/checkpoints/sd_xl_base_1.0.safetensors`。取得相应权重及许可，或由管理员在模板的 `CheckpointLoaderSimple.ckpt_name` 中改为本机同架构模型的真实文件名。不要把模型、私有参考图、账号 Key 提交 Git。

同机反向代理或 SSH/VPN 隧道的启动示例：

```bash
cd /opt/ComfyUI
.venv/bin/python main.py --listen 127.0.0.1 --port 8188 \
  --disable-auto-launch --disable-api-nodes --disable-all-custom-nodes
```

`--disable-api-nodes` 禁用的是可能调用外部收费服务的 Partner/API 节点，**不是关闭本地 `/prompt` 等 HTTP 接口**。若使用自定义节点，必须审查后明确启用，不能先全开放再相信 `local_models_only=true` 字样；这个配置不能替代节点审计或出站防火墙。

`deploy/comfyui/comfyui.service.example` 是可修改的 systemd 模板，不会自动创建用户、安装 CUDA/模型或开放端口。输入、输出、user 目录须提前创建并交给该非 root 用户。服务重启可能清空内存队列/历史；MJ 不会因此认定旧任务没执行。

## 4. 连接方式 A：受保护的私网/VPN（优先）

在两机已经处于可信私网或加密 VPN 的前提下，将 B 的监听地址改成实际 VPN/内网 IP，并只允许 A 的出站 IP 访问 8188。不要因为地址是 10.x 就假设网络一定可信。

把 `providers.comfyui.example.json` 复制为**不入 Git 的** `providers.json`，替换 `base_url`、`hosts`、`allowed_ips`。示例 IP 10.10.10.20 不是用户已提供的服务器。

- `hosts` 必须精确匹配 origin 主机名。
- `allowed_ips` 是 1–16 个精确地址，不允许 `0.0.0.0/0` 或 CIDR 放行。
- 私网需要 `allow_private=true`；环回隧道需另设 `allow_loopback=true`。
- HTTP 只在显式 `allow_insecure_http=true` 且所有地址为已批准私网/环回时允许；公网 HTTP 始终拒绝。
- 公有云元数据、link-local、多播、未指定和 IPv4-mapped 地址始终拒绝。

## 5. 连接方式 B：公网独立域名 + HTTPS + 认证

不要裸露 8188。让 B 的 ComfyUI 只监听 127.0.0.1，由 Nginx 等代理提供可信证书、Bearer 验证以及来源 IP 限制。参考 `deploy/comfyui/nginx.conf.example`，替换域名、证书路径和 A 的出站 IP，并在服务器上执行 `nginx -t` 后再加载。

代理的 secret include 文件内容形如 `"Bearer <随机令牌>" 1;`，只保存在服务器、设为 0600；同一令牌在 A 侧通过环境变量 `MJ_COMFYUI_TOKEN` 提供。这是我们部署的代理认证，**不是假定原生 ComfyUI 会验证任意 Bearer，也不是 Comfy Cloud Key**。

MJ 的配置改为实际值：

```json
{
  "type": "comfyui",
  "base_url": "https://gpu.example.com",
  "hosts": ["gpu.example.com"],
  "allowed_ips": ["填写已核验的真实公网IP"],
  "allow_private": false,
  "allow_loopback": false,
  "allow_insecure_http": false,
  "auth": "bearer",
  "key_env": "MJ_COMFYUI_TOKEN",
  "local_models_only": true,
  "max_inflight": 1,
  "remote_timeout_seconds": 1800,
  "default_size": [768, 1024],
  "default_workflow": "sdxl-text",
  "workflows": {
    "sdxl-text": "sdxl-text.json",
    "sdxl-reference": "sdxl-reference.json",
    "sdxl-inpaint": "sdxl-inpaint.json"
  }
}
```

这个对象仍需放在 `providers.json` 的供应商 ID 键下，如 `{"comfy-gpu": {...}}`。占位 IP 不可直接启动。

请求先解析并核对全部 DNS 地址，然后连接批准 IP，同时保留原 Host/TLS SNI 验证；不继承环境代理、不接受重定向、不关闭证书验证。IP 变更需管理员核实配置。HTTPX 的 SNI 扩展依据：https://www.python-httpx.org/advanced/extensions/#sni_hostname 。

代理示例只放行本版需要的接口，不暴露 UI、扩展管理、清队列或全局 `/interrupt`。部署者仍需限制 B 的出站网络及 GPU 用户的目录权限；反向代理不是模型节点的代码沙箱。

## 6. 配置服务器 A（MJ）

生产数据库用 PostgreSQL，按 README 完成初始化/迁移，不修改已发布的 0001 迁移。没有新增数据库列，升级不需要破坏现有数据。

```dotenv
MJ_MODE=production
MJ_COMFYUI_ENABLED=true
MJ_EXTERNAL_ENABLED=false
MJ_PROVIDERS_FILE=/absolute/path/providers.json
# 其余已有数据库、管理员密码、Cookie/Origin 设置保持正确
# HTTPS 代理模式还需注入 MJ_COMFYUI_TOKEN，不能放到前端
```

`MJ_EXTERNAL_ENABLED=false` 只关闭云模型通道，不妨碍已单独批准的 ComfyUI 自托管通道；需要云端写稿/视频/TTS 再单独启用并批准预算。demo/test 模式仍不允许启用远程生成。

API 和 Worker 必须读取相同配置与模板；改配置后重启两者。模板默认随 Python 包安装；自定义模板可通过 `workflow_dir` 指向管理员只读目录。容器需将该目录以同一路径只读挂给 API 与 Worker。任务会冻结模板版本，不会随文件变化偷换正在排队的生成内容。

只检查连通性、节点和模型名称（无图片上传，无生成调用）：

```bash
python -m mj.cli comfy-check --provider comfy-gpu
```

或在 MJ「模型与预算」里点「检查节点/模型」。返回 `quality_verified=false` 是刻意设计：GET 检查通过不等于真能出图、显存足够或角色稳定。

## 7. 在 MJ 里怎么使用

1. 保存原创需求、剧本和 G1；角色页点「生成定妆/场景图」，或分镜卡片点「生图」。
2. 供应商选 `comfy-gpu`，选择文生图/改图/重绘模板。参考图从已审核且非 MOCK 的本项目素材中按顺序选取；没有隐式追加。
3. 点「仅预检」只做本地契约检查，不访问 GPU。提交真实任务前勾选提示词/参考图/蒙版外发和计算资源授权。
4. 任务页查看排队、运行、失败或待核对；成功图仍需人工审核，再关联到角色或镜头。

自托管没有供应商逐张 API 账单，因此 `fee_state=unmetered`，**不是免费**。GPU 租赁、电力、存储、流量尚未计量；不会扣云 API 预算，也不会伪造 0 元实际账单。付费 Partner 节点不在此适配器支持范围。

## 8. 失败、重试与取消

- 单个 MJ 任务只发送一次 `/prompt`。图像效果不好想再生成，是一个新的任务；需要重新确认。
- 提交应答丢失：用冻结的客户端标记和工作流哈希查队列/历史；只有唯一匹配才接回原 `prompt_id`。没有记录不证明没执行；多个匹配也阻塞。
- 已知 `prompt_id` 只轮询/下载，不再次提交。远端等待超时、历史消失、访问策略改变进入 `reconciling`；未知任务继续占本端在途名额，避免洪泛。
- 历史查回最多检查最近 1000 条。如果 GPU 重启/清历史后无法查回，需要管理员核对原服务器；当前没有「强行清零未知任务」按钮，不应直接改数据库掩盖未知结果。
- 取消标记阻止结果自动继续使用。**不调用共享服务器的全局 interrupt**；已提交 GPU 工作可能仍完成，晚到图片保留为未激活候选，不声称停止计算或退款。
- 修改模板/seed/参考图产生新指纹，旧候选不会覆盖当前版本。某个旧任务的访问配置被管理员撤销时，不会继续使用其旧地址。

## 9. 实际测试边界

参见 `docs/09-comfyui-verification.md`。协议桩产生的蓝色小图只验证上传、传输、查询、落库和审核边界；虽然模拟远端在协议测试里返回 `mock=false`，它仍是测试 fixture，不能作为真实模型能力验收。

下一步需要：真实 GPU 服务器地址与可操作连接、选择好的 SDXL checkpoint 及许可、同一原创角色的参考/蒙版样本，以及明确的计算资源与素材外发授权。在目标服务器实测节点元数据、显存峰值、输出效果和浏览器访问后，才确认部署与创作品质通过。
