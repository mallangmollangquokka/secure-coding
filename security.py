import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import g, request, session, abort
from PIL import Image
from werkzeug.utils import secure_filename

DATABASE = 'market.db'
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'

PASSWORD_MIN_LENGTH = 8
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{4,20}$')
TITLE_MAX = 100
DESCRIPTION_MAX = 2000

ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
EXT_TO_FORMAT = {
    '.jpg': 'JPEG',
    '.jpeg': 'JPEG',
    '.png': 'PNG',
    '.gif': 'GIF',
    '.webp': 'WEBP',
}
UPLOAD_DIR = os.path.join('static', 'uploads', 'products')
MAX_IMAGE_DIMENSION = 1920


def _utcnow_str():
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _get_db():
    # app.py의 get_db()와 동일한 패턴(요청당 flask.g에 연결 캐싱)을 그대로 재사용.
    # 같은 요청 컨텍스트 안에서는 app.py와 동일한 g._database를 공유한다.
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


def validate_password(password):
    """비밀번호가 정책을 만족하는지 검사해 위반 사항 메시지 목록을 반환한다. 빈 리스트면 통과."""
    errors = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f'최소 {PASSWORD_MIN_LENGTH}자 이상')
    if not re.search(r'[A-Z]', password):
        errors.append('영문 대문자 1개 이상')
    if not re.search(r'[a-z]', password):
        errors.append('영문 소문자 1개 이상')
    if not re.search(r'[0-9]', password):
        errors.append('숫자 1개 이상')
    if not re.search(r'[^A-Za-z0-9]', password):
        errors.append('특수문자 1개 이상')
    return errors


def validate_username(username):
    """사용자명이 정책(영문/숫자/밑줄 4~20자)을 만족하는지 검사한다. 빈 리스트면 통과."""
    errors = []
    if not USERNAME_RE.match(username):
        errors.append('영문, 숫자, 밑줄(_)로 이루어진 4~20자여야 합니다')
    return errors


def validate_product_title(title):
    errors = []
    if not (1 <= len(title.strip()) <= TITLE_MAX):
        errors.append(f'1~{TITLE_MAX}자 이내로 입력해주세요')
    return errors


def validate_product_description(description):
    errors = []
    if not (1 <= len(description.strip()) <= DESCRIPTION_MAX):
        errors.append(f'1~{DESCRIPTION_MAX}자 이내로 입력해주세요')
    return errors


REASON_MAX = 500
REPORT_THRESHOLD = 5


def validate_report_reason(reason):
    """신고 사유 검증. 스페이스바만 입력해도 길이 검증을 통과하지 못하도록 strip 후 검사한다."""
    errors = []
    if not (1 <= len(reason.strip()) <= REASON_MAX):
        errors.append(f'1~{REASON_MAX}자 이내로 입력해주세요')
    return errors


def validate_product_price(price_raw):
    """price 문자열을 정수로 파싱 시도. (errors, parsed_int) 튜플 반환 — 실패 시 parsed_int는 None."""
    errors = []
    try:
        price = int(price_raw)
    except (TypeError, ValueError):
        errors.append('가격은 숫자로 입력해주세요')
        return errors, None
    if price < 0:
        errors.append('가격은 0 이상이어야 합니다')
        return errors, None
    return errors, price


def owner_required(get_owner_id_fn):
    """라우트의 view_args로 소유자 id를 조회해 session['user_id']와 비교하는 인가 데코레이터.
    get_owner_id_fn이 None을 반환하면(리소스 없음) 그대로 통과시켜 라우트 본체가 404를 처리하게 한다."""
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(*args, **kwargs):
            owner_id = get_owner_id_fn(**request.view_args)
            if owner_id is not None and owner_id != session.get('user_id'):
                abort(403)
            return view_func(*args, **kwargs)
        return wrapped_view
    return decorator


