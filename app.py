import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, send, emit, join_room
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from security import (
    validate_password,
    validate_username,
    validate_product_title,
    validate_product_description,
    validate_product_price,
    validate_report_reason,
    owner_required,
    admin_required,
    get_product_seller_id,
    log_action,
    save_product_image,
    delete_product_image,
    socket_user_or_none,
    REPORT_THRESHOLD,
    UPLOAD_DIR,
)

load_dotenv()

# SECRET_KEY는 세션 서명/CSRF 토큰 생성에 쓰이는 필수값이라 하드코딩 기본값을 두지 않는다
# (기본값을 두면 원본 코드의 하드코딩 결함이 되살아난다). .env가 아예 없는 경우뿐 아니라
# .env는 있지만 SECRET_KEY 줄이 비어있는 경우(`SECRET_KEY=`)도 os.environ에는 빈 문자열로
# 들어오므로 함께 걸러낸다. 스택 트레이스 대신 설정 방법을 안내하고 깔끔하게 종료한다.
_secret_key = os.environ.get('SECRET_KEY', '').strip()
if not _secret_key:
    print(
        "[설정 오류] SECRET_KEY가 설정되지 않았습니다.\n"
        "  1. .env.example을 복사해 .env 파일을 만드세요:\n"
        "     cp .env.example .env\n"
        "  2. .env 파일을 열어 SECRET_KEY에 랜덤한 값을 채우세요. 예:\n"
        "     python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "  3. 서버를 다시 실행하세요:\n"
        "     python app.py",
        file=sys.stderr,
    )
    sys.exit(1)

app = Flask(__name__)
app.config['SECRET_KEY'] = _secret_key

# Render 등 리버스 프록시 뒤에서 실행될 때 요청 스킴/호스트/클라이언트 IP를 올바르게 인식하도록 함
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

FLASK_DEBUG = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

# 로컬 http 개발 편의성을 위해 디버그 모드일 때만 Secure 플래그를 끔
app.config['SESSION_COOKIE_SECURE'] = not FLASK_DEBUG
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.permanent_session_lifetime = timedelta(minutes=30)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 상품 이미지 업로드 5MB 제한

DATABASE = 'market.db'
socketio = SocketIO(app)
csrf = CSRFProtect(app)
limiter = Limiter(key_func=get_remote_address, app=app, storage_uri="memory://")

LOGIN_FAIL_LIMIT = 5
LOCK_DURATION = timedelta(minutes=15)
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'

