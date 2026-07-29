# NN Chat

Neighborly Network Chat — 一个 Flask + SocketIO 多人即时通讯聊天室。

## ✨ 功能

- ✅ 文字消息实时发送（WebSocket 广播）
- ✅ 图片/文件上传
- ✅ 好友私聊、群组聊天
- ✅ 表情回应
- ✅ 消息撤回 / 编辑
- ✅ 未读红点提示
- ✅ @ 提及
- ✅ 输入状态指示
- ✅ 最近联系人排序
- ✅ 管理员后台封号 / 解封
- ✅ 玻璃拟态暗色风格 UI

## 🧩 技术栈

| 层 | 选型 | 备注 |
|---|------|------|
| 后端框架 | Flask | Python 3.13，单文件 1800+ 行启动器 |
| 实时通信 | Flask-SocketIO + eventlet | WebSocket 广播新消息 |
| WSGI | Waitress | eventlet monkey_patch 驱动 |
| 数据库 | SQLite + WAL 模式 | 单文件 chat.db |
| 前端 | jQuery 3.6 + Font Awesome | 玻璃拟态暗色 CSS 全部手写 |
| 认证 | SHA256 + 随机盐 | Flask session 维护登录态 |
| 加密 | ❌ 已关停 | 原 E2E XOR 实现有 bug，2026-07-28 彻底关停，改纯明文 |

## 🚀 快速启动

```bash
# 克隆仓库
git clone https://github.com/ciallo0721-cmd/NN-Chat.git
cd NN-Chat

# 安装依赖
pip install flask flask-socketio eventlet waitress

# 启动（会自动初始化数据库）
python 启动器.py
```

访问 `http://localhost:5000` 即可。

> 如果端口被 IIS 占用，先停掉：`net stop W3SVC /yes`

## 📁 项目结构

```
NN-Chat/
├── 启动器.py        # 主入口，Flask 应用 + SocketIO 事件
├── templates/
│   └── index.html   # 前端页面（单页应用）
├── aud/             # 上传的音频文件
├── picture/         # 上传的图片文件
├── sf/              # 上传的文件
├── deliverables/    # 附属交付物
├── payload.js       # ⚠️ WebShell 入口（HTML 内容，.js 后缀仅为绕过检测）
├── chat.db          # SQLite 数据库（自动生成）
├── chat.log         # 运行日志（启动记录、管理操作日志）
├── blank.png        # 占位/空白图片
├── .gitignore
└── README.md
```

## 🐛 踩坑记录

详见博客文章：[NN 聊天室 —— 一个 Flask 项目的血泪修复史](https://ciallo0721-cmd.top/blog/闲聊/72/)

修复了点：
- `@app.after_request` 强制 text/html 导致 ajax 解析失败
- 图片/文件路径字段复用导致显示异常
- `request.get_json()` 在 Content-Type 错误时抛 415
- E2E 加密 key 同步顺序 bug → 最终彻底关掉加密
- Session 在 LAN IP 下被浏览器拦截 → fallback cookie 兜底

## 📝 许可

MIT
