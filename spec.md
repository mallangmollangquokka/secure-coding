# Tiny Second-hand Shopping Platform — 상세 설계 스펙 (v2, 실제 코드 확인 후 작성)

> 이 문서는 `ugonfor/secure-coding` 저장소의 **실제 `app.py`, `templates/*.html`, `enviroments.yaml`을 codeload.github.com으로 직접 받아서 읽고** 작성함.
> v1(이전 버전)은 코드를 안 읽고 추측으로 쓴 부분이 많아 전량 폐기.

---

## 0. 기존 코드 원본 그대로 정리 (사실관계, 추측 없음)

### 스택
- Flask + Flask-SocketIO
- DB: 순수 `sqlite3` (SQLAlchemy는 `enviroments.yaml`에 설치되어 있지만 코드에서 미사용)
- 템플릿: Jinja2, `base.html` 상속 구조. **`|safe` 필터는 어디에도 없음** → 서버사이드 XSS 자동이스케이프는 기본적으로 살아있음

### 기존 DB 스키마 (원본 그대로, 손대지 않음)
```sql
CREATE TABLE IF NOT EXISTS user (
    id TEXT PRIMARY KEY,        -- uuid4 문자열
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,     -- 평문 저장 중
    bio TEXT
);

CREATE TABLE IF NOT EXISTS product (
    id TEXT PRIMARY KEY,        -- uuid4 문자열
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price TEXT NOT NULL,        -- 문자열! 숫자 검증 없음
    seller_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report (
    id TEXT PRIMARY KEY,
    reporter_id TEXT NOT NULL,
    target_id TEXT NOT NULL,    -- 유저/상품 구분 컬럼 없음
    reason TEXT NOT NULL
);
```

### 기존 라우트 (원본, `app.py` 기준)
| 라우트 | 메서드 | 인증 | 비고 |
|---|---|---|---|
| `/` | GET | ✕ | 로그인 시 dashboard로 redirect |
| `/register` | GET/POST | ✕ | 중복 username만 체크, 비밀번호 검증 없음 |
| `/login` | GET/POST | ✕ | 평문 비교, 시도 횟수 제한 없음 |
| `/logout` | **GET** | 세션 체크 없음(그냥 pop) | 상태변경인데 GET |
| `/dashboard` | GET | ✓ | 본인 정보 + 전체 상품 리스트 |
| `/profile` | GET/POST | ✓ | bio만 수정 가능. 비밀번호 변경 없음 |
| `/product/new` | GET/POST | ✓ | title/description/price 서버검증 전혀 없음 |
| `/product/<product_id>` | GET | ✕ | 누구나 조회 가능, 판매자 정보(username) 같이 노출 |
| `/report` | GET/POST | ✓ | target_id에 사용자ID/상품ID 자유 입력(구분 로직 없음) |
| Socket `send_message` | - | **인증 체크 전혀 없음** | 세션 확인 없이 누구나 emit 가능, 전체 broadcast |

### 원본에 없는 것 (전량 신규 구현 필요, 추측 아님)
- 비밀번호 변경, 상품 수정/삭제, 상품 검색, 1:1 채팅, 신고 자동처리(차단/휴면), 송금, 관리자 기능, 공개 프로필 조회

### 원본의 명백한 보안 결함 (코드 직접 근거 있음)
1. `app.config['SECRET_KEY'] = 'secret!'` — 하드코딩
2. `socketio.run(app, debug=True)` — 배포 시 반드시 꺼야 함
3. 비밀번호 평문 저장 + 평문 비교
4. CSRF 토큰 전무 (모든 `<form method="post">`에 토큰 없음)
5. 로그인 실패 횟수 제한/계정 잠금 없음
6. 서버측 입력 검증 전무 (username 길이, password 복잡도, price 숫자 여부 등 전부 없음)
7. Socket.IO 핸들러에 인증 체크 없음 — 소켓으로 직접 연결하면 로그인 없이도 메시지 전송 가능
8. `/logout`이 GET → 상태 변경 요청이 GET으로 열려있음
9. `report` 테이블에 유저/상품 신고 구분 컬럼이 없어서, 자동 처리(신고 누적 시 차단) 로직을 짜려면 대상 종류부터 구분하는 컬럼 추가 필요