# 배포/로컬 어느 환경에서든 상품 이미지 저장 디렉토리가 미리 존재하도록 부팅 시 생성
os.makedirs(UPLOAD_DIR, exist_ok=True)


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

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN password_history TEXT NOT NULL DEFAULT '[]'")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN last_login_at TEXT DEFAULT NULL")
            db.commit()
        except sqlite3.OperationalError:
            pass

        # product 테이블 마이그레이션: price TEXT -> INTEGER 재생성 (재실행 안전: 이미 INTEGER면 스킵)
        cursor.execute("PRAGMA table_info(product)")
        product_columns = {row['name']: row['type'] for row in cursor.fetchall()}
        if product_columns.get('price') != 'INTEGER':
            cursor.execute("""
                CREATE TABLE product_new (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    price INTEGER NOT NULL CHECK (price >= 0),
                    seller_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    report_count INTEGER NOT NULL DEFAULT 0,
                    image_filename TEXT DEFAULT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                INSERT INTO product_new (id, title, description, price, seller_id)
                SELECT id, title, description, MAX(CAST(price AS INTEGER), 0), seller_id FROM product
            """)
            cursor.execute("DROP TABLE product")
            cursor.execute("ALTER TABLE product_new RENAME TO product")
            db.commit()

        # product 테이블에 신규 컬럼이 빠져 있는 과거 상태 대비 (재생성을 스킵한 경우에도 안전하게)
        try:
            cursor.execute("ALTER TABLE product ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE product ADD COLUMN report_count INTEGER NOT NULL DEFAULT 0")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE product ADD COLUMN image_filename TEXT DEFAULT NULL")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE product ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
            db.commit()
        except sqlite3.OperationalError:
            pass

        # 접근/조작 이력 감사 로그 테이블 (신규)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                actor_id TEXT,
                actor_username TEXT,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                ip_address TEXT,
                user_agent TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

        # --- Phase 2B/2C 마이그레이션 -----------------------------------

        # user 테이블: 송금/신고 자동조치에 필요한 컬럼 (spec.md §1에 있었으나 누락돼 있던 것)
        try:
            cursor.execute("ALTER TABLE user ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE user ADD COLUMN report_count INTEGER NOT NULL DEFAULT 0")
            db.commit()
        except sqlite3.OperationalError:
            pass

        # report 테이블 확장. ALTER ADD COLUMN ... DEFAULT는 비파괴적이므로 spec §3-3의
        # "0이 아니면 중단" 가드는 여기서는 적용하지 않고(모듈 최상단 실행 중 중단하면 배포
        # 서버 전체가 기동 실패한다), 대신 기존 행이 있으면 경고만 출력한다.
        cursor.execute("SELECT COUNT(*) AS c FROM report")
        existing_report_count = cursor.fetchone()['c']
        if existing_report_count != 0:
            print(f"[MIGRATION WARNING] 기존 report {existing_report_count}건에 target_type='product' "
                  f"기본값이 부여됨. 실제 대상 종류 수동 확인 필요.")

        try:
            cursor.execute("ALTER TABLE report ADD COLUMN target_type TEXT NOT NULL DEFAULT 'product'")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE report ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE report ADD COLUMN auto_action_taken INTEGER NOT NULL DEFAULT 0")
            db.commit()
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE report ADD COLUMN auto_action_at TEXT DEFAULT NULL")
            db.commit()
        except sqlite3.OperationalError:
            pass

        # 중복 신고 방지용 UNIQUE 인덱스 (애플리케이션 레벨 체크의 이중 안전장치).
        # 기존 데이터에 중복이 있으면 생성 실패 -> 경고만 남기고 진행 (앱을 죽이지 않음)
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_unique "
                "ON report (reporter_id, target_type, target_id)"
            )
            db.commit()
        except sqlite3.OperationalError as e:
            print(f"[MIGRATION WARNING] idx_report_unique 생성 실패 (기존 데이터에 중복 신고 존재 가능): {e}")

        # 관리자 수동/시스템 자동 조치 감사 로그 (신규)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_action_log (
                id TEXT PRIMARY KEY,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

        # 1:1 다이렉트 메시지 (신규)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS direct_message (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

        # 송금 내역. "transaction"은 SQL 예약어이므로 모든 참조에서 반드시 큰따옴표로 감쌀 것
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS "transaction" (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

        # 전체 채팅 영구 저장 (신규)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_message (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                sender_username TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

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
@limiter.limit('5 per hour', methods=['POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')

        if validate_username(username):
            flash('사용자명은 영문, 숫자, 밑줄(_)로 이루어진 4~20자여야 합니다.')
            return render_template('register.html')
        password_errors = validate_password(password)
        if password_errors:
            flash('비밀번호 조건을 만족하지 않습니다 (' + ', '.join(password_errors) + ').')
            return render_template('register.html')

        db = get_db()
        cursor = db.cursor()
        # 중복 사용자 체크 (모든 검증을 통과한 뒤에만 DB를 조회/기록한다)
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        if cursor.fetchone() is not None:
            flash('이미 존재하는 사용자명입니다.')
            return redirect(url_for('register'))
        user_id = str(uuid.uuid4())
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        try:
            cursor.execute(
                "INSERT INTO user (id, username, password, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, password_hash, utcnow_str())
            )
            db.commit()
        except sqlite3.IntegrityError:
            # 동시에 같은 username으로 가입 요청이 들어온 경우(TOCTOU) 대비
            flash('이미 존재하는 사용자명입니다.')
            return redirect(url_for('register'))
        log_action('register', success=True, actor_id=user_id, actor_username=username)
        flash('회원가입이 완료되었습니다. 로그인 해주세요.')
        return redirect(url_for('login'))
    return render_template('register.html')


# 로그인
@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        user = cursor.fetchone()

        if user is None:
            log_action('login_failure', success=False, actor_username=username)
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))

        now = datetime.now(timezone.utc)
        if user['locked_until']:
            locked_until = parse_utc(user['locked_until'])
            if locked_until > now:
                log_action('login_failure', success=False, actor_id=user['id'], actor_username=username)
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
            log_action('login_failure', success=False, actor_id=user['id'], actor_username=username)
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))

        if user['status'] != 'active':
            log_action('login_failure', success=False, actor_id=user['id'], actor_username=username)
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))

        session_token = str(uuid.uuid4())
        cursor.execute(
            "UPDATE user SET failed_login_count = 0, locked_until = NULL, session_token = ?, last_login_at = ? WHERE id = ?",
            (session_token, utcnow_str(), user['id'])
        )
        db.commit()

        session.clear()
        session.permanent = True
        session['user_id'] = user['id']
        session['session_token'] = session_token
        log_action('login_success', success=True, actor_id=user['id'], actor_username=user['username'])
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
        log_action('logout', success=True, actor_id=user_id)
    session.clear()
    flash('로그아웃되었습니다.')
    return redirect(url_for('index'))


