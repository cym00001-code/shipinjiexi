# 小红书/视频号解析平台

> 粘贴小红书分享链接 → 自动解析 → 多清晰度原画下载（含图集打包）。
> 视频号支持粘贴复制链接、详情 JSON、下载命令、媒体链接 + decodeKey → 自动归一化并解密下载。
> 纯 Python + 单页前端，无需浏览器无需 JS 引擎，1.8GB 小机器轻松跑。

## 在线体验

- 🌐 http://8.138.150.200:39083 （部署在阿里云）

## 功能

- 支持 `xiaohongshu.com/explore/...`、`xhslink.com/...` 短链、App 分享文本
- **视频笔记**：完整暴露 `h264 / h265 / av1` × 所有码率分辨率，自由选清晰度
- **规格增强**：展示格式、音频编码、音频码率、文件大小等更多流信息
- **图文笔记**：原图直链 + 一键打包 ZIP；自动识别"实况图"动图，导出 MP4
- 后端代理下载，自动加 Referer 解决 CDN 防盗链 + 自定义文件名
- **视频号**：支持复制链接识别作品入口；若分享页或采集数据含 `objectDesc.media.url / urlToken / decodeKey`，则用 ISAAC64 解密前 128KB 后下载
- SQLite 解析历史，回顾过往作品
- 仿小红书风格 UI，移动端友好

## 技术栈

- **后端**：FastAPI + httpx + SQLite
- **前端**：单页 HTML + Tailwind CDN + Vue 3 CDN
- **解析原理**：抓取小红书网页中的 `window.__INITIAL_STATE__` JSON，从 `noteDetailMap[noteId].note.video.media.stream.{h264,h265,av1}` 提取多清晰度流
- **视频号原理**：微信视频号直链和 `decodeKey` 通常来自微信 PC 端/本地采集环境；本站负责归一化字段并解密下载，不在服务器侧伪造微信登录环境

## 本地开发

```bash
# 1. 进入后端目录
cd backend

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # Linux/Mac

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

打开 http://127.0.0.1:8000 即可使用。

或者直接双击 `start.bat` (Windows) / 执行 `bash start.sh` (Linux)。

## 配置

环境变量（可选）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `REQUEST_TIMEOUT` | `20` | httpx 请求超时（秒）|
| `XHS_COOKIE` | 空 | 小红书 Cookie，匿名解析够用，配 cookie 可解锁更高码率 |

也可在前端「设置」面板里临时填入 Cookie，存在浏览器 localStorage。

## 视频号用法

视频号不像小红书公开页那样稳定暴露下载字段。第一版请粘贴以下任一内容：

- 微信 PC 端/采集工具拿到的详情 JSON，包含 `objectDesc.media[].url`、`urlToken`、`decodeKey`
- 视频号「复制链接」得到的分享链接，系统会抓取分享页并尽力提取内嵌媒体字段
- 下载命令或文本，例如包含 `https://wxapp.tc.qq.com/...` 和 `decodeKey=123456789`
- 已拼好鉴权参数的媒体链接；如果有 `decodeKey`，下载时会自动解密

如果普通分享页没有下发 `urlToken / decodeKey`，页面会显示“链接已识别但缺少下载密钥”。这是平台机制限制，完整下载字段通常来自微信 PC 登录环境中的 `FinderGetCommentDetail`。

## API

- `POST /api/parse` `{url, cookie?, save_history?}` — 解析作品
- `GET  /api/download?url=...&filename=...` — 流式代理下载单文件
- `GET  /api/wx-download?url=...&decode_key=...&filename=...` — 视频号加密视频代理解密下载
- `POST /api/zip` `{urls, filename?}` — 图集打包 ZIP
- `GET  /api/history` — 解析历史
- `GET  /api/history/{id}` — 取出历史完整数据
- `DELETE /api/history/{id}` — 删一条
- `DELETE /api/history` — 清空
- `GET  /api/health` — 健康检查

## 服务器部署（宝塔 / systemd）

参考 [DEPLOY.md](./DEPLOY.md)。简短版：

```bash
# 上传到 /www/wwwroot/xhs-parser/
ssh server 'cd /www/wwwroot/xhs-parser/backend && python3.8 -m venv .venv && .venv/bin/pip install -r requirements.txt'
# 启动 systemd 服务 (39083 端口) + Nginx 反代
systemctl start xhs-parser
```

## 致谢

- 解析方案参考 [XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) (GPL-3.0)

## 免责声明

仅供个人学习与备份使用，请勿用于商业用途。视频/图片版权归原作者所有，请尊重创作者。
