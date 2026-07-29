# ── 必须最先：eventlet 的 monkey_patch 要在任何其他模块导入之前完成 ──
# 原因：Werkzeug 的 LocalStack、logging 的 RLock 等对象如果在 monkey_patch
# 之前被创建，monkey_patch 遍历它们时会触发 RuntimeError，且留下未 green 的锁。
# 最终导致 eventlet 协程模型下只能串行处理一个连接。
import sys
import subprocess

try:
    import eventlet
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "eventlet"])
    import eventlet

eventlet.monkey_patch()

# ── 给 eventlet.listen 打补丁：绑定前设 SO_REUSEADDR，防止 Windows TIME_WAIT 端口复用失败 ──
_original_eventlet_listen = eventlet.listen
def _patched_eventlet_listen(addr, backlog=128):
    sock = eventlet.green.socket.socket(
        eventlet.green.socket.AF_INET, eventlet.green.socket.SOCK_STREAM)
    sock.setsockopt(eventlet.green.socket.SOL_SOCKET,
                    eventlet.green.socket.SO_REUSEADDR, 1)
    sock.bind(addr)
    sock.listen(backlog)
    return sock
eventlet.listen = _patched_eventlet_listen

# ── monkey_patch 之后再导入所有标准库与第三方库 ──
import os
import random
import threading
import time
import uuid
import socket
import base64
import sqlite3
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from functools import wraps

# ── 现在再导入 Web 框架（werkzeug/flask 会在已 green 的环境中创建对象）──
try:
    from flask import Flask, request, render_template, session, jsonify, send_from_directory
except ImportError:
    print("Flask未安装，正在自动安装...")
    os.system("pip install flask")
    from flask import Flask, request, render_template, session, jsonify, send_from_directory

try:
    from waitress import serve as waitress_serve
except ImportError:
    print("Waitress未安装，正在自动安装...")
    os.system("pip install waitress")
    from waitress import serve as waitress_serve

try:
    from flask_socketio import SocketIO, emit, join_room, leave_room
except ImportError:
    print("Flask-SocketIO未安装，正在自动安装...")
    os.system("pip install flask-socketio")
    from flask_socketio import SocketIO, emit, join_room, leave_room

# ────────────────── 结构化日志 ──────────────────
logger = logging.getLogger('nn_chat')
logger.setLevel(logging.INFO)

# 控制台处理器（INFO级别）
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
console_handler.setFormatter(console_fmt)

# 文件处理器（WARNING+级别，5MB轮转，保留3个备份）
file_handler = RotatingFileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chat.log'),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding='utf-8'
)
file_handler.setLevel(logging.WARNING)
file_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(file_fmt)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

app = Flask(__name__)
app.secret_key = 'audLLOaudllo0721'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB（文件上传最大限制）

socketio = SocketIO(app, cors_allowed_origins="*")

# ────────────────── 项目目录（保证数据库和静态文件路径固定） ──────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ────────────────── 数据库初始化 ──────────────────
DB_PATH = os.path.join(BASE_DIR, 'chat.db')

def get_db():
    """获取数据库连接（WAL模式，多线程安全）"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    """创建所有表 + 数据库迁移"""
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS friends (
            user_id TEXT NOT NULL,
            friend_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (user_id, friend_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            sender_id TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            message TEXT NOT NULL,
            image_path TEXT,
            timestamp REAL NOT NULL,
            viewed_by TEXT DEFAULT '[]',
            recalled INTEGER DEFAULT 0,
            edited INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS groups_t (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            creator_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY (group_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, timestamp);
    ''')
    conn.commit()

    # ────────── 数据库迁移 ──────────
    migrations = [
        # messages表新增列
        ("ALTER TABLE messages ADD COLUMN file_path TEXT", "file_path"),
        ("ALTER TABLE messages ADD COLUMN file_name TEXT", "file_name"),
        ("ALTER TABLE messages ADD COLUMN file_size INTEGER", "file_size"),
        ("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'", "mentions"),
        # users表新增列
        ("ALTER TABLE users ADD COLUMN avatar_path TEXT DEFAULT ''", "avatar_path"),
        ("ALTER TABLE users ADD COLUMN nickname TEXT DEFAULT ''", "nickname"),
        ("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''", "bio"),
        ("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'", "role"),
    ]
    for sql, col_name in migrations:
        try:
            conn.execute(sql)
            conn.commit()
            logger.info(f'[迁移] 新增列 {col_name} 成功')
        except sqlite3.OperationalError:
            pass  # 列已存在

    # 新增reactions表
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS reactions (
                msg_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                emoji TEXT NOT NULL,
                PRIMARY KEY (msg_id, user_id)
            )
        ''')
        conn.commit()
    except Exception:
        pass

    conn.close()

# ────────────────── 密码工具 ──────────────────
def hash_password(password, salt=None):
    """SHA256 + 随机盐哈希密码，返回 (hash_hex, salt_hex)"""
    if salt is None:
        salt = os.urandom(16).hex()
    h = hashlib.sha256((password + salt).encode('utf-8')).hexdigest()
    return h, salt

def verify_password(password, salt, stored_hash):
    """验证密码"""
    h, _ = hash_password(password, salt)
    return h == stored_hash

# ────────────────── 管理员中间件 ──────────────────
def admin_required(f):
    """管理员权限装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify(status='error', message='请先登录'), 401
        uid = session['user_id']
        conn = get_db()
        row = conn.execute('SELECT role FROM users WHERE id=?', (uid,)).fetchone()
        conn.close()
        if not row or row['role'] != 'admin':
            return jsonify(status='error', message='需要管理员权限'), 403
        return f(*args, **kwargs)
    return decorated

# ────────────────── 全局内存数据 ──────────────────
messages = {}       # {msg_id: msg_obj}  -- 消息缓存
lock = threading.Lock()

user_registry = {}  # {user_id: {"name": str, "last_seen": float}}
friends = {}        # {user_id: set(friend_user_id)}
typing_status = {}  # {chat_id: {user_id: timestamp}}
rate_limit_data = {}  # {ip: [timestamp, ...]}

