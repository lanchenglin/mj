# MJ v0.3 · ComfyUI 自托管生图接入

本次在 v0.2 基础上新增独立 GPU 服务器接入：文生图、参考图改图、蒙版重绘、不可变工作流、查回原任务、候选审核与安全传输。**未实际部署到用户服务器、未验证真实模型画质。**

- [ComfyUI 分服务器部署与使用](docs/08-comfyui-deployment.md)
- [v0.3 实际测试与未验证项](docs/09-comfyui-verification.md)
- [私网供应商配置样例](providers.comfyui.example.json)
- [Nginx 认证代理样例](deploy/comfyui/nginx.conf.example)

云模型与 ComfyUI 使用独立开关；自托管费用记为未计量，不虚构免费账单。模型权重不随仓库分发。以下 v0.2 使用说明仍适用，其验证数字是历史记录。

---

# MJ · 漫境原创漫剧工作台

**v0.2.0 开发版**。从原创构想、剧本和角色设定，到分镜、候选素材、独立声音、预演和成片合成。先做原创漫剧，不包含影视原片检索或自动发布。

这次交付包含可运行的 Web/API/Worker、数据库迁移、模拟/外部服务适配、FFmpeg 合成及测试。**工程链路已测试，不等于真实模型制作的漫剧已经验收**：没有调用付费模型，没有验证真实人物动作、角色一致性与中文音色，也没有把占位卡片称为正式作品。

## 先运行离线工作台

需要 Python 3.11+、FFmpeg/ffprobe、fontconfig 和 Noto Sans CJK 字体；Linux 优先。前端是随包提供的 HTML/CSS/ES modules，不需要 Node.js、npm、CDN 或前端构建。

```bash
# 在解压后的源码目录，或含完整实现的仓库检出目录中执行
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m mj.cli init
python -m mj.cli doctor
python -m mj.cli serve --with-worker --port 8080
```

浏览器打开 `http://127.0.0.1:8080`。初始化会以仅当前用户可读写的权限创建 `.env`，请在本机查看 `MJ_ADMIN_PASSWORD`，不要提交或转发该文件。端口占用时改成其他端口。Windows 建议 Docker/WSL；当前没有原生 Windows 实测记录。

离线模式使用 SQLite，仅用于本地编辑、自己的素材和工程测试，**强制禁止外部调用**。点击“创建离线演示”，可以编辑 20 个镜头并制作完整 120 秒预演。模拟配音是测试音，不是中文 TTS；预演会带 MOCK 标记，无法通过真实成片批准。

## 已实现的工作流

1. 创建项目，自定义故事、画风、横竖画幅、时长与帧率。
2. 手写或生成候选方案，审阅后应用；编辑剧本的动作、台词与声音归属。
3. 管理角色、场景、道具和参考素材；编辑镜头动作及前后状态。
4. 上传或生成图片、视频、音频；所有文件以内容哈希保存，先审核再选用。
5. 制作完整动态分镜预演、10–20 秒相邻镜头样片，再制作全片。
6. 每镜头替换素材、裁剪入点、调整顺序；旁白、音乐、音效独立混音。
7. 输出干净 MP4、字幕 MP4、SRT、声音分轨、时间线、工作区、QA 和带哈希的项目包。

版本冲突返回 409，模型结果不会自动覆盖工作区，晚到任务不会自动激活素材。正式人物动作镜头拒绝使用静图或 MOCK 摄像运动代替。需要真实运动的素材必须通过人工审核。

## 外部服务与生产部署

生产配置使用 PostgreSQL + 单独 Worker + Alembic，SQLite 不提供生产替代路径。

```bash
# 新目录中执行；已有 .env 不会被覆盖
python scripts/init_production.py
# 先检查 .env、providers.json、Dockerfile 和 compose.yaml
# 安装 Docker 后执行（本次未实际验证 Docker/PG 部署）
docker compose up --build -d
```

Compose 只将网页端口绑定宿主机 `127.0.0.1`，数据库不发布公网端口。远程使用需要 HTTPS 反向代理或可信隧道。HTTPS 时设置 `MJ_SECURE_COOKIE=true` 和精确的 `MJ_PUBLIC_ORIGIN=https://你的域名`。

参照 `providers.example.json` 写管理员维护的 `providers.json`；密钥只能放到 `key_env` 指向的服务端环境变量。示例故意使用占位模型名、零预占值和空视频时长档位，不能当作可直接消费的配置。

当前接口适配：
- OpenAI-compatible：`chat/completions` JSON 文本、`images/generations` / `images/edits` PNG 图片、`audio/speech` WAV。
- fal Queue：单张分镜参考图的视频异步提交、查询与受限下载。每个模型的 endpoint、图片字段、时长档位和下载域名须按该模型文档配置；不假设所有模型同一种参数。
- Mock：明确标记的本地结构模板、占位图、摄像运动和测试音频。

启用外部服务要同时满足：生产模式、`MJ_EXTERNAL_ENABLED=true`、有效供应商配置及凭据、用户在工作台中明确授权预算、所需阶段审批、参考图外发授权。**预算是本地准入与风险预占，不是供应商价格承诺**。结果未知时留在 `reconciling`，不自动再次 POST；费用由用户按真实账单结算。取消不代表远端停机或退款。

具体操作、实现差异和待验证项见 [实现与运行说明](docs/06-implementation.md)。

## 测试与验证

```bash
pip install -e '.[test]'
python -m pytest
node --check mj/web/app.js   # 可选：仅检查 JS 语法
PYTHONPATH=. python scripts/verify_120s.py
```

浏览器冒烟测试见 `scripts/browser_check.py`，使用环境变量 `MJ_TEST_PASSWORD`，可通过 `MJ_TEST_URL` 连接实际运行的演示服务。本次浏览器网络被环境限制，因此实测采用 `MJ_BROWSER_BRIDGE=1` 的 TestClient 进程内测试桥；它验证真实浏览器交互与后端接口，但不等于部署后的 HTTP/CSP 联调。

[本次实测记录](docs/verification.json) 与 [验证说明](docs/07-verification.md) 区分通过、模拟与未运行项。旧大纲中的 T01–T24 是更广的验收计划，不能将本次测试数直接等同于全部计划完成。

## 范围与许可

本版采用独立实现，没有复制 ArcReel/Toonflow/OpenMontage 的代码，也没有把 video-use 的源文件混入 MJ。video-use 仅作为后期架构研究参考；对其“字幕最后合成”等思想进行了独立实现。

MJ 自有代码的对外分发许可证仍由仓库所有者决定，本次不擅自添加项目级 LICENSE。依赖、FFmpeg 构建、模型服务、参考图片、声音和音乐的许可需分别核查。字体通过系统包安装，不随源码分发字体文件。

保留原有 `docs/00–05` 作为目标设计；v0.2 的实际实现与限制以 `docs/06–07` 为准。本源码交付包只包含本次实现文件；原始规划文件仍在 GitHub 规划提交中。