세션 쿠키 정정: Flask는 `SESSION_COOKIE_HTTPONLY=True`가 기본값이라 HttpOnly는 별도 설정 없이도 켜져 있음. **Secure, SameSite만 명시적으로 설정 안 된 상태.**

---

## 1. DB 마이그레이션 계획 (기존 테이블 갈아엎지 않고 ALTER로 확장)

```sql
-- user 테이블 확장
ALTER TABLE user ADD COLUMN role TEXT NOT NULL DEFAULT 'user';
ALTER TABLE user ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE user ADD COLUMN balance INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user ADD COLUMN report_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user ADD COLUMN locked_until TEXT DEFAULT NULL;
ALTER TABLE user ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE user ADD COLUMN session_token TEXT DEFAULT NULL;  -- 로그인/정지/비번변경 시 갱신 → 기존 세션 실시간 무효화용 (§3-1-B)
-- password 컬럼명은 유지하되, 저장 시점부터 bcrypt 해시 문자열을 넣도록 애플리케이션 코드만 변경
-- 기존에 평문으로 만든 테스트 계정이 있다면 재가입 필요 (일괄 변환 불가)

-- product 테이블 재생성 (price를 TEXT -> INTEGER로 변경. 결정됨: 실데이터 없는 초기 단계라 재생성 비용 0)
CREATE TABLE product_new (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price >= 0),
    seller_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',       -- 'active' | 'blocked' | 'deleted'
    report_count INTEGER NOT NULL DEFAULT 0,
    image_filename TEXT DEFAULT NULL,             -- 결정됨: 이미지 업로드 포함
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO product_new (id, title, description, price, seller_id)
    SELECT id, title, description, CAST(price AS INTEGER), seller_id FROM product;
DROP TABLE product;
ALTER TABLE product_new RENAME TO product;
-- 주의: 기존 price에 숫자 아닌 값이 있으면 CAST 결과가 0이 됨 -> 재생성 전 기존 데이터 확인/초기화 권장

-- report 테이블 확장
ALTER TABLE report ADD COLUMN target_type TEXT NOT NULL DEFAULT 'product'; -- 'user' | 'product'
ALTER TABLE report ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE report ADD COLUMN auto_action_taken INTEGER NOT NULL DEFAULT 0;   -- 0/1, 감사로그용
ALTER TABLE report ADD COLUMN auto_action_at TEXT DEFAULT NULL;
-- report.html 폼에도 target_type 선택 라디오 버튼 추가 필요

-- 관리자 조치 감사 로그 (신규)
CREATE TABLE IF NOT EXISTS admin_action_log (
    id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,       -- 'system'(자동) | 'admin'(수동)
    actor_id TEXT,                  -- admin 수동조치일 때만 관리자 user.id
    action TEXT NOT NULL,           -- 'block_product' | 'suspend_user' | 'restore_product' | 'unsuspend_user' 등
    target_type TEXT NOT NULL,      -- 'user' | 'product'
    target_id TEXT NOT NULL,
    reason TEXT,                    -- 관련 report.id 또는 사유
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 신규 테이블
CREATE TABLE IF NOT EXISTS direct_message (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS "transaction" (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

> `report_count`는 애플리케이션 레벨에서 INSERT 시점에 대상 카운트를 +1 하고, 임계값(예: 5회) 초과 시 status를 자동 변경하는 방식으로 구현 (DB 트리거 대신 Python 코드에서 처리 — 기존 스타일과 일관성 유지).

---

## 2. 신규/보강 라우트 설계 (기존 네이밍 `/product/...` singular 유지)

| 라우트 | 메서드 | 인증 | 인가 | 비고 |
|---|---|---|---|---|
| `/register` | POST | ✕ | - | **보강**: 서버 검증 추가, bcrypt 해싱 |
| `/login` | POST | ✕ | - | **보강**: 실패 카운트, 잠금 로직 |
| `/logout` | **POST로 변경** | ✓ | - | GET→POST, nav 링크를 form+button으로 교체 |
| `/user/<user_id>` | GET | ✕ | - | 신규. bio/username만 공개 |
| `/profile` | POST | ✓ | 본인만 | 기존 유지 |
| `/profile/password` | POST | ✓ | 본인만 | 신규 |
| `/product/new` | POST(multipart) | ✓ | - | **보강**: price 숫자 검증, 길이 제한, 이미지 업로드 처리 |
| `/product/<product_id>` | GET | ✕ | status='active'만 노출 | 기존 유지 + status 필터 |
| `/product/<product_id>/edit` | GET/POST | ✓ | seller_id==현재유저 | 신규 |
| `/product/<product_id>/delete` | POST | ✓ | seller_id==현재유저 | 신규, soft delete |
| `/product/search` | GET | ✕ | - | 신규, title LIKE 검색(파라미터 바인딩 필수) |
| `/report` | POST | ✓ | - | **보강**: target_type 추가, 자기신고 방지, 임계값 처리 |
| `/transfer` | POST | ✓ | 잔액검증 | 신규 |
| `/message/<user_id>` | GET/POST + socket room | ✓ | sender/receiver 본인만 | 신규 1:1 채팅 |
| `/admin` | GET | ✓ | role=='admin' | 신규, 신고 목록 + 자동조치 내역 대시보드 |
| `/admin/user/<user_id>/suspend` | POST | ✓ | role=='admin' | 신규, admin_action_log 기록 |
| `/admin/user/<user_id>/unsuspend` | POST | ✓ | role=='admin' | 신규(복구), admin_action_log 기록 |
| `/admin/product/<product_id>/delete` | POST | ✓ | role=='admin' | 신규, admin_action_log 기록 |
| `/admin/product/<product_id>/restore` | POST | ✓ | role=='admin' | 신규(복구), admin_action_log 기록 |
| Socket `send_message` | - | **세션 체크 + 길이검증 + rate limit 추가** | - | 인증 안 되면 disconnect, 메시지 500자 제한, 5초당 5회 제한 |

---

## 2-1. 이미지 업로드 설계 (결정됨: 포함)

- 저장 위치: `static/uploads/products/` (Flask static 하위, 디렉토리 리스팅 비활성화 확인)
- 파일명: 원본 파일명 신뢰 금지 → `uuid4() + 검증된 확장자`로 서버가 새로 생성 (path traversal 방지)
- 확장자 화이트리스트: `jpg, jpeg, png, gif, webp` — **`svg` 제외** (SVG는 내부에 `<script>` 포함 가능해 XSS 벡터가 됨)
- 확장자만 검사하는 걸로 끝내지 말고, `Pillow`의 `Image.open(file).verify()`로 실제 이미지 파일인지 내용 검증 (확장자만 바꾼 위장 파일 방지)
- 업로드 용량 제한: `app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024` (5MB) 등 서버에서 강제
- 상품 삭제 시 연결된 이미지 파일도 같이 정리(orphan 파일 방지)
- product 테이블의 `image_filename` 컬럼에 파일명만 저장, 조회 시 `url_for('static', filename='uploads/products/' + image_filename)`로 렌더링

## 2-2. 관리자 계정 최초 생성 (결정됨: 환경변수 자동 시딩 방식, 위 B안)

- 회원가입 폼에는 role 선택지 없음 (모든 신규 가입자는 무조건 `role='user'`로 고정, 서버에서 강제)
- `.env`에 `ADMIN_USERNAME`, `ADMIN_PASSWORD` 정의
- `init_db()` 실행 시: 해당 username의 유저가 없으면 `role='admin'`으로 신규 생성, 비밀번호는 반드시 bcrypt 해싱 후 저장
- README에 "관리자 테스트 계정은 .env의 ADMIN_USERNAME/ADMIN_PASSWORD로 로그인" 이라고 명시 (채점자가 바로 확인 가능하게)

## 3. 보안 보강 작업 목록 (우선순위 순)

1. `SECRET_KEY`를 환경변수로 이동 (`.env` + `python-dotenv`)
2. `debug=False`(배포) / 개발 시에만 True (환경변수로 분기)
3. 비밀번호 `bcrypt` 해싱/검증
4. CSRF: `Flask-WTF`의 `CSRFProtect(app)` + 모든 폼에 `{{ csrf_token() }}`
5. 로그인 실패 5회 이상 시 계정 잠금
6. 서버측 입력 검증 (username, password, price, title/description)
7. `/logout` GET→POST 전환 (base.html 네비게이션 수정 포함)
8. Socket 핸들러 인증 체크 추가
9. 세션 쿠키 `SESSION_COOKIE_SECURE=True`(배포시), `SESSION_COOKIE_SAMESITE='Lax'`
10. 상품 수정/삭제 시 소유권 서버측 재검증
11. 신고 자기자신 방지, 중복 신고 방지
12. **세션 만료 및 재인증**: 세션에 `last_activity` 타임스탬프 저장, 30분 유휴 시 자동 로그아웃(만료 세션 접근 시 재로그인 유도). `/profile/password`, `/transfer` 처럼 민감한 작업은 현재 비밀번호 재입력 요구(재인증)
13. **오류 처리**: `@app.errorhandler(400/403/404/500)` 전부 등록해서 커스텀 에러 페이지 반환, 어떤 경우에도 스택트레이스/DB 에러 메시지 그대로 노출 금지. 서버 로그(logging 모듈)에는 상세 기록하되 request.form 원본(비밀번호 포함 가능)은 절대 로깅 금지
14. **채팅 메시지 검증**: 소켓 핸들러에서 메시지 길이 제한(예: 500자), 빈 문자열/공백만 있는 메시지 거부. **주의(정정)**: 저장 시점에 미리 이스케이프하지 말 것 — 입력은 파라미터 바인딩으로 원본 그대로 저장하고, 이스케이프는 오직 렌더링(템플릿 출력) 시점에만 수행(Jinja2 autoescape가 이미 담당). 저장 전 이스케이프를 추가하면 이중 인코딩 버그(`&amp;lt;` 등)가 생김
15. **채팅 Rate Limiting**: 동일 세션이 짧은 시간(예: 5초) 내 일정 횟수(예: 5회) 초과 전송 시 서버에서 무시/경고, 세션별 마지막 전송 타임스탬프 메모리에 저장해서 체크
16. **연결 암호화**: 개발 중엔 ngrok이 자동으로 https/wss 제공. 상시 배포 시 PaaS(Render 등)의 TLS 종단 사용 필수 — 평문 http/ws로 운영 금지
17. **신고 감사 로그 + 관리자 검토**: `report`에 `auto_action_taken`(bool), `auto_action_at` 컬럼 추가. 임계값 넘어 자동 차단/휴면 처리될 때마다 별도 `admin_action_log` 테이블에 "무엇을, 언제, 어떤 신고 때문에" 기록. `/admin/reports`에서 관리자가 자동조치 내역을 보고 오판이면 되돌릴 수 있는 라우트(`/admin/product/<product_id>/restore`, `/admin/user/<user_id>/unsuspend`) 추가
18. **보안 헤더**: `after_request` 훅에서 `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` 설정 (또는 `flask-talisman` 라이브러리 사용)
19. **DB 파일 권한 최소화(로컬/자체 서버 배포 시에만 해당)**: `market.db` 파일 권한을 `chmod 600`으로 제한(소유자만 읽기/쓰기), **`.env` 파일도 동일하게 `chmod 600`** (SECRET_KEY, ADMIN_PASSWORD 등 포함), 앱 프로세스를 root가 아닌 별도 계정으로 실행, `.gitignore`에 이미 `*.db` 포함되어 있음(확인됨) → `.env`도 `.gitignore`에 추가. **Render 등 PaaS 배포 시에는 `.env` 파일 자체를 올리지 않고 플랫폼 환경변수로 대체하므로 이 항목 해당 없음**
20. **의존성 관리**: `requirements.txt`에 버전 고정(`flask==x.x.x` 형태), 개발 완료 후 `pip list --outdated` 또는 `pip-audit`로 알려진 취약점 있는 패키지 여부 1회 점검 후 보고서에 결과 기록

---

## 3-1. 외부 리뷰로 확인된 치명적 결함 3건 (설계 확정)

### (A) 송금 Race Condition 방지
- Python 레벨의 "잔액 조회 → 검증 → 차감"을 분리해서 처리하지 않음. **차감 쿼리 자체에 잔액 조건을 포함**해서 원자적으로 처리:
```sql
UPDATE user SET balance = balance - ? WHERE id = ? AND balance >= ?;
```
- 위 UPDATE의 `cursor.rowcount == 0`이면 잔액 부족으로 판단하고 즉시 전체 트랜잭션 롤백 (에러를 던지는 게 아니라 조건절이 매칭 안 된 것이므로 rowcount로 확인해야 함)
- 트랜잭션 시작 시 `db.execute("BEGIN IMMEDIATE")`로 격리 수준을 강제 (SQLite 기본 동시쓰기 제어가 약하므로 애플리케이션 레벨 검증에만 의존하지 않음)
- 차감(sender) → 증가(receiver) → `transaction` 테이블 INSERT까지 하나의 트랜잭션으로 묶고, 실패 시 전체 롤백

### (B) 계정 정지/비밀번호 변경 시 기존 세션 실시간 무효화
- Flask 기본 세션은 서명된 클라이언트 쿠키 방식이라, DB의 status만 바꿔서는 이미 로그인된 브라우저가 즉시 차단되지 않음
- user 테이블에 `session_token TEXT` 컬럼 추가 (로그인 성공 시 매번 새로 발급해서 저장, 세션 쿠키에도 같이 저장)
- `login_required` 데코레이터를 단순 "세션에 user_id 있는지"를 넘어서, **매 요청마다 DB 조회 → `status == 'active'` 확인 + 세션의 `session_token`과 DB의 `session_token`이 일치하는지 확인**하도록 강화 (불일치/비활성 시 세션 종료 후 로그인 페이지로)
- 관리자가 유저를 정지시키거나(`suspend`), 유저가 비밀번호를 변경하면 `session_token`을 새로 갱신 → 기존에 발급된 다른 세션들은 자동으로 다음 요청에서 튕겨나감
- Socket.IO 핸들러에도 동일한 per-request 상태 체크 적용 (연결 유지 중에도 정지되면 다음 메시지 전송 시 차단)

### (C) 1:1 채팅 Socket.IO room 검증 (BOLA 방지)
- room_id를 유추 가능한 값(예: 단순 두 user_id 조합 문자열)으로 만들되, **`join_room` 이벤트 핸들러에서 반드시**: 소켓의 현재 세션 `user_id`가 요청된 room의 `sender_id` 또는 `receiver_id` 중 하나와 일치하는지 서버측에서 검증 후에만 `join_room()` 호출
- 검증 실패 시 조인 거부 + 로그 기록 (누가 남의 방에 들어오려 했는지 추적 가능하게)
- 메시지 전송(`send_dm`) 이벤트에서도 매번 동일 검증 반복 (join 시점에만 확인하고 이후 안 하면, 세션 상태가 바뀌었을 때 여전히 취약)

## 3-2. 이미지 재인코딩 (defense-in-depth, 위험도는 완화되지만 비용 낮아 채택)
- `X-Content-Type-Options: nosniff`(§3-18) 적용으로 대부분의 polyglot 실행 경로는 이미 차단되지만, 방어 계층을 하나 더 추가
- 업로드된 이미지를 `Image.open(file).verify()`로 1차 검증한 뒤, **Pillow로 다시 읽어서 지정된 최대 해상도로 리사이즈 후 새 파일로 재저장** (EXIF 등 메타데이터 전량 제거 효과, 파일 내부에 숨겨진 비-이미지 데이터 소멸)

## 3-3. 마이그레이션 안전장치 (폭포수 엄격성)
- `report` 테이블에 `target_type` 컬럼을 추가하는 마이그레이션 스크립트 상단에 **실행 전 `SELECT COUNT(*) FROM report`가 0인지 확인하는 가드 코드**를 넣고, 0이 아니면 스크립트를 중단하고 수동 검토하도록 함 (현재는 실데이터 없음이 확인된 상태지만, 스크립트 자체에 안전장치를 남겨서 나중에 실수로 재실행해도 기존 데이터를 침묵 속에 오염시키지 않게 함)

## 4. 폴더 구조 (기존 단일 `app.py` 구조에서 점진적으로만 분리)

```
secure-coding-main/
├── app.py                 # 앱 초기화 + 라우트 등록
├── db.py                  # get_db, close_connection, init_db, 마이그레이션
├── auth.py                # register/login/logout, 해싱, 잠금
├── products.py            # product 관련 라우트
├── social.py              # report, message(1:1), transfer
├── admin.py                # 관리자 라우트
├── security.py             # login_required, owner_required, admin_required 데코레이터
├── templates/ (기존 + 신규 템플릿 추가)
├── static/uploads/products/  # 신규, 이미지 저장 (gitignore에 내용물 추가 권장)
├── .env                     # SECRET_KEY 등 (gitignore)
├── .gitignore
├── requirements.txt          # bcrypt, flask-wtf, python-dotenv 추가
└── tests/
```

---

## 4-1. CSS 스타일 컨벤션 (결정됨: 프레임워크 미도입, 기존 인라인 CSS 확장)

- `base.html`의 기존 스타일(Notion풍 미니멀, `.container`, `.flash`, 기본 `input/textarea/button` 스타일)을 그대로 유지하고 확장만 함. Bootstrap/Tailwind 등 신규 프레임워크 도입 안 함
- 신규 화면에서 필요한 클래스만 `base.html`의 `<style>` 블록에 추가:

| 신규 컴포넌트 | 클래스(제안) | 용도 |
|---|---|---|
| 상품 카드(검색결과/목록) | `.product-card`, `.product-thumb` | 썸네일+제목+가격 묶음, 썸네일 없으면 회색 placeholder |
| 상태 배지 | `.badge`, `.badge-blocked`, `.badge-suspended` | 차단/휴면 상태 시각 표시 (관리자 화면, 마이페이지) |
| 검색창 | `.search-bar` | 상품 목록 상단 검색 입력 |
| 위험 동작 버튼 | `.btn-danger` | 삭제/정지/차단 버튼 (기존 `button`과 색만 구분, 빨간 계열) |
| 관리자 테이블 | `.admin-table` | 신고 내역/유저 목록 등 표 형태 |
| 1:1 채팅 목록/스레드 | `.dm-list`, `.dm-thread`, `.dm-bubble` | 기존 `#chat`/`#messages` 스타일을 참고해서 확장 |
| 신고 대상유형 라디오 | `.radio-group` | `report.html`에 target_type(user/product) 선택 추가 시 |