# ────────────────── 启动时从DB恢复数据 ──────────────────
def load_from_db():
    """启动时从数据库恢复内存数据"""
    try:
        conn = get_db()
        # 恢复用户
        for row in conn.execute('SELECT * FROM users').fetchall():
            user_registry[row['id']] = {
                'name': row['username'],
                'last_seen': row['last_seen']
            }
        # 恢复好友关系
        for row in conn.execute('SELECT * FROM friends').fetchall():
            uid = row['user_id']
            fid = row['friend_id']
            if uid not in friends:
                friends[uid] = set()
            friends[uid].add(fid)
        # 恢复最近500条消息
        cols = [d[1] for d in conn.execute('PRAGMA table_info(messages)').fetchall()]
        for row in conn.execute(
            'SELECT * FROM messages ORDER BY timestamp DESC LIMIT 500'
        ).fetchall():
            msg = dict(row)
            # base64解码消息（E2E已关闭，旧数据需要解码，新消息直接明文）
            try:
                msg['message'] = base64.b64decode(msg['message']).decode('utf-8')
            except Exception:
                pass  # 已经是明文，不用处理
            msg['viewed_by'] = json.loads(msg['viewed_by'])
            # 解码mentions
            if 'mentions' in cols and msg.get('mentions'):
                try:
                    msg['mentions'] = json.loads(msg['mentions'])
                except Exception:
                    msg['mentions'] = []
            else:
                msg['mentions'] = []
            messages[msg['id']] = msg
        conn.close()
        logger.info(f'[数据库] 已加载 {len(user_registry)} 个用户, {len(messages)} 条消息')
    except Exception as e:
        logger.warning(f'[数据库] 加载失败（首次运行正常）: {e}')

# ────────────────── 速率限制 ──────────────────
@app.before_request
def rate_limit():
    """IP限流：每IP每分钟最多60个请求"""
    # 静态文件不限制
    if request.path.startswith('/sf/') or request.path.startswith('/aud/'):
        return None
    # 文件下载不限制
    if request.path.startswith('/download_file/'):
        return None
    ip = request.remote_addr or '127.0.0.1'
    now = time.time()
    with lock:
        if ip not in rate_limit_data:
            rate_limit_data[ip] = []
        # 清理超过60秒的记录
        rate_limit_data[ip] = [t for t in rate_limit_data[ip] if now - t < 60]
        if len(rate_limit_data[ip]) >= 60:
            # 判断是否API请求还是页面请求
            if request.path == '/' or request.path.startswith('/static'):
                pass  # 页面请求不拦截
            else:
                return jsonify(status='error', message='请求过于频繁，请稍后再试'), 429
        rate_limit_data[ip].append(now)

# ── 修正响应 Content-Type：仅对原本缺失/错误的页面响应兜底，绝不污染 JSON ──
@app.after_request
def fix_content_type(response):
    """仅对 HTML 路由（页面响应）兜底设置 text/html；JSON/二进制文件保持原样。"""
    # 已是正确类型（包括 application/json、image/*、text/css、application/octet-stream 等）则不动
    ct = (response.headers.get('Content-Type') or '').lower()
    if ct and (ct.startswith('application/json')
               or ct.startswith('image/')
               or ct.startswith('video/')
               or ct.startswith('audio/')
               or ct.startswith('text/css')
               or ct.startswith('text/javascript')
               or ct.startswith('application/javascript')
               or ct.startswith('application/octet-stream')
               or ct.startswith('application/pdf')
               or ct.startswith('font/')):
        return response
    # 仅当没有 Content-Type 时，才补成 text/html
    if not ct:
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

# ══════════════ WebSocket 事件处理 ══════════════

@socketio.on('connect')
def handle_connect():
    """客户端连接：加入用户房间"""
    user_id = session.get('user_id')
    if user_id:
        join_room(user_id)
        logger.info(f'[WebSocket] 用户 {user_id} 连接')

@socketio.on('disconnect')
def handle_disconnect():
    """客户端断开：离开用户房间"""
    user_id = session.get('user_id')
    if user_id:
        leave_room(user_id)
        logger.info(f'[WebSocket] 用户 {user_id} 断开')

@socketio.on('join_chat')
def handle_join_chat(data):
    """客户端加入聊天房间"""
    user_id = session.get('user_id')
    chat_id = data.get('chat_id', '')
    if user_id and chat_id:
        join_room(chat_id)
        logger.info(f'[WebSocket] 用户 {user_id} 加入房间 {chat_id}')

@socketio.on('leave_chat')
def handle_leave_chat(data):
    """客户端离开聊天房间"""
    user_id = session.get('user_id')
    chat_id = data.get('chat_id', '')
    if user_id and chat_id:
        leave_room(chat_id)
        logger.info(f'[WebSocket] 用户 {user_id} 离开房间 {chat_id}')

@socketio.on('send_message')
def handle_ws_send_message(data):
    """WebSocket发送消息"""
    user_id = session.get('user_id')
    username = session.get('username')
    if not user_id:
        emit('error', {'message': '请先登录'})
        return

    message = data.get('message', '').strip()
    file_path = data.get('file_path', '')
    image_path = data.get('image_path', '')  # 兼容旧客户端
    chat_id = data.get('chat_id', '')
    file_name = data.get('file_name', '')
    file_size = data.get('file_size', 0)
    mentions = data.get('mentions', [])

    # file_path优先，兼容image_path
    if not file_path and image_path:
        file_path = image_path

    if not message and not file_path:
        emit('error', {'message': '消息不能为空'})
        return

    msg_id = f'{time.time()}-{random.randint(1000, 9999)}'
    now = time.time()

    # base64编码消息再存储（不存明文） ※ 2026-07-28: E2E/base64 已关闭，直接存明文
    encoded_message = message

    msg_obj = {
        'id': msg_id,
        'sender': username,
        'sender_id': user_id,
        'sender_name': username,
        'message': message,
        'timestamp': now,
        'viewed_by': [],
        'chat_id': chat_id,
        'image_path': image_path or None,  # 修复：原来是 file_path or None
        'file_path': file_path or None,
        'file_name': file_name,
        'file_size': file_size,
        'recalled': 0,
        'edited': 0,
        'mentions': mentions,
    }

    with lock:
        messages[msg_id] = msg_obj

        # 写入数据库（存base64编码）
        conn = get_db()
        conn.execute(
            'INSERT INTO messages (id, sender_id, sender_name, chat_id, message, '
            'image_path, file_path, file_name, file_size, timestamp, viewed_by, recalled, edited, mentions) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (msg_id, user_id, username, chat_id, encoded_message,
             file_path or None, file_path or None, file_name, file_size,
             now, '[]', 0, 0, json.dumps(mentions))
        )
        conn.commit()
        conn.close()

    # 广播给房间内所有人
    socketio.emit('new_message', msg_obj, room=chat_id)
    # 如果@了人，也给被@的人单独推送通知
    if mentions:
        for mentioned_uid in mentions:
            socketio.emit('mention_notify', {
                'message_id': msg_id,
                'sender_name': username,
                'chat_id': chat_id,
                'message': message[:50],
            }, room=mentioned_uid)


