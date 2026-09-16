# 11 · v0.4 实际验证记录

日期：2026-09-16。代码基于 main `aaf7a2cb44521c4c616ac863cfcb220c0f44ec0f`，本次不是重新引用历史测试。

## 执行结果

- `python -m pytest --disable-warnings -o faulthandler_timeout=15`：**175 passed in 27.96s**。包含原106项与新增69项API测试。
- `node --check mj/web/app.js`、`node --check mj/web/cloud-ui.js`通过。
- Python wheel构建成功；网页ES模块、三份ComfyUI模板继续作为package data分发。
- `scripts/browser_cloud_check.py`：Chromium组件测试通过图片表单/画幅默认、图片dry-run、H3互斥模式、独立旁白设置、供应商类型过滤、390px布局；0 pageerror、0外部请求。ComfyUI既有组件流程另跑通过。
- 浏览器采用**进程内TestClient组件桥**，内联已知本地模块；未修改生产CSP/网络策略。不能把此结果叫真实HTTP/HTTPS、Cookie部署联调。
- `test_15s_native_protocol_to_render` 实际走图片候选、3个H3视频任务、3个独立TTS任务、人工测试审核/选用、FFmpeg样片合成：450帧、720000采样、15秒；原生音轨默认不进入声音总线。所有API调用为拦截后的协议桩，素材是合成运动图案/正弦音，**不是AI生成质量验收**。
- `scripts/verify_120s.py`本次重新执行：20镜头MOCK预演，360×640、30fps、3600帧、120.0秒、5760000采样。预演带MOCK标记。

## 回归重点

原生请求字段/路由、H3首尾帧与混合参考互斥、片长覆盖裁剪入点、样片组连续、参考归属和审核、CNY/USD隔离、预占和真实用量区别、旧请求幂等兼容、断线不重新POST、原始task_id查单、下载白名单人工调整后续取、TTS暂存完整性、取消晚到候选、原声/独立音轨冲突、台词变化阻止旧音频、精确DNS/IP/TLS SNI、禁止私网和重定向、JSON/媒体错误、默认不开收费。

第一轮新增测试有一个fixture使用3字符幂等键，被实际规则拒绝；改用合法测试键。浏览器与pytest合并执行超出工具总时限，后续单独完整pytest运行正常完成；不把被中断运行当作通过。

## 未验证

真实Qwen/H3/MiniMax账号、区域权限、收费生成、参考素材质量、中文音色/时序、角色动作一致性、真实账单；PostgreSQL并发故障恢复、Docker/真实服务器部署、公网TLS/CSP/灾备；原始大纲全部验收。外部付费调用0、真实GPU调用0。

配置和代码存在≠所有账号都已开通；HTTP成功≠素材可用；技术测试通过≠真实G3/G4终审。机器记录见 `api-verification.json`，终端输出见 `api-test-output.txt`。Git是否完成以远端ref回读为准，不以本报告代替。
