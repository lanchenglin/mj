# 09 · v0.3 实际验证记录

日期：2026-09-16；基于 main `55cab4d9d269d3fa32a9919ad4e5fc298d62702b`。记录的是本次执行，不把历史结果重新标成实测。

## 已完成

- 原 v0.2 基线：51 项测试通过；本次最终完整运行 `python -m pytest -vv -o faulthandler_timeout=15`：**106 passed in 28.50s**，其中 55 项 ComfyUI 测试，0 failed。
- `node --check mj/web/app.js` 通过。
- `python -m pip wheel . --no-deps --no-build-isolation` 成功，检查安装包包含三份工作流。
- HTTP 协议测试真正经过环回 TCP：上传参考图 → 提交 API 格式图 → 查原任务 → 下载 → 落为待审核候选。服务端返回程序生成的测试图片，并非实际 ComfyUI 或 GPU。
- Chromium 进程内组件测试：登录、文生图/参考/蒙版表单、预算隐藏、dry-run、设置页、390px 手机布局；0 JS pageerror，无横向溢出，0 外部请求。

## 测试中的问题和边界

- 首轮新增测试的辅助函数重复传 provider 参数，修正后通过；不是把生产逻辑删掉来通过用例。
- 两次完整运行在工具 45s/90s 限时中断，之后启用 faulthandler 的完整运行正常完成；没有确定其根因，不声称已经修复某个已证明的死锁。
- 浏览器原生 HTTP 联调已尝试，被环境策略 `ERR_BLOCKED_BY_ADMINISTRATOR` 拦截。没有关闭策略、改代理或关闭 CSP；组件测试采用独立 TestClient 桥接，不能替代真实浏览器 HTTP/TLS/CSP 验收。
- 小型 FFmpeg 原测试随完整回归通过；本次未重新运行完整 120 秒预演，历史 120 秒结果仍属于 v0.2。

## 覆盖重点

固定 origin/精确 IP/DNS 与 TLS SNI、拒绝重定向与危险地址、服务器模板与节点白名单、跨项目参考隔离、蒙版尺寸、非 MOCK 已审核参考、请求幂等/种子冻结、未知提交唯一查回、历史丢失停住、单服务器并发额度、过期 Worker 防覆盖、取消后晚到结果、远端等待超时/临时失败、损坏图拒绝、只读连通性探测、安装包数据完整性。

## 尚未验收

真实 ComfyUI/GPU/模型效果与显存；人物身份与服装稳定性；真实局部重绘质量；目标服务器网络和 HTTPS；PostgreSQL 并发、Docker、systemd/Nginx 示例、生产灾备与全部初始大纲验收。云模型付费调用为 0，真实 GPU 生成调用为 0。

源码文件是适配器，不包含任何模型权重或已生成的正式漫剧。实际日志见 `comfyui-test-output.txt`，机器可读记录见 `comfyui-verification.json`。Git 发布是否完成以远端提交回读为准，不以本报告替代。