@socketio.on('typing')
def handle_ws_typing(data):
    """WebSocket输入状态"""
    user_id = session.get('user_id')
    if not user_id:
        return
    chat_id = data.get('chat_id', '')
    if not chat_id:
        return

    with lock:
        if chat_id not in typing_status:
            typing_status[chat_id] = {}
        typing_status[chat_id][user_id] = time.time()

    # 广播给房间内其他人
    socketio.emit('typing', {
        'user_id': user_id,
        'user_name': session.get('username', ''),
        'chat_id': chat_id,
    }, room=chat_id, include_self=False)


# ══════════════ WebRTC 信令（通过Socket.IO） ══════════════

@socketio.on('call_offer')
def handle_call_offer(data):
    """转发WebRTC offer"""
    to_user_id = data.get('to_user_id', '')
    if to_user_id:
        socketio.emit('call_offer', data, room=to_user_id)

@socketio.on('call_answer')
def handle_call_answer(data):
    """转发WebRTC answer"""
    to_user_id = data.get('to_user_id', '')
    if to_user_id:
        socketio.emit('call_answer', data, room=to_user_id)

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    """转发ICE candidate"""
    to_user_id = data.get('to_user_id', '')
    if to_user_id:
        socketio.emit('ice_candidate', data, room=to_user_id)

@socketio.on('call_reject')
def handle_call_reject(data):
    """转发通话拒绝"""
    to_user_id = data.get('to_user_id', '')
    if to_user_id:
        socketio.emit('call_reject', data, room=to_user_id)

@socketio.on('call_end')
def handle_call_end(data):
    """转发通话结束"""
    to_user_id = data.get('to_user_id', '')
    if to_user_id:
        socketio.emit('call_end', data, room=to_user_id)


# ────────────────── 页面路由 ──────────────────

@app.route('/')
def index():
    """首页：已登录显示聊天，未登录显示登录页"""
    user_id = session.get('user_id')
    username = session.get('username')
    logged_in = False

    # 兜底：session 被拦截时从 fallback cookie 恢复
    if not user_id:
        uid_cookie = request.cookies.get('nn_uid')
        if uid_cookie and uid_cookie in user_registry:
            user_id = uid_cookie
            username = user_registry[uid_cookie].get('name')
            # 写回 session 修复后续请求
            session['user_id'] = user_id
            session['username'] = username

    if user_id:
        # session 优先；user_registry 找不到时从 DB 兜底（防止 reload 后内存丢失）
        if user_id not in user_registry:
            try:
                conn = get_db()
                row = conn.execute('SELECT username, last_seen FROM users WHERE id=?', (user_id,)).fetchone()
                conn.close()
                if row:
                    with lock:
                        user_registry[user_id] = {'name': row['username'], 'last_seen': row['last_seen']}
                    if username is None:
                        username = row['username']
                else:
                    # session 里有 user_id 但 DB 里没有 → 视为失效，清掉
                    session.pop('user_id', None)
                    session.pop('username', None)
                    user_id = None
            except Exception as e:
                logger.warning(f'[/] 回填 user_registry 失败: {e}')

        if user_id and user_id in user_registry:
            logged_in = True
            # 更新last_seen
            try:
                with lock:
                    if user_id in user_registry:
                        user_registry[user_id]['last_seen'] = time.time()
                conn = get_db()
                conn.execute('UPDATE users SET last_seen=? WHERE id=?',
                             (time.time(), user_id))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.warning(f'[/] 更新last_seen失败: {e}')

    return render_template('index.html',
                           logged_in=logged_in,
                           username=username or '',
                           user_id=user_id or '')

# ══════════════ 用户认证 ══════════════

@app.route('/register', methods=['POST'])
def register():
    """注册新用户"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify(status='error', message='无效请求'), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')

    # 验证
    if len(username) < 2 or len(username) > 16:
        return jsonify(status='error', message='用户名需要2-16位'), 400
    if len(password) < 4:
        return jsonify(status='error', message='密码至少4位'), 400

    with lock:
        conn = get_db()
        # 检查用户名是否已存在
        existing = conn.execute(
            'SELECT id FROM users WHERE username=?', (username,)
        ).fetchone()
        if existing:
            conn.close()
            return jsonify(status='error', message='用户名已存在'), 409

        # 创建用户
        user_id = str(uuid.uuid4())
        pwd_hash, salt = hash_password(password)
        now = time.time()

        # 第一个注册的用户自动设为admin
        user_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        role = 'admin' if user_count == 0 else 'user'

        conn.execute(
            'INSERT INTO users (id, username, password_hash, salt, created_at, last_seen, role) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (user_id, username, pwd_hash, salt, now, now, role)
        )
        conn.commit()
        conn.close()

        # 写入内存
        user_registry[user_id] = {'name': username, 'last_seen': now}
        if user_id not in friends:
            friends[user_id] = set()

    # 自动登录
    session['user_id'] = user_id
    session['username'] = username

    # 额外写普通 cookie 兜底（防止 SameSite 拦截导致 session 写不进去）
    resp = jsonify(status='success', user_id=user_id, username=username)
    resp.set_cookie('nn_uid', user_id, max_age=86400, httponly=True, samesite='Lax')
    resp.set_cookie('nn_uname', username, max_age=86400, httponly=True, samesite='Lax')
    return resp


@app.route('/login', methods=['POST'])
def login():
    """用户登录"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify(status='error', message='无效请求'), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify(status='error', message='用户名和密码不能为空'), 400

    with lock:
        conn = get_db()
        row = conn.execute(
            'SELECT * FROM users WHERE username=?', (username,)
        ).fetchone()
        conn.close()

        if not row:
            return jsonify(status='error', message='用户名不存在'), 404

        if not verify_password(password, row['salt'], row['password_hash']):
            return jsonify(status='error', message='密码错误'), 401

        # 检查是否被封禁
        if row['role'] == 'banned':
            return jsonify(status='error', message='您的账号已被封禁'), 403

        user_id = row['id']
        now = time.time()

        # 更新last_seen
        conn2 = get_db()
        conn2.execute('UPDATE users SET last_seen=? WHERE id=?', (now, user_id))
        conn2.commit()
        conn2.close()

        user_registry[user_id] = {'name': username, 'last_seen': now}
        if user_id not in friends:
            friends[user_id] = set()

    session['user_id'] = user_id
    session['username'] = username

    return jsonify(status='success', user_id=user_id, username=username)


@app.route('/logout', methods=['POST'])
def logout():
    """退出登录（同时清理 fallback cookie，防止自动重登）"""
    session.clear()
    resp = jsonify(status='success')
    resp.set_cookie('nn_uid', '', max_age=0, httponly=True, samesite='Lax')
    resp.set_cookie('nn_uname', '', max_age=0, httponly=True, samesite='Lax')
    return resp

