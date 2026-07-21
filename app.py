import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from flask_socketio import SocketIO, send
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

if 'SECRET_KEY' not in os.environ:
    raise RuntimeError(
        "SECRET_KEY 환경변수가 설정되지 않았습니다. .env 파일을 생성하고 SECRET_KEY를 정의하세요 "
        "(.env.example 참고)."
    )

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ['SECRET_KEY']

# Render 등 리버스 프록시 뒤에서 실행될 때 요청 스킴/호스트/클라이언트 IP를 올바르게 인식하도록 함
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

FLASK_DEBUG = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

# 로컬 http 개발 편의성을 위해 디버그 모드일 때만 Secure 플래그를 끔
app.config['SESSION_COOKIE_SECURE'] = not FLASK_DEBUG
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.permanent_session_lifetime = timedelta(minutes=30)

DATABASE = 'market.db'
socketio = SocketIO(app)
csrf = CSRFProtect(app)

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{4,20}$')
LOGIN_FAIL_LIMIT = 5
LOCK_DURATION = timedelta(minutes=15)
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'


def utcnow_str():
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_utc(value):
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


# 데이터베이스 연결 관리: 요청마다 연결 생성 후 사용, 종료 시 close
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row  # 결과를 dict처럼 사용하기 위함
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# 테이블 생성 (최초 실행 시에만) + 컬럼 마이그레이션
def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        # 사용자 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                bio TEXT
            )
        """)
        # 상품 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS product (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price TEXT NOT NULL,
                seller_id TEXT NOT NULL
            )
        """)
        # 신고 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report (
                id TEXT PRIMARY KEY,
                reporter_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        db.commit()

        # user 테이블 마이그레이션: 컬럼 하나당 독립된 try/except로 처리
        # (하나의 try에 몰아넣으면 먼저 실행된 ALTER가 실패하는 순간 뒤의 컬럼들이 전혀 추가되지 않음)
        try:
            cursor.execute("ALTER TABLE user ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN locked_until TEXT DEFAULT NULL")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN session_token TEXT DEFAULT NULL")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
            db.commit()
        except sqlite3.OperationalError:
            pass

        # 관리자 계정 환경변수 자동 시딩 (spec.md §2-2, 멱등적으로 동작: 이미 있으면 아무것도 안 함)
        admin_username = os.environ.get('ADMIN_USERNAME')
        admin_password = os.environ.get('ADMIN_PASSWORD')
        if admin_username and admin_password:
            cursor.execute("SELECT id FROM user WHERE username = ?", (admin_username,))
            if cursor.fetchone() is None:
                admin_id = str(uuid.uuid4())
                admin_password_hash = bcrypt.hashpw(admin_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                cursor.execute(
                    "INSERT INTO user (id, username, password, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (admin_id, admin_username, admin_password_hash, 'admin', utcnow_str())
                )
                db.commit()


# gunicorn이 app.py를 모듈로 import하는 배포 환경에서는 __name__이 '__main__'이 아니므로,
# init_db()는 반드시 모듈 최상단(여기)에서 호출해야 함. 블랭킷 try/except로 감싸지 않음 —
# 초기화가 실패하면 그 에러가 그대로 드러나야 원인을 찾을 수 있음.
init_db()


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        user_id = session.get('user_id')
        if not user_id:
            session.clear()
            flash('로그인이 필요합니다.')
            return redirect(url_for('login'))
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if user is None or user['status'] != 'active' or session.get('session_token') != user['session_token']:
            session.clear()
            flash('세션이 만료되었거나 더 이상 유효하지 않습니다. 다시 로그인해주세요.')
            return redirect(url_for('login'))
        return view_func(*args, **kwargs)
    return wrapped_view


# 기본 라우트
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


# 회원가입
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')

        if not USERNAME_RE.match(username):
            flash('사용자명은 영문, 숫자, 밑줄(_)로 이루어진 4~20자여야 합니다.')
            return render_template('register.html')
        if len(password) < 8:
            flash('비밀번호는 최소 8자 이상이어야 합니다.')
            return render_template('register.html')

        db = get_db()
        cursor = db.cursor()
        # 중복 사용자 체크
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        if cursor.fetchone() is not None:
            flash('이미 존재하는 사용자명입니다.')
            return redirect(url_for('register'))
        user_id = str(uuid.uuid4())
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cursor.execute(
            "INSERT INTO user (id, username, password, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, password_hash, utcnow_str())
        )
        db.commit()
        flash('회원가입이 완료되었습니다. 로그인 해주세요.')
        return redirect(url_for('login'))
    return render_template('register.html')


# 로그인
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        user = cursor.fetchone()

        if user is None:
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))

        now = datetime.now(timezone.utc)
        if user['locked_until']:
            locked_until = parse_utc(user['locked_until'])
            if locked_until > now:
                flash('로그인 실패 횟수가 초과되어 계정이 잠겼습니다. 잠시 후 다시 시도해주세요.')
                return redirect(url_for('login'))

        if not bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            failed_count = user['failed_login_count'] + 1
            if failed_count >= LOGIN_FAIL_LIMIT:
                locked_until_str = (now + LOCK_DURATION).strftime(TIMESTAMP_FORMAT)
                cursor.execute(
                    "UPDATE user SET failed_login_count = ?, locked_until = ? WHERE id = ?",
                    (failed_count, locked_until_str, user['id'])
                )
            else:
                cursor.execute(
                    "UPDATE user SET failed_login_count = ? WHERE id = ?",
                    (failed_count, user['id'])
                )
            db.commit()
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))

        if user['status'] != 'active':
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))

        session_token = str(uuid.uuid4())
        cursor.execute(
            "UPDATE user SET failed_login_count = 0, locked_until = NULL, session_token = ? WHERE id = ?",
            (session_token, user['id'])
        )
        db.commit()

        session.clear()
        session.permanent = True
        session['user_id'] = user['id']
        session['session_token'] = session_token
        flash('로그인 성공!')
        return redirect(url_for('dashboard'))
    return render_template('login.html')


