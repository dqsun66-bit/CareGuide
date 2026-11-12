# Seedance 视频工作台

基于智云 TokenHub `doubao-seedance-2-0-260128` 模型的 AI 视频生成 Web 工作台：
文生视频 / 图生视频、任务轮询、视频预览与下载、历史记录、费用提示。

作为 CareGuide 仓库内的**独立子项目**运行，不改动 `careguide/` 与 `frontend/` 任何代码。

```
seedance-workbench/
├── backend/          # FastAPI 服务（Python 3.11）
│   ├── main.py       # 入口：uvicorn backend.main:app
│   ├── routes.py     # API 路由与响应信封
│   ├── client.py     # TokenHub HTTP 封装（中文错误翻译）
│   ├── mock.py       # Mock 客户端（无 Key 时全流程可测）
│   ├── poller.py     # 后台轮询 + 服务重启恢复
│   ├── store.py      # SQLite 任务与临时视频元数据
│   ├── validation.py # 参数校验与请求构造
│   └── tests/        # pytest 测试（58 个用例）
├── frontend/         # Vue 3 + Vite
│   ├── src/          # App.vue + 组件
│   └── tests/        # node --test 测试（9 个用例）
└── data/             # 运行时数据（seedance.db / uploads / video_uploads）
```

## 快速开始（Mock 模式，零配置零费用）

不配置 API Key 时自动进入 **Mock 模式**：完整走 排队→生成→成功/失败 流程，
不会发起任何真实请求、不产生费用。

```powershell
cd D:\codex\CareGuide\seedance-workbench

# 1. 安装依赖（复用 CareGuide 的 .venv）
..\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 启动后端（8001）
..\.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8001

# 3. 另开终端，启动前端开发服务器（5176，/api 自动代理到 8001）
cd frontend
npm install
npm run dev
```

打开 <http://127.0.0.1:5176>。页面顶部显示「模拟模式 · 不扣费」横幅；
创作面板底部有「模拟结果」开关，可选成功/失败路径测试。

## 从本地上传参考视频

视频参考和编辑视频模式提供“上传本地视频”按钮，支持 MP4/MOV、单个 2～15 秒、最大 50MB。工作台会先在本机验证文件，再通过随机、临时的 Cloudflare HTTPS 地址仅向 Seedance/TokenHub 提供该视频。任务到达成功、失败或超时状态并经过五分钟宽限期后，临时文件和通道会自动清理。

本地上传需要 `ffprobe` 可从命令行运行，并满足以下任意一项：

- `cloudflared` 位于系统 `PATH`；
- 可执行文件位于项目的 `tools/cloudflared.exe`；
- `.env` 中的 `SEEDANCE_CLOUDFLARED_PATH` 指向可执行文件。

```dotenv
SEEDANCE_MAX_VIDEO_BYTES=52428800
SEEDANCE_CLOUDFLARED_PATH=
SEEDANCE_VIDEO_UPLOAD_TTL_SECONDS=1800
SEEDANCE_VIDEO_POST_TASK_GRACE_SECONDS=300
SEEDANCE_TUNNEL_START_TIMEOUT_SECONDS=30
```

生成期间视频会通过 Cloudflare Tunnel 传输，并由 Seedance/TokenHub 通过不可猜测的随机路径拉取。工作台接口、目录列表、数据库和其他本地文件不会通过该通道公开。如果通道启动失败，仍可使用原有公网 HTTPS URL 或 `asset://` 素材 ID。

### 生产模式（单端口）

```powershell
cd frontend && npm run build
cd .. && ..\.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8001
```

打开 <http://127.0.0.1:8001>（后端自动托管 `frontend/dist`）。

## 真实模式

1. 复制 `.env.example` 为 `.env`，填入你的智云 TokenHub API Key：

   ```env
   TOKENHUB_API_KEY=你的密钥
   ```

2. 重启后端。健康检查确认模式切换：

   ```powershell
   curl http://127.0.0.1:8001/api/seedance/health
   # {"ok":true,"data":{"mode":"real","api_key_configured":true,...}}
   ```

3. 页面顶部变为「真实模式 · 消耗额度」，提交前会显示费用提醒。

> 图生视频以 Base64 内嵌图片发送。若平台返回参数错误，页面会明确提示，
> 届时可将图片先上传到图床，改用公网图片 URL 方案。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TOKENHUB_API_KEY` | 空 | 智云 TokenHub 密钥；为空自动 Mock |
| `TOKENHUB_BASE_URL` | `https://aigw.telecomjs.com` | 网关地址 |
| `TOKENHUB_SEEDANCE_MODEL` | `doubao-seedance-2-0-260128` | 模型 ID |
| `SEEDANCE_MOCK` | `auto` | `auto` 无 Key 自动 Mock；`true` 强制 Mock；`false` 强制真实 |
| `SEEDANCE_POLL_INTERVAL_SECONDS` | `10` | 任务轮询间隔 |
| `SEEDANCE_MAX_WAIT_SECONDS` | `900` | 单任务最长等待；超时标记失败（可用「刷新状态」只读查询） |
| `SEEDANCE_RESUME_WINDOW_SECONDS` | `1800` | 服务重启后恢复轮询的时间窗口 |
| `SEEDANCE_PRICE_CNY_PER_SECOND` | 空 | 可信价格（元/秒），配置后前端显示预估费用 |
| `SEEDANCE_MAX_IMAGE_BYTES` | `8388608` | 图片大小上限（8MB） |
| `SEEDANCE_MAX_GENERATIONS_PER_HOUR` | `20` | 创建接口每小时限流次数（0 为不限制） |
| `SEEDANCE_DATA_DIR` | `./data` | SQLite 任务库与上传目录（相对路径以项目根为基准） |