# ══════════════ 消息收发 ══════════════

@app.route('/send', methods=['POST'])
def send_message():
    """发送消息（HTTP，兼容旧客户端）"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify(status='error', message='无效的请求'), 400

    message = data.get('message', '').strip()
    image_path = data.get('image_path', '')
    file_path = data.get('file_path', '')  # 新的通用文件路径字段
    chat_id = data.get('chat_id', '')
    file_name = data.get('file_name', '')
    file_size = data.get('file_size', 0)
    mentions = data.get('mentions', [])

    # 兼容旧客户端：file_path优先，fallback到image_path
    if not file_path and image_path:
        file_path = image_path

    if not message and not file_path:
        return jsonify(status='error', message='消息不能为空'), 400

    msg_id = f'{time.time()}-{random.randint(1000, 9999)}'
    now = time.time()
    sender_id = session['user_id']
    sender_name = session['username']

    # base64编码消息再存储（不存明文） ※ 2026-07-28: E2E/base64 已关闭，直接存明文
    encoded_message = message

    msg_obj = {
        'id': msg_id,
        'sender': sender_name,
        'sender_id': sender_id,
        'sender_name': sender_name,
        'message': message,  # 内存中存明文（方便渲染）
        'timestamp': now,
        'viewed_by': [],
        'chat_id': chat_id,
        'image_path': image_path or None,  # 修复：原来是 file_path or None，会让文件被当图片
        'file_path': file_path or None,
        'file_name': file_name,
        'file_size': file_size,
        'recalled': 0,
        'edited': 0,
        'mentions': mentions,
    }

    with lock:
        messages[msg_id] = msg_obj

        # 写入数据库（存base64编码）
        conn = get_db()
        conn.execute(
            'INSERT INTO messages (id, sender_id, sender_name, chat_id, message, '
            'image_path, file_path, file_name, file_size, timestamp, viewed_by, recalled, edited, mentions) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (msg_id, sender_id, sender_name, chat_id, encoded_message,
             file_path or None, file_path or None, file_name, file_size,
             now, '[]', 0, 0, json.dumps(mentions))
        )
        conn.commit()
        conn.close()

    # 通过WebSocket广播给房间内所有人
    socketio.emit('new_message', msg_obj, room=chat_id)
    if mentions:
        for mentioned_uid in mentions:
            socketio.emit('mention_notify', {
                'message_id': msg_id,
                'sender_name': sender_name,
                'chat_id': chat_id,
                'message': message[:50],
            }, room=mentioned_uid)

    return jsonify(status='success', message_id=msg_id, message_data=msg_obj)


@app.route('/receive')
def receive_messages():
    """拉取消息（含已读回执 + 输入状态 + 分页）"""
    if 'user_id' not in session:
        return jsonify(messages=[], typing_users=[])

    user_id = session.get('user_id', '')
    chat_id = request.args.get('chat_id', '')
    last_id = request.args.get('last_id', '')  # 增量拉取
    before_id = request.args.get('before_id', '')  # 分页拉取

    result = []

    if before_id:
        # ────────── #8 消息分页 ──────────
        # 返回比before_id早的最近50条消息
        with lock:
            conn = get_db()
            rows = conn.execute(
                '''SELECT * FROM messages
                   WHERE chat_id=? AND id < ?
                   ORDER BY timestamp DESC LIMIT 50''',
                (chat_id, before_id)
            ).fetchall()
            conn.close()

            for row in rows:
                msg = dict(row)
                try:
                    msg['message'] = base64.b64decode(msg['message']).decode('utf-8')
                except Exception:
                    pass
                msg['viewed_by'] = json.loads(msg['viewed_by'])
                try:
                    msg['mentions'] = json.loads(msg.get('mentions', '[]'))
                except Exception:
                    msg['mentions'] = []
                result.append(msg)

        # 按时间升序返回
        result.sort(key=lambda x: x['timestamp'])

        # 输入状态
        typing_users = _get_typing_users(chat_id, user_id)
        return jsonify(messages=result, typing_users=typing_users)

    # 原有的增量拉取逻辑
    with lock:
        # 收集需要更新的消息ID
        to_update = []
        for msg_id, msg in list(messages.items()):
            if msg['chat_id'] != chat_id:
                continue
            # 增量拉取优化
            if last_id and msg_id <= last_id:
                continue
            # 已读回执
            if user_id not in msg['viewed_by'] and msg['sender_id'] != user_id:
                msg['viewed_by'].append(user_id)
                to_update.append(msg_id)
            result.append(msg)

        # 更新数据库中的viewed_by
        if to_update:
            conn = get_db()
            for mid in to_update:
                m = messages.get(mid)
                if m:
                    conn.execute(
                        'UPDATE messages SET viewed_by=? WHERE id=?',
                        (json.dumps(m['viewed_by']), mid)
                    )
            conn.commit()
            conn.close()

        # 输入状态
        typing_users = _get_typing_users(chat_id, user_id)

    # 按时间排序
    result.sort(key=lambda x: x['timestamp'])
    return jsonify(messages=result, typing_users=typing_users)


def _get_typing_users(chat_id, user_id):
    """获取某聊天室的输入状态列表"""
    typing_users = []
    if chat_id in typing_status:
        now = time.time()
        for uid, ts in list(typing_status[chat_id].items()):
            if now - ts < 3 and uid != user_id:
                name = user_registry.get(uid, {}).get('name', '未知')
                typing_users.append({'user_id': uid, 'name': name})
            elif now - ts >= 3:
                del typing_status[chat_id][uid]
    return typing_users


# ══════════════ 图片上传 ══════════════

@app.route('/upload_image', methods=['POST'])
def upload_image():
    """上传图片到 sf/ 目录"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    if 'image' not in request.files:
        return jsonify(status='error', message='没有图片文件'), 400

    f = request.files['image']
    if f.filename == '':
        return jsonify(status='error', message='未选择文件'), 400

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'png'
    if ext not in ('png', 'jpg', 'jpeg', 'gif'):
        return jsonify(status='error', message='只支持 PNG/JPG/GIF'), 400

    try:
        raw = f.read()
        if len(raw) > 2 * 1024 * 1024:
            return jsonify(status='error', message='图片超过 2MB'), 400

        # 保存到 sf/ 目录
        filename = f'img_{uuid.uuid4().hex[:12]}.{ext}'
        sf_dir = os.path.join(BASE_DIR, 'sf')
        os.makedirs(sf_dir, exist_ok=True)
        with open(os.path.join(sf_dir, filename), 'wb') as fw:
            fw.write(raw)

        return jsonify(status='success', image_path=filename)
    except Exception as e:
        return jsonify(status='error', message=str(e)), 500


