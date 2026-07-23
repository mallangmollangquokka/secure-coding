import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, send
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from security import (
    validate_password,
    validate_username,
    validate_product_title,
    validate_product_description,
    validate_product_price,
    owner_required,
    get_product_seller_id,
    log_action,
    save_product_image,
    delete_product_image,
    UPLOAD_DIR,
)

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
        title = request.form.get('title', '')
        description = request.form.get('description', '')
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
        title = request.form.get('title', '')
        description = request.form.get('description', '')
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


# 신고하기
@app.route('/report', methods=['GET', 'POST'])
@login_required
@limiter.limit('5 per minute', methods=['POST'])
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