# 로그아웃
@app.route('/logout', methods=['POST'])
def logout():
    user_id = session.get('user_id')
    if user_id:
        db = get_db()
        cursor = db.cursor()
        # 탈취된 옛 세션 쿠키의 재사용을 막기 위해 DB의 session_token도 즉시 무효화
        cursor.execute("UPDATE user SET session_token = NULL WHERE id = ?", (user_id,))
        db.commit()
    session.clear()
    flash('로그아웃되었습니다.')
    return redirect(url_for('index'))


# 대시보드: 사용자 정보와 전체 상품 리스트 표시
@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    cursor = db.cursor()
    # 현재 사용자 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()
    # 모든 상품 조회
    cursor.execute("SELECT * FROM product")
    all_products = cursor.fetchall()
    return render_template('dashboard.html', products=all_products, user=current_user)


# 프로필 페이지: bio 업데이트 가능
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        bio = request.form.get('bio', '')
        cursor.execute("UPDATE user SET bio = ? WHERE id = ?", (bio, session['user_id']))
        db.commit()
        flash('프로필이 업데이트되었습니다.')
        return redirect(url_for('profile'))
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()
    return render_template('profile.html', user=current_user)


# 상품 등록
@app.route('/product/new', methods=['GET', 'POST'])
@login_required
def new_product():
    if request.method == 'POST':
        title = request.form.get('title', '')
        description = request.form.get('description', '')
        price_raw = request.form.get('price', '')

        if not (1 <= len(title) <= 100):
            flash('상품명은 1~100자 이내로 입력해주세요.')
            return render_template('new_product.html')
        if not (1 <= len(description) <= 2000):
            flash('상품 설명은 1~2000자 이내로 입력해주세요.')
            return render_template('new_product.html')
        try:
            price = int(price_raw)
        except ValueError:
            flash('가격은 숫자로 입력해주세요.')
            return render_template('new_product.html')
        if price < 0:
            flash('가격은 0 이상이어야 합니다.')
            return render_template('new_product.html')

        db = get_db()
        cursor = db.cursor()
        product_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO product (id, title, description, price, seller_id) VALUES (?, ?, ?, ?, ?)",
            (product_id, title, description, str(price), session['user_id'])
        )
        db.commit()
        flash('상품이 등록되었습니다.')
        return redirect(url_for('dashboard'))
    return render_template('new_product.html')


# 상품 상세보기
@app.route('/product/<product_id>')
def view_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    # 판매자 정보 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (product['seller_id'],))
    seller = cursor.fetchone()
    return render_template('view_product.html', product=product, seller=seller)


# 신고하기
@app.route('/report', methods=['GET', 'POST'])
@login_required
def report():
    if request.method == 'POST':
        target_id = request.form.get('target_id', '')
        reason = request.form.get('reason', '')
        db = get_db()
        cursor = db.cursor()
        report_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO report (id, reporter_id, target_id, reason) VALUES (?, ?, ?, ?)",
            (report_id, session['user_id'], target_id, reason)
        )
        db.commit()
        flash('신고가 접수되었습니다.')
        return redirect(url_for('dashboard'))
    return render_template('report.html')


# 실시간 채팅: 클라이언트가 메시지를 보내면 전체 브로드캐스트
@socketio.on('send_message')
def handle_send_message_event(data):
    if 'user_id' not in session:
        return
    if not isinstance(data, dict):
        return
    message = data.get('message', '')
    if not isinstance(message, str):
        return
    if not message.strip():
        return
    if len(message) > 500:
        return
    data['message_id'] = str(uuid.uuid4())
    send(data, broadcast=True)


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return render_template('error.html', code=403, message='요청이 유효하지 않습니다(CSRF 토큰 오류). 페이지를 새로고침한 뒤 다시 시도해주세요.'), 403


@app.errorhandler(400)
def handle_bad_request(e):
    return render_template('error.html', code=400, message='잘못된 요청입니다.'), 400


@app.errorhandler(403)
def handle_forbidden(e):
    return render_template('error.html', code=403, message='접근이 거부되었습니다.'), 403


@app.errorhandler(404)
def handle_not_found(e):
    return render_template('error.html', code=404, message='페이지를 찾을 수 없습니다.'), 404


@app.errorhandler(500)
def handle_server_error(e):
    return render_template('error.html', code=500, message='서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.'), 500


@app.after_request
def set_security_headers(response):
    response.headers['Content-Security-Policy'] = (
        "script-src 'self' https://cdnjs.cloudflare.com; "
        "connect-src 'self' ws: wss:;"
    )
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


if __name__ == '__main__':
    socketio.run(app, debug=FLASK_DEBUG)