def socket_user_or_none():
    """소켓 핸들러용 인증 확인. Flask-SocketIO의 session은 소켓 연결(handshake) 시점의
    사본이라 이후 로그아웃·비번변경·정지가 반영되지 않는다. 그래서 매 이벤트마다
    session의 user_id + DB status='active' + session_token 일치를 다시 확인해야
    실시간 무효화가 성립한다. 전부 통과하면 user Row, 아니면 None을 반환한다."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    db = _get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    if user is None or user['status'] != 'active' or session.get('session_token') != user['session_token']:
        return None
    return user


def admin_required(view_func):
    """login_required 뒤에 적용하는 관리자 전용 데코레이터. 세션의 role은 신뢰하지 않고
    매 요청마다 DB에서 role을 재조회한다."""
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        user_id = session.get('user_id')
        if not user_id:
            abort(403)
        db = _get_db()
        cursor = db.cursor()
        cursor.execute("SELECT role FROM user WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if row is None or row['role'] != 'admin':
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped_view


def get_product_seller_id(product_id):
    db = _get_db()
    cursor = db.cursor()
    cursor.execute("SELECT seller_id FROM product WHERE id = ?", (product_id,))
    row = cursor.fetchone()
    return row['seller_id'] if row else None


def log_action(action, target_type=None, target_id=None, success=True, actor_id=None, actor_username=None):
    """audit_log 테이블에 접근/조작 이력을 기록. 비밀번호 등 민감정보는 절대 전달/기록하지 않는다."""
    if actor_id is None:
        actor_id = session.get('user_id')
    user_agent_string = request.user_agent.string if request.user_agent and request.user_agent.string else None
    if user_agent_string is not None:
        user_agent_string = user_agent_string[:500]

    db = _get_db()
    cursor = db.cursor()
    cursor.execute(
        """INSERT INTO audit_log
           (id, actor_id, actor_username, action, target_type, target_id, ip_address, user_agent, success, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            actor_id,
            actor_username,
            action,
            target_type,
            target_id,
            request.remote_addr,
            user_agent_string,
            1 if success else 0,
            _utcnow_str(),
        )
    )
    db.commit()


def _safe_mode_for_format(image, save_format):
    if save_format == 'JPEG':
        return 'RGB'
    if save_format == 'PNG':
        return image.mode if image.mode in ('RGB', 'RGBA', 'P') else 'RGBA'
    if save_format == 'GIF':
        return image.mode if image.mode == 'P' else 'P'
    if save_format == 'WEBP':
        return image.mode if image.mode in ('RGB', 'RGBA') else 'RGBA'
    return 'RGB'


def save_product_image(file_storage):
    """상품 이미지를 검증 + Pillow로 재인코딩해서 저장한다.
    반환: (filename, None) 성공 / (None, error_message) 실패. 이미지가 없으면 (None, None)."""
    if not file_storage or not file_storage.filename:
        return None, None

    _, ext = os.path.splitext(secure_filename(file_storage.filename))
    ext = ext.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None, '지원하지 않는 이미지 형식입니다 (jpg, jpeg, png, gif, webp만 허용).'

    try:
        image = Image.open(file_storage)
        image.verify()
    except Exception:
        return None, '올바른 이미지 파일이 아닙니다.'

    file_storage.seek(0)

    try:
        image = Image.open(file_storage)
        save_format = EXT_TO_FORMAT[ext]

        if save_format == 'GIF':
            image.seek(0)  # 애니메이션 GIF는 첫 프레임만 저장

        image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))

        target_mode = _safe_mode_for_format(image, save_format)
        if image.mode != target_mode:
            image = image.convert(target_mode)

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        filename = f'{uuid.uuid4().hex}{ext}'
        save_path = os.path.join(UPLOAD_DIR, filename)
        image.save(save_path, format=save_format)
    except Exception:
        return None, '이미지 처리 중 오류가 발생했습니다.'

    return filename, None


def delete_product_image(filename):
    """상품 이미지 파일을 파일시스템에서 제거. 파일이 없어도 조용히 무시."""
    if not filename:
        return
    path = os.path.join(UPLOAD_DIR, filename)
    try:
        os.remove(path)
    except OSError:
        pass