## 防重复扣费设计（重要）

- 每次「生成视频」携带一个 `client_request_id`（UUID）。后端对其建唯一约束：
  双击、重试、页面刷新后重新提交，都命中同一条记录，**绝不会对同一表单重复 POST**
  （并发竞态由数据库唯一约束兜底，后到者返回已有任务）。
- 提交超时或网关 502/503/504 后任务标记为「结果未知」：后端不自动重试 POST，
  前端也**保留幂等键**——用户重试时复用同一键，命中已有记录而非重新 POST。
  提供「刷新状态」按钮走只读 GET 查询。
- 页面刷新后，通过 sessionStorage 中的幂等键调用 `GET /tasks/by-crid/{crid}`
  精确找回任务并恢复轮询；只有确认后端不存在该任务时才清除键。
- 服务重启后，对窗口内仍在进行的任务自动恢复轮询。

## 安全说明

- **仅本机使用**：后端默认绑定 127.0.0.1；写操作（POST/DELETE）校验 Origin
  头，恶意网页无法跨站表单触发扣费；Vite 开发服务器仅监听 127.0.0.1。
  **不要**用 `--host 0.0.0.0` 启动后端或 Vite——那会让局域网内任何人可以
  提交生成并消耗你的 TokenHub 额度。
- 密钥只放在 `seedance-workbench/.env` 的 `TOKENHUB_API_KEY=`（已被 gitignore）；
  不会出现在任何接口响应、数据库或日志中；视频下载请求不携带该密钥。
- 上游错误响应只提取已知错误结构（`error.message`），原始响应体不回显。
- 手工填写的视频地址只接受公网 HTTPS，拒绝内网/环回/保留地址与任何跳转；本地上传由受限临时素材服务转换为 HTTPS。
- 上传图片按魔数（JPEG/PNG/WebP 文件头）校验真实格式，分块读取防止内存耗尽。

## API 概览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/seedance/health` | 模式 / 模型 / 价格信息 |
| POST | `/api/seedance/generations` | 创建任务（multipart：参数 + 多图 `images[]`/`image_roles` + `video_urls[]`/`audio_urls[]` 重复字段） |
| POST | `/api/seedance/video-uploads` | 上传本地 MP4/MOV，返回不透明素材 ID |
| DELETE | `/api/seedance/video-uploads/{id}` | 删除尚未被任务引用的本地视频 |
| GET | `/api/seedance/tasks/{id}` | 查询任务（只读） |
| GET | `/api/seedance/tasks/by-crid/{crid}` | 按幂等键精确找回任务（刷新恢复用） |
| GET | `/api/seedance/tasks?page=&page_size=` | 历史列表（分页） |
| GET | `/api/seedance/tasks/{id}/refresh` | 只读刷新一次上游状态 |
| GET | `/api/seedance/tasks/{id}/video` | 视频流（代理下载，不带 API Key，链接过期自动刷新一次） |
| GET | `/api/seedance/tasks/{id}/last-frame` | 尾帧图代理（开启 return_last_frame 且成功后可用） |
| DELETE | `/api/seedance/tasks/{id}` | 删除记录与上传图片 |

响应统一信封：`{"ok": true, "data": ...}`；错误 `{"ok": false, "error": {"code", "message"}}`（中文）。

## v2 多模态能力（2026-11-12）

对应官方 doubao-seedance-2-0-260128 新增能力（docs.ai-tokenhub.com）：

| 模式 | 输入素材 | 说明 |
|------|---------|------|
| text 文生视频 | 文本 + 可选联网搜索 | 纯文本；`tools: [{type: web_search}]` 仅此模式生效 |
| image 图生视频 | 首帧（可选尾帧）+ 参考图 ≤9 张 | Base64 上传；`role`: first_frame/last_frame/reference_image |
| video 视频参考 | 参考视频 ≤3 个 + 参考图 ≤9 张 + 参考音频 ≤3 个 | 可选择本地 MP4/MOV，由后台转换为临时 HTTPS；也支持直接填写 HTTPS 或 `asset://`，单个 2~15s ≤50MB |
| edit 编辑视频 | 待编辑视频 + 可选参考图 + 编辑指令 | 替换主体/对象增删改/局部重绘，运镜不变 |

- **提示词素材引用规则**（官方）：必须用「素材类型+序号」引用，如"全程使用视频1的运镜，人物换成图片1"。前端在提示词上方提供引用芯片，点击即在光标处插入正确编号。
- **尾帧图**：开关 `return_last_frame` 开启后，任务成功可通过 `/tasks/{id}/last-frame` 查看/下载尾帧；`has_last_frame` 字段指示可用性。
- 素材摘要（`materials`）只含 kind/role/序号/来源，图片与音频原始字节**绝不**进入接口响应或数据库。

## 测试

```powershell
# 后端（100 个用例：校验/客户端/存储/Mock 全流程/真实模式错误路径/防重复计费/多模态 v2）
cd D:\codex\CareGuide\seedance-workbench
..\.venv\Scripts\python.exe -m pytest backend/tests -q

# 前端（16 个用例：幂等键/表单/多模态编号/历史/状态/费用工具函数）
cd frontend
node --test tests/*.test.js
```

全部测试只使用模拟响应，不消耗真实额度。

## Mock 样例视频

`data/assets/mock-sample.mp4` 由 ffmpeg 生成并已提交。若缺失可重新生成：

```powershell
ffmpeg -y -f lavfi -i "testsrc2=duration=6:size=640x360:rate=24" -f lavfi -i "sine=frequency=440:duration=6" -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -movflags +faststart data/assets/mock-sample.mp4
```
