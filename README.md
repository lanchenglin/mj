# MJ · 原创漫剧工作台 v0.4

**当前路线：MJ＋云端图片＋H3 API＋独立旁白。**文本、图像、视频和声音均通过 API 调用；ComfyUI 保留为可选独立后端，默认无需部署。MJ 本身只需要 CPU 服务器、数据库和 FFmpeg，不在业务进程加载 GPU 模型。

从原创构想、可审核剧本、角色设定到分镜、候选素材、独立声音和成片合成。现有 API/Worker/Web 及工程链路有测试，**不代表真实模型效果、角色一致性或首条正式漫剧已验收**。本次付费模型调用为0。

## 先读这两份

- [v0.4 API 配置、制作顺序与恢复规则](docs/10-api-first.md)
- [v0.4 实际验证记录与未验证项](docs/11-api-verification.md)
- [云 API 配置样例](providers.cloud.example.json)：文本 / Qwen图片 / H3视频 / MiniMax旁白。
- [可选 ComfyUI 独立部署](docs/08-comfyui-deployment.md)：沿用 v0.3，不删除、不强制启用。

## API 链路

```text
原创需求 → 文本API/人工编辑 → 故事与分镜批准
      → Qwen图片API：角色、场景、参考编辑、分镜图
      → H3 API：按镜头生成视频（首/尾帧或多素材参考，互斥）
      → 独立TTS：已保存的分镜台词 → 试听/选用
      → MJ：候选审核、局部重做、字幕、多轨混音、FFmpeg
      → 15秒样片批准 → 全片制作 → 120秒视频与项目包
```

新任务冻结模型、参数、参考顺序、声音与台词；收到结果只保存待审核候选，不自动覆盖角色或分镜。H3原生音轨默认不入片；保留原声与选定独立旁白不能同时开启。改台词会阻止继续用旧TTS，改字幕样式不会重新生成视频。

## 离线运行（不需要 API Key）

需要 Python 3.11+、FFmpeg/ffprobe、fontconfig、Noto Sans CJK 字体。前端是随包提供的 HTML/CSS/ES modules，不需要 npm/CDN 或前端构建。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m mj.cli init
python -m mj.cli doctor
python -m mj.cli serve --with-worker --port 8080
```

打开 `http://127.0.0.1:8080`。首次 init 使用0600权限创建 `.env`，管理员密码在服务器本地读取，不贴到聊天、日志或Git。离线SQLite模式可编辑和使用自有素材、跑MOCK工程预演，但强制禁止外部生成。

## 生产部署与云接口

生产数据库使用 PostgreSQL，独立 Worker 与 Alembic 迁移。没有新增表或列，不修改已发布的初始迁移；请先备份数据库和媒体。

```bash
# 新目录；若存在.env则拒绝覆盖
python scripts/init_production.py
# 阅读/合并 providers.cloud.example.json 到 providers.json
# 在服务器设置 Key、正数预占值、实际币种、可用声音ID
# 安装Docker后（目标环境的部署测试仍需单独执行）：
docker compose up --build -d
```

`.env` 中配置 `MJ_PROVIDERS_FILE`、`MJ_QWEN_API_KEY`、`MJ_MINIMAX_API_KEY`；审核配置后才设 `MJ_EXTERNAL_ENABLED=true`。`MJ_COMFYUI_ENABLED=false` 可继续保留。Compose 只发布127.0.0.1:8080，不裸露数据库；跨主机网页访问用可信隧道或HTTPS反向代理。HTTPS时配置 `MJ_SECURE_COOKIE=true` 与精确 `MJ_PUBLIC_ORIGIN`。

```bash
python -m mj.cli api-check
```

这是**零网络请求的本地配置检查**，不是 Key 有效性、账户额度或生成效果验证。原生协议路径、配置细节和白名单见 [10](docs/10-api-first.md)。

示例模型使用已核对的接口名称，但预占/费率是0，TTS音色为空：**这是故意阻止未授权消费，不是开箱自动收费**。须按实际账户填正预占、创建项目预算、完成所需阶段审批。一个项目可以分别授权CNY/USD，互不挪用，不作无来源换汇。

## 实现范围

| 模块 | 当前范围 |
|---|---|
| 创作 | 项目、三候选方案、结构化剧本、角色/场景/道具与分镜 |
| 图片 | Qwen2.0同步文生图/最多3图参考编辑；保留兼容图片API和ComfyUI |
| 视频 | MiniMax H3 V2文字/首帧/尾帧/首尾帧/参考模式；保留原fal队列 |
| 旁白 | MiniMax Speech2.8非流式、音色/语速/音调/发音词典；保留兼容TTS |
| 任务 | 幂等、版本冻结、预算预占、已知ID查单、结果暂存、取消晚到候选 |
| 后期 | 帧级视频时间线、独立音轨、中文分句、合成和哈希导出包 |
| 安全 | 登录/CSRF/所有权、管理员端点白名单、TLS/IP固定、下载不携带API密钥 |

原生视频发出请求但未取得ID时会停在待核对，不盲目重试；图片取得URL后下载失败也不会重新生图。收到usage不等于费用已结算，仍按真实账单证据登记。未知任务继续占并发额度。没有“自动永久授权”或“失败一定免费”的假设。

当前字幕按实测声音长度比例排时，不是词级对齐；人物动作与音色需人工审核；精确口型、自动拆分原生声音、复杂视频转场、多租户SaaS、自动发布不在本次完成范围。

## 测试

```bash
pip install -e '.[test]'
python -m pytest --disable-warnings
node --check mj/web/app.js
node --check mj/web/cloud-ui.js
PYTHONPATH=. python scripts/verify_120s.py
# Chromium进程内组件检查，不代表真实HTTP/CSP联调
PYTHONPATH=. python scripts/browser_cloud_check.py
```

v0.4新增原生协议/任务/媒体测试，所有远端网络均拦截到合成协议桩；实际执行FFmpeg检查15秒与120秒工程输出。细目与未验证项以 [11](docs/11-api-verification.md) 为准，不沿用过去未核验数字，也不把测试数量等同初始大纲全部验收。

## 历史、许可与升级

Git仓库保留原 `docs/00–05` 目标设计；v0.2实际说明为06–07、ComfyUI为08–09、当前API路线为10–11。发布的应用ZIP不包含用户数据/权重/字体；原始规划文件在Git中保留。

没有复制上游完整平台业务代码。MJ自有代码许可证仍由仓库所有者选择，依赖、FFmpeg构建、模型服务、参考素材与声音许可分别核查。不得上传.env、providers.json、数据库、密钥和用户媒体。