# ══════════════ 文件分享（#9） ══════════════

ALLOWED_FILE_EXTENSIONS = {'pdf', 'txt', 'zip', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

@app.route('/upload_file', methods=['POST'])
def upload_file():
    """上传文件到 sf/ 目录"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    if 'file' not in request.files:
        return jsonify(status='error', message='没有文件'), 400

    f = request.files['file']
    if f.filename == '':
        return jsonify(status='error', message='未选择文件'), 400

    original_name = f.filename
    ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
    if ext not in ALLOWED_FILE_EXTENSIONS:
        return jsonify(
            status='error',
            message=f'不支持的文件格式，仅支持: {", ".join(ALLOWED_FILE_EXTENSIONS)}'
        ), 400

    try:
        raw = f.read()
        if len(raw) > MAX_FILE_SIZE:
            return jsonify(status='error', message='文件超过 10MB'), 400

        # 保存到 sf/ 目录
        safe_name = f'file_{uuid.uuid4().hex[:12]}.{ext}'
        sf_dir = os.path.join(BASE_DIR, 'sf')
        os.makedirs(sf_dir, exist_ok=True)
        with open(os.path.join(sf_dir, safe_name), 'wb') as fw:
            fw.write(raw)

        return jsonify(
            status='success',
            file_path=safe_name,
            file_name=original_name,
            file_size=len(raw)
        )
    except Exception as e:
        return jsonify(status='error', message=str(e)), 500


@app.route('/download_file/<path:filename>')
def download_file(filename):
    """下载文件"""
    # 安全检查：只允许下载sf/目录下的文件
    sf_dir = os.path.join(BASE_DIR, 'sf')
    file_path = os.path.join(sf_dir, os.path.basename(filename))
    if not os.path.exists(file_path):
        return jsonify(status='error', message='文件不存在'), 404
    return send_from_directory(sf_dir, os.path.basename(filename), as_attachment=True)


# ══════════════ 好友系统 ══════════════

@app.route('/search_users')
def search_users():
    """搜索用户"""
    if 'user_id' not in session:
        return jsonify(users=[])

    q = request.args.get('q', '').strip().lower()
    uid = session.get('user_id', '')
    if not q:
        return jsonify(users=[])

    results = []
    with lock:
        for u, info in user_registry.items():
            if u == uid:
                continue
            if q in u.lower() or q in info['name'].lower():
                results.append({'id': u, 'name': info['name']})
    return jsonify(users=results[:20])


@app.route('/add_friend', methods=['POST'])
def add_friend():
    """添加好友"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    target_id = data.get('user_id', '')
    uid = session.get('user_id', '')

    if not target_id or target_id == uid:
        return jsonify(status='error', message='无效的用户'), 400

    with lock:
        if target_id not in user_registry:
            return jsonify(status='error', message='用户不存在'), 404
        if uid not in friends:
            friends[uid] = set()
        if target_id not in friends:
            friends[target_id] = set()

        if target_id in friends[uid]:
            return jsonify(status='error', message='已经是好友'), 400

        friends[uid].add(target_id)
        friends[target_id].add(uid)

        # 写入数据库
        now = time.time()
        conn = get_db()
        conn.execute(
            'INSERT OR IGNORE INTO friends (user_id, friend_id, created_at) VALUES (?, ?, ?)',
            (uid, target_id, now)
        )
        conn.execute(
            'INSERT OR IGNORE INTO friends (user_id, friend_id, created_at) VALUES (?, ?, ?)',
            (target_id, uid, now)
        )
        conn.commit()
        conn.close()

    return jsonify(
        status='success',
        friend_id=target_id,
        friend_name=user_registry[target_id]['name']
    )


@app.route('/remove_friend', methods=['POST'])
def remove_friend():
    """删除好友（#5）"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    friend_id = data.get('friend_id', '')
    uid = session['user_id']

    if not friend_id:
        return jsonify(status='error', message='缺少friend_id'), 400

    with lock:
        # 从内存中移除
        if uid in friends and friend_id in friends[uid]:
            friends[uid].discard(friend_id)
        if friend_id in friends and uid in friends[friend_id]:
            friends[friend_id].discard(uid)

        # 从数据库删除（双向）
        conn = get_db()
        conn.execute('DELETE FROM friends WHERE user_id=? AND friend_id=?', (uid, friend_id))
        conn.execute('DELETE FROM friends WHERE user_id=? AND friend_id=?', (friend_id, uid))
        conn.commit()
        conn.close()

    logger.info(f'[好友] {uid} 删除了好友 {friend_id}')
    return jsonify(status='success')


@app.route('/friend_list')
def friend_list():
    """获取好友列表（含在线状态）"""
    if 'user_id' not in session:
        return jsonify(friends=[])

    uid = session.get('user_id', '')
    now = time.time()
    result = []
    with lock:
        for fid in friends.get(uid, set()):
            info = user_registry.get(fid, {})
            last_seen = info.get('last_seen', 0)
            online = (now - last_seen) < 30
            result.append({
                'id': fid,
                'name': info.get('name', '未知用户'),
                'online': online,
                'last_seen': last_seen,
            })
    return jsonify(friends=result)


# ══════════════ 在线状态 ══════════════

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    """心跳：前端每10秒请求一次，更新last_seen"""
    if 'user_id' not in session:
        return jsonify(status='error'), 401

    user_id = session['user_id']
    now = time.time()
    with lock:
        if user_id in user_registry:
            user_registry[user_id]['last_seen'] = now
        # 每30秒同步一次到DB（减少写操作）
        conn = get_db()
        conn.execute('UPDATE users SET last_seen=? WHERE id=?', (now, user_id))
        conn.commit()
        conn.close()

    return jsonify(status='success')


@app.route('/friend_status')
def friend_status():
    """获取好友在线状态（批量）"""
    if 'user_id' not in session:
        return jsonify(users={})

    uid = session.get('user_id', '')
    now = time.time()
    result = {}
    with lock:
        for fid in friends.get(uid, set()):
            info = user_registry.get(fid, {})
            last_seen = info.get('last_seen', 0)
            result[fid] = {
                'online': (now - last_seen) < 30,
                'last_seen': last_seen,
            }
    return jsonify(users=result)


# ══════════════ 群组聊天 ══════════════

@app.route('/create_group', methods=['POST'])
def create_group():
    """创建群组"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    name = data.get('name', '').strip()
    members = data.get('members', [])  # user_id 列表
    creator_id = session['user_id']

    if not name:
        return jsonify(status='error', message='群组名称不能为空'), 400
    if len(members) < 1:
        return jsonify(status='error', message='至少需要1个成员'), 400

    group_id = f'g{uuid.uuid4().hex[:10]}'
    now = time.time()

    with lock:
        conn = get_db()
        conn.execute(
            'INSERT INTO groups_t (id, name, creator_id, created_at) VALUES (?, ?, ?, ?)',
            (group_id, name, creator_id, now)
        )
        # 创建者也是成员
        all_members = list(set(members + [creator_id]))
        for mid in all_members:
            conn.execute(
                'INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)',
                (group_id, mid)
            )
        conn.commit()
        conn.close()

    return jsonify(status='success', group_id=group_id, name=name)


@app.route('/group_list')
def group_list():
    """获取用户的群组列表"""
    if 'user_id' not in session:
        return jsonify(groups=[])

    uid = session.get('user_id', '')
    with lock:
        conn = get_db()
        rows = conn.execute(
            '''SELECT g.* FROM groups_t g
               INNER JOIN group_members gm ON g.id = gm.group_id
               WHERE gm.user_id = ?
               ORDER BY g.created_at DESC''',
            (uid,)
        ).fetchall()
        conn.close()

    result = []
    for row in rows:
        result.append({
            'id': row['id'],
            'name': row['name'],
            'creator_id': row['creator_id'],
            'created_at': row['created_at'],
        })
    return jsonify(groups=result)


@app.route('/group_members')
def group_members():
    """获取群组成员"""
    if 'user_id' not in session:
        return jsonify(members=[])

    group_id = request.args.get('group_id', '')
    if not group_id:
        return jsonify(members=[])

    with lock:
        conn = get_db()
        rows = conn.execute(
            'SELECT user_id FROM group_members WHERE group_id=?',
            (group_id,)
        ).fetchall()
        conn.close()

    result = []
    for row in rows:
        info = user_registry.get(row['user_id'], {})
        result.append({
            'id': row['user_id'],
            'name': info.get('name', '未知'),
        })
    return jsonify(members=result)


@app.route('/leave_group', methods=['POST'])
def leave_group():
    """退出群组（#5）"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    group_id = data.get('group_id', '')
    uid = session['user_id']

    if not group_id:
        return jsonify(status='error', message='缺少group_id'), 400

    with lock:
        conn = get_db()
        # 删除该用户
        conn.execute('DELETE FROM group_members WHERE group_id=? AND user_id=?',
                     (group_id, uid))

        # 检查剩余人数
        remaining = conn.execute(
            'SELECT COUNT(*) FROM group_members WHERE group_id=?', (group_id,)
        ).fetchone()[0]

        if remaining == 0:
            # 最后一人退出，删除群组
            conn.execute('DELETE FROM group_members WHERE group_id=?', (group_id,))
            conn.execute('DELETE FROM groups_t WHERE id=?', (group_id,))
        conn.commit()
        conn.close()

    logger.info(f'[群组] {uid} 退出了群组 {group_id}')
    return jsonify(status='success')


# ══════════════ 输入状态 ══════════════

@app.route('/typing', methods=['POST'])
def set_typing():
    """记录输入状态（HTTP，兼容旧客户端）"""
    if 'user_id' not in session:
        return jsonify(status='error'), 401

    data = request.get_json(silent=True)
    chat_id = data.get('chat_id', '')
    user_id = session['user_id']

    if not chat_id:
        return jsonify(status='error', message='缺少chat_id'), 400

    with lock:
        if chat_id not in typing_status:
            typing_status[chat_id] = {}
        typing_status[chat_id][user_id] = time.time()

    return jsonify(status='success')

# ══════════════ 消息撤回/编辑 ══════════════

@app.route('/recall_message', methods=['POST'])
def recall_message():
    """撤回消息（仅发送方，2分钟内）"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    msg_id = data.get('message_id', '')
    user_id = session['user_id']

    with lock:
        msg = messages.get(msg_id)
        if not msg:
            return jsonify(status='error', message='消息不存在'), 404
        if msg['sender_id'] != user_id:
            return jsonify(status='error', message='只能撤回自己的消息'), 403
        if time.time() - msg['timestamp'] > 120:
            return jsonify(status='error', message='超过2分钟，无法撤回'), 400

        msg['recalled'] = 1
        msg['message'] = '[消息已撤回]'
        msg['image_path'] = None

        conn = get_db()
        conn.execute(
            'UPDATE messages SET recalled=1, message=?, image_path=NULL WHERE id=?',
            ('[消息已撤回]', msg_id)
        )
        conn.commit()
        conn.close()

    # 通过WebSocket广播撤回事件
    socketio.emit('message_recalled', {
        'message_id': msg_id,
        'chat_id': msg['chat_id'],
    }, room=msg['chat_id'])

    return jsonify(status='success')


@app.route('/edit_message', methods=['POST'])
def edit_message():
    """编辑消息（仅发送方，2分钟内）"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    msg_id = data.get('message_id', '')
    new_text = data.get('message', '').strip()
    user_id = session['user_id']

    if not new_text:
        return jsonify(status='error', message='消息不能为空'), 400

    with lock:
        msg = messages.get(msg_id)
        if not msg:
            return jsonify(status='error', message='消息不存在'), 404
        if msg['sender_id'] != user_id:
            return jsonify(status='error', message='只能编辑自己的消息'), 403
        if time.time() - msg['timestamp'] > 120:
            return jsonify(status='error', message='超过2分钟，无法编辑'), 400

        msg['message'] = new_text
        msg['edited'] = 1

        conn = get_db()
        conn.execute(
            'UPDATE messages SET message=?, edited=1 WHERE id=?',
            (new_text, msg_id)
        )
        conn.commit()
        conn.close()

    # 通过WebSocket广播编辑事件
    socketio.emit('message_edited', {
        'message_id': msg_id,
        'chat_id': msg['chat_id'],
        'message': new_text,
    }, room=msg['chat_id'])

    return jsonify(status='success', message_data=msg)


# ══════════════ 表情回应（#11） ══════════════

@app.route('/react', methods=['POST'])
def add_reaction():
    """添加/切换表情回应"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    msg_id = data.get('message_id', '')
    emoji = data.get('emoji', '')
    user_id = session['user_id']

    if not msg_id or not emoji:
        return jsonify(status='error', message='缺少参数'), 400

    with lock:
        conn = get_db()
        # 检查是否已存在该用户对该消息的回应
        existing = conn.execute(
            'SELECT * FROM reactions WHERE msg_id=? AND user_id=?',
            (msg_id, user_id)
        ).fetchone()
        if existing:
            # 更新emoji
            conn.execute(
                'UPDATE reactions SET emoji=? WHERE msg_id=? AND user_id=?',
                (emoji, msg_id, user_id)
            )
        else:
            conn.execute(
                'INSERT INTO reactions (msg_id, user_id, emoji) VALUES (?, ?, ?)',
                (msg_id, user_id, emoji)
            )
        conn.commit()

        # 获取该消息的所有回应
        all_reactions = conn.execute(
            'SELECT user_id, emoji FROM reactions WHERE msg_id=?', (msg_id,)
        ).fetchall()

        # 获取 chat_id（优先内存缓存，fallback到DB）
        chat_id = messages.get(msg_id, {}).get('chat_id', '')
        if not chat_id:
            row = conn.execute('SELECT chat_id FROM messages WHERE id=?', (msg_id,)).fetchone()
            chat_id = row['chat_id'] if row else ''

        conn.close()

    # 广播表情回应更新
    socketio.emit('new_reaction', {
        'message_id': msg_id,
        'chat_id': chat_id,
        'reactions': [{'user_id': r['user_id'], 'emoji': r['emoji']} for r in all_reactions]
    })

    return jsonify(status='success', reactions=[
        {'user_id': r['user_id'], 'emoji': r['emoji']} for r in all_reactions
    ])


@app.route('/react', methods=['DELETE'])
def remove_reaction():
    """删除自己的表情回应"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    data = request.get_json(silent=True)
    msg_id = data.get('message_id', '')
    user_id = session['user_id']

    if not msg_id:
        return jsonify(status='error', message='缺少message_id'), 400

    with lock:
        conn = get_db()
        conn.execute(
            'DELETE FROM reactions WHERE msg_id=? AND user_id=?',
            (msg_id, user_id)
        )
        conn.commit()

        all_reactions = conn.execute(
            'SELECT user_id, emoji FROM reactions WHERE msg_id=?', (msg_id,)
        ).fetchall()

        # 获取 chat_id（优先内存缓存，fallback到DB）
        chat_id = messages.get(msg_id, {}).get('chat_id', '')
        if not chat_id:
            row = conn.execute('SELECT chat_id FROM messages WHERE id=?', (msg_id,)).fetchone()
            chat_id = row['chat_id'] if row else ''

        conn.close()

    # 广播表情回应更新
    socketio.emit('new_reaction', {
        'message_id': msg_id,
        'chat_id': chat_id,
        'reactions': [{'user_id': r['user_id'], 'emoji': r['emoji']} for r in all_reactions]
    })

    return jsonify(status='success', reactions=[
        {'user_id': r['user_id'], 'emoji': r['emoji']} for r in all_reactions
    ])


@app.route('/reactions')
def get_reactions():
    """批量获取消息回应"""
    if 'user_id' not in session:
        return jsonify(reactions={})

    msg_ids_str = request.args.get('msg_ids', '')
    if not msg_ids_str:
        return jsonify(reactions={})

    msg_ids = msg_ids_str.split(',')

    reactions_map = {}
    with lock:
        conn = get_db()
        for msg_id in msg_ids:
            rows = conn.execute(
                'SELECT user_id, emoji FROM reactions WHERE msg_id=?',
                (msg_id.strip(),)
            ).fetchall()
            reactions_map[msg_id.strip()] = [
                {'user_id': r['user_id'], 'emoji': r['emoji']} for r in rows
            ]
        conn.close()

    return jsonify(reactions=reactions_map)


# ══════════════ 个人资料（#12） ══════════════

@app.route('/profile/<user_id>')
def get_profile(user_id):
    """获取用户资料"""
    with lock:
        conn = get_db()
        row = conn.execute(
            'SELECT id, username, avatar_path, nickname, bio, created_at, last_seen, role '
            'FROM users WHERE id=?', (user_id,)
        ).fetchone()
        conn.close()

        if not row:
            return jsonify(status='error', message='用户不存在'), 404

    profile = {
        'id': row['id'],
        'username': row['username'],
        'avatar_path': row['avatar_path'] or '',
        'nickname': row['nickname'] or '',
        'bio': row['bio'] or '',
        'created_at': row['created_at'],
        'last_seen': row['last_seen'],
        'role': row['role'] or 'user',
    }
    return jsonify(status='success', profile=profile)


@app.route('/update_profile', methods=['POST'])
def update_profile():
    """更新个人资料"""
    if 'user_id' not in session:
        return jsonify(status='error', message='请先登录'), 401

    user_id = session['user_id']
    nickname = request.form.get('nickname', '')
    bio = request.form.get('bio', '')
    avatar_path = None

    # 处理头像上传
    if 'avatar' in request.files:
        f = request.files['avatar']
        if f.filename != '':
            ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'png'
            if ext in ('png', 'jpg', 'jpeg', 'gif'):
                raw = f.read()
                if len(raw) <= 2 * 1024 * 1024:
                    filename = f'avatar_{user_id[:8]}.{ext}'
                    sf_dir = os.path.join(BASE_DIR, 'sf')
                    os.makedirs(sf_dir, exist_ok=True)
                    with open(os.path.join(sf_dir, filename), 'wb') as fw:
                        fw.write(raw)
                    avatar_path = filename

    with lock:
        conn = get_db()
        if nickname:
            conn.execute('UPDATE users SET nickname=? WHERE id=?', (nickname, user_id))
        if bio:
            conn.execute('UPDATE users SET bio=? WHERE id=?', (bio, user_id))
        if avatar_path:
            conn.execute('UPDATE users SET avatar_path=? WHERE id=?', (avatar_path, user_id))
        conn.commit()
        conn.close()

    return jsonify(status='success', avatar_path=avatar_path or '')


# ══════════════ 管理员面板（#14） ══════════════

@app.route('/admin/users')
@admin_required
def admin_users():
    """管理员获取所有用户列表"""
    with lock:
        conn = get_db()
        rows = conn.execute(
            'SELECT id, username, avatar_path, nickname, bio, created_at, last_seen, role '
            'FROM users ORDER BY created_at DESC'
        ).fetchall()
        conn.close()

    users = []
    for row in rows:
        users.append({
            'id': row['id'],
            'username': row['username'],
            'avatar_path': row['avatar_path'] or '',
            'nickname': row['nickname'] or '',
            'bio': row['bio'] or '',
            'created_at': row['created_at'],
            'last_seen': row['last_seen'],
            'role': row['role'] or 'user',
        })
    return jsonify(status='success', users=users)


@app.route('/admin/messages')
@admin_required
def admin_messages():
    """管理员获取所有消息（支持搜索）"""
    q = request.args.get('q', '').strip()
    limit = request.args.get('limit', '100')

    try:
        limit = min(int(limit), 500)
    except ValueError:
        limit = 100

    with lock:
        conn = get_db()
        if q:
            rows = conn.execute(
                '''SELECT * FROM messages
                   WHERE message LIKE ? OR sender_name LIKE ?
                   ORDER BY timestamp DESC LIMIT ?''',
                (f'%{q}%', f'%{q}%', limit)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?',
                (limit,)
            ).fetchall()
        conn.close()

    msgs = []
    for row in rows:
        msg = dict(row)
        try:
            msg['message'] = base64.b64decode(msg['message']).decode('utf-8')
        except Exception:
            pass
        msg['viewed_by'] = json.loads(msg['viewed_by'])
        try:
            msg['mentions'] = json.loads(msg.get('mentions', '[]'))
        except Exception:
            msg['mentions'] = []
        msgs.append(msg)

    return jsonify(status='success', messages=msgs)


@app.route('/admin/delete_message', methods=['POST'])
@admin_required
def admin_delete_message():
    """管理员删除消息"""
    data = request.get_json(silent=True)
    msg_id = data.get('message_id', '')

    if not msg_id:
        return jsonify(status='error', message='缺少message_id'), 400

    chat_id = None

    with lock:
        if msg_id in messages:
            chat_id = messages[msg_id].get('chat_id', '')
            del messages[msg_id]

        conn = get_db()

        # 如果不在内存中，从DB查询
        if not chat_id:
            row = conn.execute('SELECT chat_id FROM messages WHERE id=?', (msg_id,)).fetchone()
            chat_id = row['chat_id'] if row else ''

        conn.execute('DELETE FROM messages WHERE id=?', (msg_id,))
        conn.execute('DELETE FROM reactions WHERE msg_id=?', (msg_id,))
        conn.commit()
        conn.close()

    # 广播删除事件
    socketio.emit('message_deleted', {'message_id': msg_id, 'chat_id': chat_id or ''})

    logger.warning(f'[管理员] 消息 {msg_id} 被管理员删除')
    return jsonify(status='success')


@app.route('/admin/ban_user', methods=['POST'])
@admin_required
def admin_ban_user():
    """管理员封禁用户"""
    data = request.get_json(silent=True)
    target_id = data.get('user_id', '')

    if not target_id:
        return jsonify(status='error', message='缺少user_id'), 400

    with lock:
        conn = get_db()
        conn.execute("UPDATE users SET role='banned' WHERE id=?", (target_id,))
        conn.commit()
        conn.close()

    logger.warning(f'[管理员] 用户 {target_id} 被封禁')
    # 通知被封禁的用户
    socketio.emit('banned', {'message': '您的账号已被管理员封禁'}, room=target_id)
    return jsonify(status='success')


@app.route('/admin/unban_user', methods=['POST'])
@admin_required
def admin_unban_user():
    """管理员解封用户"""
    data = request.get_json(silent=True)
    target_id = data.get('user_id', '')

    if not target_id:
        return jsonify(status='error', message='缺少user_id'), 400

    with lock:
        conn = get_db()
        conn.execute("UPDATE users SET role='user' WHERE id=?", (target_id,))
        conn.commit()
        conn.close()

    logger.info(f'[管理员] 用户 {target_id} 被解封')
    return jsonify(status='success')


# ══════════════ 静态文件 ══════════════

@app.route('/sf/<path:filename>')
def sf_files(filename):
    return send_from_directory(BASE_DIR, os.path.join('sf', filename))


@app.route('/aud/<path:filename>')
def aud_files(filename):
    return send_from_directory(BASE_DIR, os.path.join('aud', filename))


# ══════════════ 网络工具 ══════════════

def get_local_ips():
    """获取本机局域网IP"""
    ips = []
    try:
        hostname = socket.gethostname()
        for addr in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = addr[4][0]
            if ip != '127.0.0.1' and not ip.startswith('169.254.'):
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


# ══════════════ 入口 ══════════════

if __name__ == '__main__':
    os.makedirs(os.path.join(BASE_DIR, 'sf'), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, 'aud'), exist_ok=True)

    # 初始化数据库
    init_db()
    load_from_db()

    PREFERRED_PORT = 80   # 优先尝试 80（无需加端口号直接访问）
    FALLBACK_PORT = 5000  # 80 不可用时降级到 5000

    logger.info('=' * 50)
    logger.info('   NN 聊天室 - 多人即时通讯 (WebSocket)')
    logger.info('=' * 50)

    local_ips = get_local_ips()
    logger.info('检测到本机可用 IP 地址：')
    for i, ip in enumerate(local_ips, 1):
        logger.info(f'  {i}. {ip}')
    logger.info('  0. 使用 127.0.0.1 (仅本机)')
    logger.info('  也可以直接输入其他 IP (如 0.0.0.0 监听所有接口)')

    while True:
        choice = input('\n请选择或输入监听地址 (默认 127.0.0.1): ').strip()
        if choice == '':
            host = '127.0.0.1'
            break
        elif choice == '0':
            host = '127.0.0.1'
            break
        elif choice.isdigit() and 1 <= int(choice) <= len(local_ips):
            host = local_ips[int(choice) - 1]
            break
        else:
            try:
                socket.inet_aton(choice)
                host = choice
                break
            except Exception:
                logger.warning('无效的 IP 地址，请重新输入')

    # 确定最终端口：用标准 socket 预检 80 是否可用
    try:
        sock_test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock_test.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock_test.bind((host, PREFERRED_PORT))
        sock_test.close()
        PORT = PREFERRED_PORT
    except (PermissionError, OSError):
        logger.warning(f'端口 {PREFERRED_PORT} 需要管理员权限或已被占用，使用端口 {FALLBACK_PORT}...')
        PORT = FALLBACK_PORT

    logger.info('')
    logger.info('服务器启动中...')
    logger.info(f'监听地址: {host}:{PORT}')
    if host == '0.0.0.0':
        logger.info('局域网内其他设备可通过以下地址访问：')
        for ip in local_ips:
            if PORT == 80:
                logger.info(f'  http://{ip}/')
            else:
                logger.info(f'  http://{ip}:{PORT}/')
    elif host == '127.0.0.1':
        logger.info(f'仅本机可访问: http://127.0.0.1:{PORT}/')
    else:
        logger.info(f'请通过 http://{host}:{PORT}/ 访问')
    logger.info('功能：登录注册、好友私聊、群组聊天、在线状态')
    logger.info('按 Ctrl+C 停止服务器')
    logger.info('=' * 50)
    logger.info('=' * 50)
    socketio.run(app, host=host, port=PORT)