- 색상은 기존 팔레트(`#007AFF` 파랑, `#333` 텍스트, `#e0e0e0` 보더) 그대로 재사용, `.btn-danger`만 빨간 계열(`#D32F2F`) 신규 추가
- 별도 `.css` 파일로 분리할지 여부: 컴포넌트 수가 적어서 지금처럼 `base.html` 안 인라인 유지해도 무방. 나중에 스타일 블록이 너무 길어지면(200줄 이상) 그때 `static/style.css`로 분리

## 5. 테스트 케이스

- 기존 기능(회원가입/로그인/상품등록/조회/채팅) 회귀 테스트
- 로그인 5회 실패 후 잠금 확인
- CSRF 토큰 없이 POST 시 403 확인
- 타인 상품 `/product/<product_id>/edit` 직접 접근 시 차단 확인
- price에 문자열 입력 시 400 처리 확인
- 소켓 비로그인 상태로 직접 연결 후 emit 시 차단 확인
- 신고 5회 누적 시 자동 status 변경 및 이후 404 처리 확인
- 송금 잔액 부족/동시 요청 시 정합성 확인

---

## 6. 배포 (결정됨: Render 무료 티어)

- 개발 중 테스트: `readme.md`에 이미 안내된 대로 `ngrok http 5000`
- **상시 배포: Render 무료 티어로 확정.** GitHub repo 연결만으로 자동 빌드/배포, HTTPS 자동 적용, 카드 등록 불필요
- **필수 코드 변경사항**:
  - `init_db()` 호출을 `if __name__ == '__main__':` 블록 밖(모듈 최상단)으로 이동 — gunicorn이 `app.py`를 모듈로 import할 때는 `__name__`이 `'__main__'`이 아니므로, 이 블록 안에만 두면 gunicorn 환경에서 `init_db()`가 전혀 실행되지 않음 (Phase 1 프롬프트 A-6 항목)
  - `requirements.txt`에 `gunicorn`, `gevent`, `gevent-websocket` 추가 (Python 3.13에서 eventlet 호환 문제로 gevent로 전환)
  - Start Command: `gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:$PORT app:app` (`$PORT` 바인딩 누락 시 서비스가 뜬 것처럼 보여도 응답 없음)