# 대시보드: 사용자 정보와 활성 상품 리스트 표시
@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    cursor = db.cursor()
    # 현재 사용자 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()
    # 활성 상품만 조회
    cursor.execute("SELECT * FROM product WHERE status = 'active' ORDER BY created_at DESC")
    all_products = cursor.fetchall()
    # 전체 채팅 최근 50건 (오래된 순으로 표시)
    cursor.execute(
        "SELECT * FROM (SELECT * FROM global_message ORDER BY created_at DESC LIMIT 50) ORDER BY created_at ASC"
    )
    global_messages = cursor.fetchall()
    return render_template('dashboard.html', products=all_products, user=current_user, global_messages=global_messages)


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


# 비밀번호 변경 (재인증 + 재사용 방지)
@app.route('/profile/password', methods=['POST'])
@login_required
@limiter.limit('3 per hour', methods=['POST'])
def change_password():
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    new_password_confirm = request.form.get('new_password_confirm', '')

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    user = cursor.fetchone()

    if not bcrypt.checkpw(current_password.encode('utf-8'), user['password'].encode('utf-8')):
        log_action('password_change', success=False)
        flash('현재 비밀번호가 올바르지 않습니다.')
        return redirect(url_for('profile'))

    if new_password != new_password_confirm:
        flash('새 비밀번호와 확인이 일치하지 않습니다.')
        return redirect(url_for('profile'))

    password_errors = validate_password(new_password)
    if password_errors:
        flash('비밀번호 조건을 만족하지 않습니다 (' + ', '.join(password_errors) + ').')
        return redirect(url_for('profile'))

    if bcrypt.checkpw(new_password.encode('utf-8'), user['password'].encode('utf-8')):
        flash('최근 사용한 비밀번호는 재사용할 수 없습니다.')
        return redirect(url_for('profile'))

    try:
        history = json.loads(user['password_history']) if user['password_history'] else []
    except (TypeError, ValueError):
        history = []

    for old_hash in history:
        if bcrypt.checkpw(new_password.encode('utf-8'), old_hash.encode('utf-8')):
            flash('최근 사용한 비밀번호는 재사용할 수 없습니다.')
            return redirect(url_for('profile'))

    new_password_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    history.insert(0, user['password'])
    history = history[:5]
    new_session_token = str(uuid.uuid4())

    cursor.execute(
        "UPDATE user SET password = ?, password_history = ?, session_token = ? WHERE id = ?",
        (new_password_hash, json.dumps(history), new_session_token, session['user_id'])
    )
    db.commit()
    session['session_token'] = new_session_token
    log_action('password_change', success=True)
    flash('비밀번호가 변경되었습니다.')
    return redirect(url_for('profile'))