- **환경변수**: `.env` 파일을 배포 환경에 올리지 않고, Render 대시보드의 Environment Variables에 `SECRET_KEY`, `FLASK_DEBUG=false`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`를 직접 입력
- **감수하기로 결정한 트레이드오프**: Render 무료 티어는 디스크가 비영속적(ephemeral) — 재배포하거나 컨테이너가 재시작되면 `market.db`와 업로드된 상품 이미지(`static/uploads/products/`)가 초기화될 수 있음. Persistent Disk는 유료 플랜부터 지원됨. 이 프로젝트는 과제 데모/채점 목적이라 **재배포를 자주 하지 않는 선에서 이 리스크를 감수**하기로 결정 (데이터베이스를 Postgres로 옮기는 등의 대응은 하지 않음)
- 무료 티어는 15분 무요청 시 슬립 → 첫 요청 시 최대 ~60초 콜드스타트 발생. README에 안내 문구 추가
- 배포용 requirements와 로컬 개발용 `enviroments.yaml`(conda)을 병행 제공, README에 두 설치 방법 모두 안내

---

## 7. 체크리스트 대조표 (`secure_coding_checklist.csv` 28개 항목 전체, 보고서 제출용)

| Section | Checklist Item | 대응 위치 |
|---|---|---|
| 회원가입/프로필 | 서버측 입력 검증 | §3-6 |
| 회원가입/프로필 | CSRF 보호 | §3-4 |
| 회원가입/프로필 | 비밀번호 보안(해싱) | §3-3 |
| 회원가입/프로필 | 세션 쿠키 설정 | §3-9 |
| 회원가입/프로필 | 세션 만료 및 재인증 | §3-12 |
| 회원가입/프로필 | 실패 로그인 방어 | §3-5 |
| 회원가입/프로필 | 오류 메시지 | §3-13 |
| 상품 관리 | 폼 입력 검증 | §3-6 |
| 상품 관리 | XSS 방어 | Jinja2 autoescape 유지, `\|safe` 사용 금지(§0) |
| 상품 관리 | 인증된 사용자만 등록 | 기존에도 구현됨(§0) |
| 상품 관리 | 소유자 확인 | §3-10 |
| 상품 관리 | 데이터 무결성 | §1 (price INTEGER CHECK) |
| 채팅 | 메시지 내용 검증 | §3-14 |
| 채팅 | 사용자 인증 | §3-8 |
| 채팅 | 메시지 검증 | §3-14 |
| 채팅 | Rate Limiting | §3-15 |
| 채팅 | 연결 암호화 | §3-16 |
| 신고 | 폼 입력 검증 | §2 report 라우트 보강 |
| 신고 | 인증된 사용자 접근 | 기존에도 구현됨(§0) |
| 신고 | 데이터 무결성 및 로그 관리 | §3-17 |
| 신고 | 신고 남용 방지 | §3-11, §3-17(관리자 검토) |
| 전체 시스템 | ORM/파라미터 바인딩 | 기존에도 파라미터 바인딩 사용 중(§0) |
| 전체 시스템 | 데이터베이스 권한 | §3-19 |
| 전체 시스템 | 보안 헤더 설정 | §3-18 |
| 전체 시스템 | HTTPS 적용 | §3-16, §6 배포 |
| 전체 시스템 | 에러 및 예외 처리 | §3-13 |
| 전체 시스템 | 라이브러리 및 의존성 관리 | §3-20 |

> 28개 항목 전부 대응 위치가 있는 상태. 실제 구현 후에는 이 표를 "적용됨(✅) / 부분적용(△) / 미적용(❌)" 컬럼을 추가해서 보고서의 "체크리스트 작성 및 확인" 항목에 그대로 제출하면 됨.

## 8. 결정 사항 (확정됨)

- [x] 이미지 업로드 **포함**. 상세 설계는 2-1 참고
- [x] price 컬럼 **INTEGER로 재생성** (테이블 재생성 방식, 1번 마이그레이션 SQL 참고)
- [x] 관리자 계정 **환경변수 자동 시딩(B안)**. 상세 설계는 2-2 참고