# 송금 (재인증 + 원자적 트랜잭션)
@app.route('/transfer', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per hour', methods=['POST'])
def transfer():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()

    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        receiver_username = request.form.get('receiver_username', '')
        amount_raw = request.form.get('amount', '')

        # 1. 민감 작업 재인증
        if not bcrypt.checkpw(current_password.encode('utf-8'), current_user['password'].encode('utf-8')):
            flash('현재 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('transfer'))

        # 2. amount 검증
        try:
            amount = int(amount_raw)
        except (TypeError, ValueError):
            flash('금액은 숫자로 입력해주세요.')
            return redirect(url_for('transfer'))
        if amount <= 0:
            flash('금액은 0보다 커야 합니다.')
            return redirect(url_for('transfer'))

        # 3. 수신자 조회
        cursor.execute("SELECT * FROM user WHERE username = ?", (receiver_username,))
        receiver = cursor.fetchone()
        if receiver is None or receiver['status'] != 'active':
            flash('존재하지 않거나 이용할 수 없는 수신자입니다.')
            return redirect(url_for('transfer'))

        # 4. 자기 자신에게 송금 방지
        if receiver['id'] == current_user['id']:
            flash('자기 자신에게는 송금할 수 없습니다.')
            return redirect(url_for('transfer'))

        # 트랜잭션: 잔액 조건부 차감 -> 증가 -> transaction 기록. 전부 하나의 트랜잭션.
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "UPDATE user SET balance = balance - ? WHERE id = ? AND balance >= ?",
                (amount, current_user['id'], amount)
            )
            if cursor.rowcount == 0:
                db.rollback()
                flash('잔액이 부족합니다.')
                return redirect(url_for('transfer'))

            cursor.execute(
                "UPDATE user SET balance = balance + ? WHERE id = ?",
                (amount, receiver['id'])
            )
            transaction_id = str(uuid.uuid4())
            cursor.execute(
                'INSERT INTO "transaction" (id, sender_id, receiver_id, amount, created_at) VALUES (?, ?, ?, ?, ?)',
                (transaction_id, current_user['id'], receiver['id'], amount, utcnow_str())
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            flash('송금 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.')
            return redirect(url_for('transfer'))

        # 🔴 log_action은 반드시 커밋 이후에 호출 (트랜잭션 중간에 부르면 그 시점까지만 커밋되고 끊김)
        log_action('transfer', target_type='user', target_id=receiver['id'], success=True)
        flash(f'{receiver_username}님에게 {amount}원을 송금했습니다.')
        return redirect(url_for('dashboard'))

    return render_template('transfer.html', user=current_user)


def dm_room_id(user_a, user_b):
    """두 user_id를 정렬 후 결합해 결정론적인 room id를 만든다. UUID는 '_'를 포함하지
    않으므로 '_'로 split해도 참여자 id가 안전하게 분리된다."""
    lo, hi = sorted([user_a, user_b])
    return f'dm_{lo}_{hi}'


def dm_room_participants(room_id):
    """room_id가 dm_room_id()로 생성 가능한 정상 형태인지 검증하고, 맞으면
    (참여자 id 2개) 튜플을, 아니면 None을 반환한다."""
    if not isinstance(room_id, str):
        return None
    parts = room_id.split('_')
    if len(parts) != 3 or parts[0] != 'dm' or not parts[1] or not parts[2]:
        return None
    a, b = parts[1], parts[2]
    if dm_room_id(a, b) != room_id:
        return None
    return a, b


# 쪽지함: 나와 DM을 주고받은 상대 목록
@app.route('/messages')
@login_required
def dm_list():
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """SELECT DISTINCT CASE WHEN sender_id = ? THEN receiver_id ELSE sender_id END AS other_id
           FROM direct_message WHERE sender_id = ? OR receiver_id = ?""",
        (session['user_id'], session['user_id'], session['user_id'])
    )
    other_ids = [row['other_id'] for row in cursor.fetchall()]
    partners = []
    for other_id in other_ids:
        cursor.execute("SELECT id, username FROM user WHERE id = ?", (other_id,))
        partner = cursor.fetchone()
        if partner is not None:
            partners.append(partner)
    return render_template('dm_list.html', partners=partners)


# 특정 상대와의 1:1 대화 스레드
@app.route('/message/<user_id>')
@login_required
def dm_thread(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, username, status FROM user WHERE id = ?", (user_id,))
    other = cursor.fetchone()
    if other is None or other['status'] != 'active':
        abort(404)
    cursor.execute(
        """SELECT * FROM direct_message
           WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
           ORDER BY created_at ASC""",
        (session['user_id'], user_id, user_id, session['user_id'])
    )
    messages = cursor.fetchall()
    room_id = dm_room_id(session['user_id'], user_id)
    return render_template('dm_thread.html', other=other, messages=messages, room_id=room_id)


# 공개 프로필: 누구나 조회 가능, 민감정보 미노출
@app.route('/user/<user_id>')
def user_profile(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, username, bio, created_at, status FROM user WHERE id = ?", (user_id,))
    profile_user = cursor.fetchone()
    if profile_user is None or profile_user['status'] != 'active':
        abort(404)
    cursor.execute(
        "SELECT * FROM product WHERE seller_id = ? AND status = 'active' ORDER BY created_at DESC",
        (user_id,)
    )
    products = cursor.fetchall()
    return render_template('user_profile.html', profile_user=profile_user, products=products)


# 상품 등록
@app.route('/product/new', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per hour', methods=['POST'])
def new_product():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        price_raw = request.form.get('price', '')

        title_errors = validate_product_title(title)
        if title_errors:
            flash('상품명 ' + ', '.join(title_errors))
            return render_template('new_product.html')
        description_errors = validate_product_description(description)
        if description_errors:
            flash('상품 설명 ' + ', '.join(description_errors))
            return render_template('new_product.html')
        price_errors, price = validate_product_price(price_raw)
        if price_errors:
            flash('가격 ' + ', '.join(price_errors))
            return render_template('new_product.html')

        image_filename, image_error = save_product_image(request.files.get('image'))
        if image_error:
            flash(image_error)
            return render_template('new_product.html')

        db = get_db()
        cursor = db.cursor()
        product_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO product (id, title, description, price, seller_id, status, report_count, image_filename, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, title, description, price, session['user_id'], 'active', 0, image_filename, utcnow_str())
        )
        db.commit()
        log_action('product_create', target_type='product', target_id=product_id)
        flash('상품이 등록되었습니다.')
        return redirect(url_for('dashboard'))
    return render_template('new_product.html')


# 상품 상세보기 (활성 상품만 노출)
@app.route('/product/<product_id>')
def view_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if product is None or product['status'] != 'active':
        abort(404)
    # 판매자 정보 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (product['seller_id'],))
    seller = cursor.fetchone()
    return render_template('view_product.html', product=product, seller=seller)


# 상품 수정 (소유자만)
@app.route('/product/<product_id>/edit', methods=['GET', 'POST'])
@login_required
@owner_required(lambda product_id: get_product_seller_id(product_id))
def edit_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if product is None:
        abort(404)

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        price_raw = request.form.get('price', '')

        title_errors = validate_product_title(title)
        if title_errors:
            flash('상품명 ' + ', '.join(title_errors))
            return render_template('edit_product.html', product=product)
        description_errors = validate_product_description(description)
        if description_errors:
            flash('상품 설명 ' + ', '.join(description_errors))
            return render_template('edit_product.html', product=product)
        price_errors, price = validate_product_price(price_raw)
        if price_errors:
            flash('가격 ' + ', '.join(price_errors))
            return render_template('edit_product.html', product=product)

        image_file = request.files.get('image')
        new_image_filename = product['image_filename']
        if image_file and image_file.filename:
            saved_filename, image_error = save_product_image(image_file)
            if image_error:
                flash(image_error)
                return render_template('edit_product.html', product=product)
            delete_product_image(product['image_filename'])
            new_image_filename = saved_filename

        cursor.execute(
            "UPDATE product SET title = ?, description = ?, price = ?, image_filename = ? WHERE id = ?",
            (title, description, price, new_image_filename, product_id)
        )
        db.commit()
        log_action('product_update', target_type='product', target_id=product_id)
        flash('상품이 수정되었습니다.')
        return redirect(url_for('view_product', product_id=product_id))
    return render_template('edit_product.html', product=product)


# 상품 삭제 (소유자만, soft delete)
@app.route('/product/<product_id>/delete', methods=['POST'])
@login_required
@owner_required(lambda product_id: get_product_seller_id(product_id))
def delete_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if product is None:
        abort(404)

    cursor.execute("UPDATE product SET status = 'deleted' WHERE id = ?", (product_id,))
    db.commit()
    delete_product_image(product['image_filename'])
    log_action('product_delete', target_type='product', target_id=product_id)
    flash('상품이 삭제되었습니다.')
    return redirect(url_for('dashboard'))


# 상품 검색 (제목 LIKE 검색, 특수문자 이스케이프)
@app.route('/product/search')
def search_products():
    q = request.args.get('q', '')
    if not (1 <= len(q) <= 50):
        return render_template('search_results.html', products=[], query=q)

    escaped_q = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM product WHERE title LIKE ? ESCAPE '\\' AND status = 'active' ORDER BY created_at DESC LIMIT 100",
        ('%' + escaped_q + '%',)
    )
    products = cursor.fetchall()
    return render_template('search_results.html', products=products, query=q)


# 신고하기 (대상 타입 검증 + 자기/중복 신고 방지 + 임계치 자동조치)
@app.route('/report', methods=['GET', 'POST'])
@login_required
@limiter.limit('5 per minute', methods=['POST'])
def report():
    if request.method == 'POST':
        target_type = request.form.get('target_type', '')
        target_id = request.form.get('target_id', '')
        reason = request.form.get('reason', '').strip()

        if target_type not in ('user', 'product'):
            abort(400)

        reason_errors = validate_report_reason(reason)
        if reason_errors:
            flash('신고 사유 ' + ', '.join(reason_errors))
            return render_template('report.html', target_type=target_type, target_id=target_id)

        db = get_db()
        cursor = db.cursor()

        if target_type == 'user':
            cursor.execute("SELECT id FROM user WHERE id = ?", (target_id,))
            if cursor.fetchone() is None:
                abort(400)
            if target_id == session['user_id']:
                flash('자기 자신은 신고할 수 없습니다.')
                return redirect(url_for('report'))
        else:
            cursor.execute("SELECT id, seller_id FROM product WHERE id = ?", (target_id,))
            target_product = cursor.fetchone()
            if target_product is None:
                abort(400)
            if target_product['seller_id'] == session['user_id']:
                flash('본인 상품은 신고할 수 없습니다.')
                return redirect(url_for('report'))

        cursor.execute(
            "SELECT id FROM report WHERE reporter_id = ? AND target_type = ? AND target_id = ?",
            (session['user_id'], target_type, target_id)
        )
        if cursor.fetchone() is not None:
            flash('이미 신고한 대상입니다.')
            return redirect(url_for('report'))

        report_id = str(uuid.uuid4())
        created_at = utcnow_str()
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "INSERT INTO report (id, reporter_id, target_id, reason, target_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, session['user_id'], target_id, reason, target_type, created_at)
            )

            auto_action = None
            if target_type == 'user':
                cursor.execute("UPDATE user SET report_count = report_count + 1 WHERE id = ?", (target_id,))
                cursor.execute("SELECT report_count, status FROM user WHERE id = ?", (target_id,))
                state = cursor.fetchone()
                if state['report_count'] >= REPORT_THRESHOLD and state['status'] != 'suspended':
                    cursor.execute(
                        "UPDATE user SET status = 'suspended', session_token = NULL WHERE id = ?",
                        (target_id,)
                    )
                    auto_action = 'suspend_user'
            else:
                cursor.execute("UPDATE product SET report_count = report_count + 1 WHERE id = ?", (target_id,))
                cursor.execute("SELECT report_count, status FROM product WHERE id = ?", (target_id,))
                state = cursor.fetchone()
                if state['report_count'] >= REPORT_THRESHOLD and state['status'] != 'blocked':
                    cursor.execute("UPDATE product SET status = 'blocked' WHERE id = ?", (target_id,))
                    auto_action = 'block_product'

            if auto_action:
                cursor.execute(
                    "UPDATE report SET auto_action_taken = 1, auto_action_at = ? WHERE id = ?",
                    (created_at, report_id)
                )
                cursor.execute(
                    "INSERT INTO admin_action_log "
                    "(id, actor_type, actor_id, action, target_type, target_id, reason, created_at) "
                    "VALUES (?, 'system', NULL, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), auto_action, target_type, target_id, report_id, created_at)
                )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash('이미 신고한 대상입니다.')
            return redirect(url_for('report'))
        except sqlite3.Error:
            db.rollback()
            flash('신고 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.')
            return redirect(url_for('report'))

        # 🔴 log_action은 커밋 이후에 호출
        log_action('report_create', target_type=target_type, target_id=target_id, success=True)
        flash('신고가 접수되었습니다.')
        return redirect(url_for('dashboard'))

    target_type = request.args.get('target_type', '')
    target_id = request.args.get('target_id', '')
    return render_template('report.html', target_type=target_type, target_id=target_id)


# 관리자 대시보드: 신고/자동조치 내역, 유저, 상품 목록
@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM report ORDER BY created_at DESC")
    reports = cursor.fetchall()
    cursor.execute("SELECT * FROM admin_action_log ORDER BY created_at DESC")
    actions = cursor.fetchall()
    cursor.execute("SELECT * FROM user ORDER BY created_at DESC")
    users = cursor.fetchall()
    cursor.execute("SELECT * FROM product ORDER BY created_at DESC")
    products = cursor.fetchall()
    return render_template('admin.html', reports=reports, actions=actions, users=users, products=products)


@app.route('/admin/user/<user_id>/suspend', methods=['POST'])
@login_required
@admin_required
def admin_suspend_user(user_id):
    if user_id == session['user_id']:
        flash('자기 자신은 정지할 수 없습니다.')
        return redirect(url_for('admin_dashboard'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM user WHERE id = ?", (user_id,))
    if cursor.fetchone() is None:
        abort(404)
    cursor.execute("UPDATE user SET status = 'suspended', session_token = NULL WHERE id = ?", (user_id,))
    cursor.execute(
        "INSERT INTO admin_action_log (id, actor_type, actor_id, action, target_type, target_id, reason, created_at) "
        "VALUES (?, 'admin', ?, 'suspend_user', 'user', ?, NULL, ?)",
        (str(uuid.uuid4()), session['user_id'], user_id, utcnow_str())
    )
    db.commit()
    log_action('admin_suspend_user', target_type='user', target_id=user_id, success=True)
    flash('사용자를 정지했습니다.')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<user_id>/unsuspend', methods=['POST'])
@login_required
@admin_required
def admin_unsuspend_user(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM user WHERE id = ?", (user_id,))
    if cursor.fetchone() is None:
        abort(404)
    cursor.execute("UPDATE user SET status = 'active', report_count = 0 WHERE id = ?", (user_id,))
    cursor.execute(
        "INSERT INTO admin_action_log (id, actor_type, actor_id, action, target_type, target_id, reason, created_at) "
        "VALUES (?, 'admin', ?, 'unsuspend_user', 'user', ?, NULL, ?)",
        (str(uuid.uuid4()), session['user_id'], user_id, utcnow_str())
    )
    db.commit()
    log_action('admin_unsuspend_user', target_type='user', target_id=user_id, success=True)
    flash('정지를 해제했습니다.')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<user_id>/grant', methods=['POST'])
@login_required
@admin_required
def admin_grant_balance(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM user WHERE id = ?", (user_id,))
    if cursor.fetchone() is None:
        abort(404)
    amount_raw = request.form.get('amount', '')
    try:
        amount = int(amount_raw)
    except (TypeError, ValueError):
        flash('금액은 숫자로 입력해주세요.')
        return redirect(url_for('admin_dashboard'))
    if amount <= 0:
        flash('금액은 0보다 커야 합니다.')
        return redirect(url_for('admin_dashboard'))
    cursor.execute("UPDATE user SET balance = balance + ? WHERE id = ?", (amount, user_id))
    cursor.execute(
        "INSERT INTO admin_action_log (id, actor_type, actor_id, action, target_type, target_id, reason, created_at) "
        "VALUES (?, 'admin', ?, 'grant_balance', 'user', ?, ?, ?)",
        (str(uuid.uuid4()), session['user_id'], user_id, str(amount), utcnow_str())
    )
    db.commit()
    log_action('admin_grant_balance', target_type='user', target_id=user_id, success=True)
    flash('잔액을 지급했습니다.')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/<product_id>/block', methods=['POST'])
@login_required
@admin_required
def admin_block_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM product WHERE id = ?", (product_id,))
    if cursor.fetchone() is None:
        abort(404)
    cursor.execute("UPDATE product SET status = 'blocked' WHERE id = ?", (product_id,))
    cursor.execute(
        "INSERT INTO admin_action_log (id, actor_type, actor_id, action, target_type, target_id, reason, created_at) "
        "VALUES (?, 'admin', ?, 'block_product', 'product', ?, NULL, ?)",
        (str(uuid.uuid4()), session['user_id'], product_id, utcnow_str())
    )
    db.commit()
    log_action('admin_block_product', target_type='product', target_id=product_id, success=True)
    flash('상품을 차단했습니다.')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/<product_id>/restore', methods=['POST'])
@login_required
@admin_required
def admin_restore_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM product WHERE id = ?", (product_id,))
    if cursor.fetchone() is None:
        abort(404)
    cursor.execute("UPDATE product SET status = 'active', report_count = 0 WHERE id = ?", (product_id,))
    cursor.execute(
        "INSERT INTO admin_action_log (id, actor_type, actor_id, action, target_type, target_id, reason, created_at) "
        "VALUES (?, 'admin', ?, 'restore_product', 'product', ?, NULL, ?)",
        (str(uuid.uuid4()), session['user_id'], product_id, utcnow_str())
    )
    db.commit()
    log_action('admin_restore_product', target_type='product', target_id=product_id, success=True)
    flash('상품을 복구했습니다.')
    return redirect(url_for('admin_dashboard'))


# nav에서 관리자 링크 노출 여부 판단용 (UI 편의일 뿐, 실제 방어는 admin_required가 담당)
@app.context_processor
def inject_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return {'current_user': None}
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
    return {'current_user': cursor.fetchone()}


# 채팅/DM 공용 rate limiting: 5초 윈도우 내 5회 초과 전송 차단
_chat_rate_limits = {}
CHAT_RATE_WINDOW_SECONDS = 5
CHAT_RATE_MAX_MESSAGES = 5


def check_chat_rate_limit(user_id):
    """5초 이내 5회 초과 전송이면 False. 윈도우를 벗어난 타임스탬프는 이벤트마다 정리하고,
    정리 후 리스트가 비면 키 자체를 dict에서 삭제해 재방문하지 않는 유저의 항목이
    프로세스 수명 내내 쌓이지 않게 한다."""
    now = datetime.now(timezone.utc).timestamp()
    timestamps = [t for t in _chat_rate_limits.get(user_id, []) if now - t < CHAT_RATE_WINDOW_SECONDS]
    if not timestamps:
        _chat_rate_limits.pop(user_id, None)
    if len(timestamps) >= CHAT_RATE_MAX_MESSAGES:
        _chat_rate_limits[user_id] = timestamps
        return False
    timestamps.append(now)
    _chat_rate_limits[user_id] = timestamps
    return True


# 실시간 채팅: 클라이언트가 메시지를 보내면 전체 브로드캐스트 + DB 영구 저장
@socketio.on('send_message')
def handle_send_message_event(data):
    # 소켓의 session은 handshake 시점의 사본이라 로그아웃/정지가 반영되지 않으므로
    # 매 이벤트마다 DB 상태와 session_token을 재검증한다.
    user = socket_user_or_none()
    if user is None:
        return
    if not isinstance(data, dict):
        return
    message = data.get('message', '')
    if not isinstance(message, str):
        return
    message = message.strip()
    if not message or len(message) > 500:
        return

    if not check_chat_rate_limit(user['id']):
        emit('rate_limited', {'scope': 'global'})
        return

    db = get_db()
    cursor = db.cursor()
    message_id = str(uuid.uuid4())
    created_at = utcnow_str()
    cursor.execute(
        "INSERT INTO global_message (id, sender_id, sender_username, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (message_id, user['id'], user['username'], message, created_at)
    )
    db.commit()

    # 클라이언트가 보낸 dict를 재사용하지 않고 서버가 세션 기준으로 새 payload를 구성 (신원 위조 방지)
    payload = {
        'message_id': message_id,
        'username': user['username'],
        'message': message,
        'created_at': created_at,
    }
    send(payload, broadcast=True)


# 1:1 채팅 room 입장: 요청된 room의 참여자 중 하나인지 서버측에서 반드시 재검증
@socketio.on('join_dm')
def handle_join_dm(data):
    user = socket_user_or_none()
    if user is None:
        return
    if not isinstance(data, dict):
        return
    room_id = data.get('room_id', '')
    participants = dm_room_participants(room_id)
    if participants is None or user['id'] not in participants:
        log_action('dm_join_denied', target_type='room',
                   target_id=room_id if isinstance(room_id, str) else None, success=False)
        return
    join_room(room_id)


# 1:1 메시지 전송: join 시점 검증을 신뢰하지 않고 매번 재검증
@socketio.on('send_dm')
def handle_send_dm(data):
    user = socket_user_or_none()
    if user is None:
        return
    if not isinstance(data, dict):
        return
    room_id = data.get('room_id', '')
    participants = dm_room_participants(room_id)
    if participants is None or user['id'] not in participants:
        log_action('dm_join_denied', target_type='room',
                   target_id=room_id if isinstance(room_id, str) else None, success=False)
        return

    message = data.get('message', '')
    if not isinstance(message, str):
        return
    message = message.strip()
    if not message or len(message) > 500:
        return

    if not check_chat_rate_limit(user['id']):
        emit('rate_limited', {'scope': 'dm'})
        return

    other_id = participants[0] if participants[1] == user['id'] else participants[1]
    db = get_db()
    cursor = db.cursor()
    dm_id = str(uuid.uuid4())
    created_at = utcnow_str()
    cursor.execute(
        "INSERT INTO direct_message (id, sender_id, receiver_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (dm_id, user['id'], other_id, message, created_at)
    )
    db.commit()
    payload = {
        'message_id': dm_id,
        'sender_id': user['id'],
        'username': user['username'],
        'message': message,
        'created_at': created_at,
    }
    emit('dm_message', payload, room=room_id)


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


@app.errorhandler(413)
def handle_payload_too_large(e):
    return render_template('error.html', code=413, message='업로드 용량이 너무 큽니다 (최대 5MB).'), 413


@app.errorhandler(429)
def handle_rate_limit(e):
    return render_template('error.html', code=429, message='요청이 너무 많습니다. 잠시 후 다시 시도해주세요.'), 429


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
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=(), usb=()'
    # 로컬 http 개발 시 HSTS가 걸리면 브라우저가 강제로 https 리다이렉트를 시도해 불편하므로 배포시에만 적용
    if not FLASK_DEBUG:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


if __name__ == '__main__':
    socketio.run(app, debug=FLASK_DEBUG)
